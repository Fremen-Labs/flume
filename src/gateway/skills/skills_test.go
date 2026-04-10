package skills

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Unit tests for the skill parser, registry, and generator.
// ─────────────────────────────────────────────────────────────────────────────

const sampleSkillMD = `---
name: test-skill
version: 1.0.0
inception: hybrid
ensemble_size: 2
creativity_level: 1
---

## Description
A test skill for unit testing the parser.

## Input Schema
` + "```json" + `
{
  "type": "object",
  "properties": {
    "name": { "type": "string" }
  },
  "required": ["name"]
}
` + "```" + `

## Output Schema
` + "```json" + `
{
  "type": "object",
  "properties": {
    "greeting": { "type": "string" }
  }
}
` + "```" + `

## Context Requirements
- index: flume-elastro-graph
- query: functions_called contains {name}

## Validation Rules
- Output must include a greeting field
- Greeting must not be empty

## Prompt Template
Hello {name}, welcome to Flume!
`

func TestParseSkillFile(t *testing.T) {
	// Write sample to temp file
	dir := t.TempDir()
	path := filepath.Join(dir, "test-skill.skill.md")
	if err := os.WriteFile(path, []byte(sampleSkillMD), 0o644); err != nil {
		t.Fatalf("write temp file: %v", err)
	}

	def, err := ParseSkillFile(path)
	if err != nil {
		t.Fatalf("ParseSkillFile failed: %v", err)
	}

	// Verify front-matter
	if def.Name != "test-skill" {
		t.Errorf("Name = %q, want %q", def.Name, "test-skill")
	}
	if def.Version != "1.0.0" {
		t.Errorf("Version = %q, want %q", def.Version, "1.0.0")
	}
	if def.Inception != InceptionHybrid {
		t.Errorf("Inception = %q, want %q", def.Inception, InceptionHybrid)
	}
	if def.EnsembleSize != 2 {
		t.Errorf("EnsembleSize = %d, want %d", def.EnsembleSize, 2)
	}
	if def.CreativityLevel != 1 {
		t.Errorf("CreativityLevel = %d, want %d", def.CreativityLevel, 1)
	}

	// Verify description
	if !strings.Contains(def.Description, "test skill") {
		t.Errorf("Description = %q, want contains 'test skill'", def.Description)
	}

	// Verify schemas are valid JSON
	if def.InputSchema == nil {
		t.Fatal("InputSchema is nil")
	}
	var inputSchema map[string]interface{}
	if err := json.Unmarshal(def.InputSchema, &inputSchema); err != nil {
		t.Errorf("InputSchema is not valid JSON: %v", err)
	}
	if inputSchema["type"] != "object" {
		t.Errorf("InputSchema type = %v, want 'object'", inputSchema["type"])
	}

	if def.OutputSchema == nil {
		t.Fatal("OutputSchema is nil")
	}

	// Verify context requirements
	if len(def.ContextReqs) != 1 {
		t.Fatalf("ContextReqs len = %d, want 1", len(def.ContextReqs))
	}
	if def.ContextReqs[0].Index != "flume-elastro-graph" {
		t.Errorf("ContextReqs[0].Index = %q, want %q", def.ContextReqs[0].Index, "flume-elastro-graph")
	}

	// Verify validation rules
	if len(def.ValidationRules) != 2 {
		t.Errorf("ValidationRules len = %d, want 2", len(def.ValidationRules))
	}

	// Verify prompt template
	if !strings.Contains(def.PromptTemplate, "{name}") {
		t.Errorf("PromptTemplate should contain '{name}'")
	}
}

func TestParseSkillFile_MissingName(t *testing.T) {
	dir := t.TempDir()
	content := "---\nversion: 1.0.0\n---\n\n## Description\nNo name.\n"
	path := filepath.Join(dir, "bad.skill.md")
	os.WriteFile(path, []byte(content), 0o644)

	_, err := ParseSkillFile(path)
	if err == nil {
		t.Fatal("expected error for missing name, got nil")
	}
	if !strings.Contains(err.Error(), "missing required 'name'") {
		t.Errorf("error = %q, want contains 'missing required name'", err.Error())
	}
}

func TestParseSkillFile_InvalidInception(t *testing.T) {
	dir := t.TempDir()
	content := "---\nname: bad\ninception: turbo\n---\n\n## Description\nBad inception.\n"
	path := filepath.Join(dir, "bad.skill.md")
	os.WriteFile(path, []byte(content), 0o644)

	_, err := ParseSkillFile(path)
	if err == nil {
		t.Fatal("expected error for invalid inception mode, got nil")
	}
}

func TestRegistry_LoadAndGet(t *testing.T) {
	// Create a temp skills directory with a valid skill
	dir := t.TempDir()
	path := filepath.Join(dir, "test-skill.skill.md")
	os.WriteFile(path, []byte(sampleSkillMD), 0o644)

	// Override env for discovery
	t.Setenv("FLUME_SKILLS_DIR", dir)

	registry := NewSkillRegistry()
	ctx := context.Background()

	if err := registry.LoadAll(ctx); err != nil {
		t.Fatalf("LoadAll failed: %v", err)
	}

	if registry.Count() != 1 {
		t.Errorf("Count = %d, want 1", registry.Count())
	}

	skill := registry.Get("test-skill")
	if skill == nil {
		t.Fatal("Get('test-skill') returned nil")
	}
	if skill.Definition.Version != "1.0.0" {
		t.Errorf("Version = %q, want %q", skill.Definition.Version, "1.0.0")
	}

	// Verify not found
	if registry.Get("nonexistent") != nil {
		t.Error("Get('nonexistent') should return nil")
	}
}

func TestHybridHandler_Execute(t *testing.T) {
	def := &SkillDefinition{
		Name:      "test",
		Version:   "1.0.0",
		Inception: InceptionHybrid,
		InputSchema: json.RawMessage(`{
			"type": "object",
			"properties": {"name": {"type": "string"}},
			"required": ["name"]
		}`),
		PromptTemplate: "Hello {name}!",
	}

	handler := &HybridHandler{def: def}
	ctx := context.Background()

	// Valid input
	out, err := handler.Execute(ctx, json.RawMessage(`{"name": "World"}`))
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	var result map[string]interface{}
	json.Unmarshal(out, &result)
	if result["mode"] != "hybrid" {
		t.Errorf("mode = %v, want 'hybrid'", result["mode"])
	}

	// Missing required field
	_, err = handler.Execute(ctx, json.RawMessage(`{}`))
	if err == nil {
		t.Fatal("expected validation error for missing required field")
	}
}

func TestToGoStructName(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"hello-world", "HelloWorld"},
		{"fastapi-crud-generator", "FastapiCrudGenerator"},
		{"simple", "Simple"},
		{"multi.part.name", "MultiPartName"},
		{"under_score", "UnderScore"},
	}
	for _, tt := range tests {
		got := toGoStructName(tt.input)
		if got != tt.want {
			t.Errorf("toGoStructName(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestValidateJSON_RequiredFields(t *testing.T) {
	schema := json.RawMessage(`{"required": ["name", "age"]}`)

	// All required present
	err := validateJSON(json.RawMessage(`{"name": "test", "age": 25}`), schema)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}

	// Missing required field
	err = validateJSON(json.RawMessage(`{"name": "test"}`), schema)
	if err == nil {
		t.Error("expected error for missing 'age' field")
	}

	// No schema = no validation
	err = validateJSON(json.RawMessage(`{}`), nil)
	if err != nil {
		t.Errorf("nil schema should skip validation, got: %v", err)
	}
}

func TestCompileToFile(t *testing.T) {
	def := &SkillDefinition{
		Name:      "test-compile",
		Version:   "2.0.0",
		Inception: InceptionFull,
	}

	dir := t.TempDir()
	outPath, err := CompileToFile(def, dir)
	if err != nil {
		t.Fatalf("CompileToFile failed: %v", err)
	}

	// Verify file exists
	if _, err := os.Stat(outPath); os.IsNotExist(err) {
		t.Fatalf("generated file does not exist: %s", outPath)
	}

	// Verify content
	data, _ := os.ReadFile(outPath)
	content := string(data)
	if !strings.Contains(content, "TestCompileHandler") {
		t.Error("generated file should contain struct name 'TestCompileHandler'")
	}
	if !strings.Contains(content, "DO NOT EDIT") {
		t.Error("generated file should contain DO NOT EDIT header")
	}
	if !strings.Contains(content, "package generated") {
		t.Error("generated file should be in package 'generated'")
	}
}

func TestCompileToFile_NonFullInception(t *testing.T) {
	def := &SkillDefinition{
		Name:      "hybrid-skill",
		Inception: InceptionHybrid,
	}

	_, err := CompileToFile(def, t.TempDir())
	if err == nil {
		t.Fatal("expected error when compiling non-full inception skill")
	}
}
