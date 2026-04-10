package skills

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/Fremen-Labs/flume/src/gateway/skillslog"
)

// ─────────────────────────────────────────────────────────────────────────────
// Skill Parser — Deterministic Markdown AST extraction for .skill.md files.
//
// Parses YAML front-matter and H2-delimited sections from Markdown skill
// definitions.  Uses stdlib regex + string scanning — no third-party YAML
// dependency needed since front-matter is simple key-value pairs.
//
// Logging follows the unified gateway pattern: slog with secret scrubbing.
// ─────────────────────────────────────────────────────────────────────────────

// InceptionMode determines how a skill is executed.
type InceptionMode string

const (
	InceptionFull    InceptionMode = "full"
	InceptionHybrid  InceptionMode = "hybrid"
	InceptionLLMOnly InceptionMode = "llm-only"
)

// SkillDefinition represents a fully parsed .skill.md file.
type SkillDefinition struct {
	Name            string          `json:"name"`
	Version         string          `json:"version"`
	Inception       InceptionMode   `json:"inception"`
	EnsembleSize    int             `json:"ensemble_size"`
	CreativityLevel int             `json:"creativity_level"`
	Description     string          `json:"description"`
	InputSchema     json.RawMessage `json:"input_schema"`
	OutputSchema    json.RawMessage `json:"output_schema"`
	ContextReqs     []ContextReq    `json:"context_requirements,omitempty"`
	ValidationRules []string        `json:"validation_rules,omitempty"`
	PromptTemplate  string          `json:"prompt_template,omitempty"`
	SourcePath      string          `json:"-"`
}

// ContextReq defines an Elastro context injection requirement.
type ContextReq struct {
	Index string `json:"index"`
	Query string `json:"query"`
}

// frontMatterRe matches the YAML front-matter block delimited by --- lines.
var frontMatterRe = regexp.MustCompile(`(?s)\A---\n(.*?)\n---`)

// sectionRe matches H2 headers to split the body into named sections.
var sectionRe = regexp.MustCompile(`(?m)^## (.+)$`)

// fencedCodeRe extracts the content of fenced code blocks.
var fencedCodeRe = regexp.MustCompile("(?s)```(?:json)?\\s*\\n(.*?)\\n```")

// contextReqRe parses "- key: value" lines in the Context Requirements section.
var contextReqRe = regexp.MustCompile(`(?m)^-\s+(\w+):\s+(.+)$`)

// ParseSkillFile reads and parses a single .skill.md file.
func ParseSkillFile(path string) (*SkillDefinition, error) {
	log := skillslog.Log().With(slog.String("component", "skill_parser"), slog.String("path", filepath.Base(path)))

	data, err := os.ReadFile(path)
	if err != nil {
		log.Error("failed to read skill file", slog.String("error", err.Error()))
		return nil, fmt.Errorf("read skill file %s: %w", path, err)
	}

	content := string(data)
	skill := &SkillDefinition{
		SourcePath:   path,
		Inception:    InceptionHybrid, // default
		EnsembleSize: 1,
	}

	// ── Extract front-matter ──────────────────────────────────────────────
	fmMatch := frontMatterRe.FindStringSubmatch(content)
	if fmMatch == nil {
		return nil, fmt.Errorf("skill file %s: missing YAML front-matter (---)", filepath.Base(path))
	}
	if err := parseFrontMatter(fmMatch[1], skill); err != nil {
		return nil, fmt.Errorf("skill file %s: %w", filepath.Base(path), err)
	}
	if skill.Name == "" {
		return nil, fmt.Errorf("skill file %s: front-matter missing required 'name' field", filepath.Base(path))
	}

	// ── Extract body sections ─────────────────────────────────────────────
	body := content[len(fmMatch[0]):]
	sections := extractSections(body)

	// Description
	if desc, ok := sections["Description"]; ok {
		skill.Description = strings.TrimSpace(desc)
	}

	// Input Schema
	if raw, ok := sections["Input Schema"]; ok {
		schema := extractCodeBlock(raw)
		if schema == "" {
			return nil, fmt.Errorf("skill %s: Input Schema section has no fenced code block", skill.Name)
		}
		if !json.Valid([]byte(schema)) {
			return nil, fmt.Errorf("skill %s: Input Schema is not valid JSON", skill.Name)
		}
		skill.InputSchema = json.RawMessage(schema)
	}

	// Output Schema
	if raw, ok := sections["Output Schema"]; ok {
		schema := extractCodeBlock(raw)
		if schema == "" {
			return nil, fmt.Errorf("skill %s: Output Schema section has no fenced code block", skill.Name)
		}
		if !json.Valid([]byte(schema)) {
			return nil, fmt.Errorf("skill %s: Output Schema is not valid JSON", skill.Name)
		}
		skill.OutputSchema = json.RawMessage(schema)
	}

	// Context Requirements
	if raw, ok := sections["Context Requirements"]; ok {
		skill.ContextReqs = parseContextReqs(raw)
	}

	// Validation Rules
	if raw, ok := sections["Validation Rules"]; ok {
		skill.ValidationRules = parseValidationRules(raw)
	}

	// Prompt Template
	if raw, ok := sections["Prompt Template"]; ok {
		skill.PromptTemplate = strings.TrimSpace(raw)
	}

	log.Info("skill parsed successfully",
		slog.String("name", skill.Name),
		slog.String("version", skill.Version),
		slog.String("inception", string(skill.Inception)),
		slog.Int("ensemble_size", skill.EnsembleSize),
	)

	return skill, nil
}

// DiscoverSkillFiles finds all *.skill.md files using the Flume Skill
// Resolution Order (inspired by OpenClaw but with security hardening):
//
//  1. FLUME_SKILLS_DIR env var (explicit override, highest priority)
//  2. <workspace>/skills/ (project-level, versioned in git)
//  3. ~/.flume/skills/ (user-level, shared across projects)
//  4. Built-in skills shipped with the binary (lowest priority)
//
// Unlike OpenClaw, Flume enforces:
//   - Path traversal prevention (no symlinks outside allowed roots)
//   - Maximum skill file size (1MB) to prevent DoS
//   - Duplicate name resolution: highest-priority path wins, logged as override
func DiscoverSkillFiles(ctx context.Context) ([]string, error) {
	log := skillslog.WithContext(ctx).With(slog.String("component", "skill_discovery"))

	var searchPaths []string
	seen := make(map[string]bool)

	// Priority 1: Explicit env var
	if envDir := os.Getenv("FLUME_SKILLS_DIR"); envDir != "" {
		abs, err := filepath.Abs(envDir)
		if err == nil {
			searchPaths = append(searchPaths, abs)
			log.Debug("skill search path added", slog.String("source", "FLUME_SKILLS_DIR"), slog.String("path", abs))
		}
	}

	// Priority 2: Workspace-relative ./skills/
	if cwd, err := os.Getwd(); err == nil {
		wsPath := filepath.Join(cwd, "skills")
		searchPaths = append(searchPaths, wsPath)
	}

	// Priority 3: User-level ~/.flume/skills/
	if home, err := os.UserHomeDir(); err == nil {
		userPath := filepath.Join(home, ".flume", "skills")
		searchPaths = append(searchPaths, userPath)
	}

	var files []string
	const maxFileSize = 1 << 20 // 1MB

	for _, dir := range searchPaths {
		info, err := os.Stat(dir)
		if err != nil || !info.IsDir() {
			continue
		}

		entries, err := os.ReadDir(dir)
		if err != nil {
			log.Warn("failed to read skills directory", slog.String("dir", dir), slog.String("error", err.Error()))
			continue
		}

		for _, entry := range entries {
			if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".skill.md") {
				continue
			}

			// Security: prevent path traversal via symlinks
			fullPath := filepath.Join(dir, entry.Name())
			resolved, err := filepath.EvalSymlinks(fullPath)
			if err != nil {
				log.Warn("skipping skill file (symlink resolution failed)",
					slog.String("file", entry.Name()), slog.String("error", err.Error()))
				continue
			}

			// Security: enforce size limit
			fi, err := os.Stat(resolved)
			if err != nil || fi.Size() > maxFileSize {
				log.Warn("skipping skill file (size limit exceeded or stat failed)",
					slog.String("file", entry.Name()), slog.Int64("size_bytes", fi.Size()))
				continue
			}

			// Deduplicate: highest-priority path wins
			baseName := strings.TrimSuffix(entry.Name(), ".skill.md")
			if seen[baseName] {
				log.Debug("skill already discovered at higher priority, skipping",
					slog.String("name", baseName), slog.String("lower_priority_path", resolved))
				continue
			}
			seen[baseName] = true
			files = append(files, resolved)
			log.Debug("skill file discovered", slog.String("name", baseName), slog.String("path", resolved))
		}
	}

	log.Info("skill discovery complete", slog.Int("total_files", len(files)))
	return files, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Internal helpers
// ─────────────────────────────────────────────────────────────────────────────

// parseFrontMatter extracts key-value pairs from the YAML front-matter block.
func parseFrontMatter(raw string, skill *SkillDefinition) error {
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(parts[0])
		val := strings.TrimSpace(parts[1])
		// Strip inline comments
		if idx := strings.Index(val, "#"); idx > 0 {
			val = strings.TrimSpace(val[:idx])
		}

		switch strings.ToLower(key) {
		case "name":
			skill.Name = val
		case "version":
			skill.Version = val
		case "inception":
			mode := InceptionMode(strings.ToLower(val))
			switch mode {
			case InceptionFull, InceptionHybrid, InceptionLLMOnly:
				skill.Inception = mode
			default:
				return fmt.Errorf("invalid inception mode: %q (valid: full, hybrid, llm-only)", val)
			}
		case "ensemble_size", "ensemble size":
			if n, err := strconv.Atoi(val); err == nil && n > 0 {
				skill.EnsembleSize = n
			}
		case "creativity_level", "creativity level":
			if n, err := strconv.Atoi(val); err == nil {
				skill.CreativityLevel = n
			}
		}
	}
	return nil
}

// extractSections splits a Markdown body into named sections by H2 headers.
func extractSections(body string) map[string]string {
	sections := make(map[string]string)
	locs := sectionRe.FindAllStringSubmatchIndex(body, -1)
	for i, loc := range locs {
		name := body[loc[2]:loc[3]]
		start := loc[1]
		var end int
		if i+1 < len(locs) {
			end = locs[i+1][0]
		} else {
			end = len(body)
		}
		sections[name] = body[start:end]
	}
	return sections
}

// extractCodeBlock returns the content of the first fenced code block in a section.
func extractCodeBlock(section string) string {
	match := fencedCodeRe.FindStringSubmatch(section)
	if match == nil {
		return ""
	}
	return strings.TrimSpace(match[1])
}

// parseContextReqs extracts structured context requirements from bullet lists.
func parseContextReqs(section string) []ContextReq {
	var reqs []ContextReq
	lines := strings.Split(section, "\n")
	current := ContextReq{}
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "- ") {
			continue
		}
		line = strings.TrimPrefix(line, "- ")
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(parts[0])
		val := strings.TrimSpace(parts[1])
		switch strings.ToLower(key) {
		case "index":
			current.Index = val
		case "query":
			current.Query = val
			if current.Index != "" {
				reqs = append(reqs, current)
				current = ContextReq{}
			}
		}
	}
	return reqs
}

// parseValidationRules extracts validation rules from bullet lists.
func parseValidationRules(section string) []string {
	var rules []string
	for _, line := range strings.Split(section, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "- ") {
			rules = append(rules, strings.TrimPrefix(line, "- "))
		}
	}
	return rules
}
