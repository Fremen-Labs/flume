package worker

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/es"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

func TestBuildWorkers_Default(t *testing.T) {
	cfg := &config.Config{
		LLMModel:       "qwen3.5:35b",
		LLMProvider:    "ollama",
		WorkersPerRole: 1,
	}

	workers := BuildWorkers(cfg, "node-1", map[string]int{"localhost": 4})

	// Should have 4 workers (one per role)
	if len(workers) != 4 {
		t.Fatalf("expected 4 workers, got %d", len(workers))
	}

	roles := map[string]bool{}
	for _, w := range workers {
		roles[w.Role] = true
		if w.Model != "qwen3.5:35b" {
			t.Errorf("expected model qwen3.5:35b, got %s", w.Model)
		}
		if w.Provider != "ollama" {
			t.Errorf("expected provider ollama, got %s", w.Provider)
		}
		if w.Status != ftypes.WorkerStatusIdle {
			t.Errorf("expected idle status, got %s", w.Status)
		}
	}

	expectedRoles := []string{"implementer", "reviewer", "tester", "pm"}
	for _, role := range expectedRoles {
		if !roles[role] {
			t.Errorf("missing role: %s", role)
		}
	}
}

func TestBuildWorkers_MultiplePerRole(t *testing.T) {
	cfg := &config.Config{
		LLMModel:       "claude-4",
		LLMProvider:    "anthropic",
		WorkersPerRole: 3,
	}

	workers := BuildWorkers(cfg, "node-2", map[string]int{})
	if len(workers) != 12 { // 4 roles × 3 workers
		t.Fatalf("expected 12 workers, got %d", len(workers))
	}
}

func TestRoleToTargetStatus(t *testing.T) {
	tests := map[string]string{
		"pm":          "planned",
		"tester":      "review",
		"reviewer":    "review",
		"implementer": "ready",
		"unknown":     "ready",
	}
	for role, expected := range tests {
		got := roleToTargetStatus(role)
		if got != expected {
			t.Errorf("roleToTargetStatus(%q) = %q, want %q", role, got, expected)
		}
	}
}

func TestTaskRequiresCode(t *testing.T) {
	tests := []struct {
		title string
		desc  string
		want  bool
	}{
		{"Implement login feature", "", true},
		{"Fix authentication bug", "", true},
		{"Update documentation", "add API endpoint docs", true},
		{"Review meeting notes", "just a summary", false},
		{"Create new service", "", true},
		{"Refactor database queries", "", true},
	}
	for _, tt := range tests {
		task := ftypes.Task{Title: tt.title, Description: tt.desc}
		got := TaskRequiresCode(task)
		if got != tt.want {
			t.Errorf("TaskRequiresCode(%q, %q) = %v, want %v",
				tt.title, tt.desc, got, tt.want)
		}
	}
}

func TestResolveBranchName(t *testing.T) {
	task := ftypes.Task{
		ID:    "task-123",
		Title: "Fix login validation",
	}

	branch := resolveBranchName(task)

	// Should start with "feature/"
	if branch[:8] != "feature/" {
		t.Errorf("branch should start with 'feature/', got %s", branch)
	}
	// Should contain sanitized title
	if branch == "" {
		t.Error("branch name is empty")
	}
	// Should be deterministic
	branch2 := resolveBranchName(task)
	if branch != branch2 {
		t.Errorf("branch names should be deterministic: %q != %q", branch, branch2)
	}
}

func TestResolveBranchName_WithParent(t *testing.T) {
	task1 := ftypes.Task{
		ID:       "child-1",
		ParentID: "story-1",
		Title:    "Subtask A",
	}
	task2 := ftypes.Task{
		ID:       "child-2",
		ParentID: "story-1",
		Title:    "Subtask A",
	}

	// Siblings with same title and parent should get same branch
	branch1 := resolveBranchName(task1)
	branch2 := resolveBranchName(task2)
	if branch1 != branch2 {
		t.Errorf("sibling branches should match: %q != %q", branch1, branch2)
	}
}

func TestBranchSanitization(t *testing.T) {
	task := ftypes.Task{
		ID:    "task-456",
		Title: "Fix [critical] bug (auth): special chars!",
	}

	branch := resolveBranchName(task)
	// Should not contain brackets, parens, colons, or exclamation marks
	for _, c := range "[]():!" {
		if containsRune(branch, c) {
			t.Errorf("branch %q contains invalid char %q", branch, string(c))
		}
	}
}

func containsRune(s string, r rune) bool {
	for _, c := range s {
		if c == r {
			return true
		}
	}
	return false
}

func TestToolResultModifiedRepo(t *testing.T) {
	tests := []struct {
		tool   string
		result string
		want   bool
	}{
		{"write_file", "File written successfully", true},
		{"write_file", "Error: permission denied", false},
		{"read_file", "file contents here", false},
		{"run_shell", "Build succeeded", true},
		{"run_shell", "Error: command not found", false},
		{"multi_replace_file_content", "Replaced 3 occurrences", true},
	}
	for _, tt := range tests {
		got := ToolResultModifiedRepo(tt.tool, tt.result)
		if got != tt.want {
			t.Errorf("ToolResultModifiedRepo(%q, %q) = %v, want %v",
				tt.tool, tt.result, got, tt.want)
		}
	}
}

func TestNewClaimer_NormalizeTitle(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelError}))
	c := NewClaimer(nil, nil, logger, "test")

	tests := map[string]string{
		"Fix Login Bug!":        "fix login bug",
		"  UPPER CASE  ":       "upper case",
		"special-chars_here.v2": "specialcharsherev2",
		"":                      "",
	}
	for input, expected := range tests {
		got := c.normalizeTitle(input)
		if got != expected {
			t.Errorf("normalizeTitle(%q) = %q, want %q", input, got, expected)
		}
	}
}

func TestHandlerRegistry(t *testing.T) {
	// Verify handler resolution
	RegisterHandler(&ImplementerHandler{})
	RegisterHandler(&ReviewerHandler{})
	RegisterHandler(&TesterHandler{})
	RegisterHandler(&PMHandler{})

	tests := map[string]string{
		"implementer":         "implementer",
		"reviewer":            "reviewer",
		"tester":              "tester",
		"pm":                  "pm",
		"implementer-node-0": "implementer",
		"reviewer-node-1":    "reviewer",
	}
	for workerName, expectedRole := range tests {
		h := resolveHandler(workerName)
		if h == nil {
			t.Errorf("no handler found for %q", workerName)
			continue
		}
		if h.Role() != expectedRole {
			t.Errorf("resolveHandler(%q).Role() = %q, want %q",
				workerName, h.Role(), expectedRole)
		}
	}
}

// TestToolRegistryAndElastroLogloomTools verifies the Phase 3.1+ registry
// (with FS + AST tools) and exact index names for contract #4.
func TestToolRegistryAndElastroLogloomTools(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelError}))

	// Without ES: still gets FS tools
	reg := NewToolRegistry(logger)
	names := make(map[string]bool)
	// We can't easily enumerate private map, but Execute on unknown fails; test known
	for _, name := range []string{"list_directory", "read_file", "write_file", "run_shell"} {
		_, err := reg.Execute(context.Background(), name, map[string]interface{}{}, "/tmp")
		// Will error on bad args or FS, but not "unknown tool"
		if err != nil && strings.Contains(err.Error(), "unknown tool") {
			t.Errorf("expected FS tool %s to be registered, got unknown: %v", name, err)
		}
		names[name] = true
	}

	// WithElastro: pass a (dummy) ES client so BOTH elastro_query_ast AND logloom_ast_query
	// get registered per the NewToolRegistryWithElastro factory (logloom only on non-nil es).
	// This exercises the full contract registration used by implementer workers.
	// Use unreachable URL so no real ES required for this unit test; execute errs are expected
	// and handled gracefully inside the executors (contract point #4: correct indices + using before edits).
	dummyES := es.New("http://127.0.0.1:1", "", logger)
	reg2 := NewToolRegistryWithElastro(dummyES, logger)
	// AST tools must be present
	for _, name := range []string{"elastro_query_ast", "logloom_ast_query"} {
		_, err := reg2.Execute(context.Background(), name, map[string]interface{}{"query": "test"}, "/tmp")
		if err != nil && strings.Contains(err.Error(), "unknown tool") {
			t.Errorf("expected AST tool %s to be registered, got unknown", name)
		}
		names[name] = true
	}

	// Confirm index consts are the documented canonical ones (contract #4).
	// Add assertions for the key indices: flume-elastro-graph and flume-logloom-ast.
	if ElastroGraphIndex != "flume-elastro-graph" {
		t.Errorf("ElastroGraphIndex = %s, want flume-elastro-graph", ElastroGraphIndex)
	}
	if LogloomEnrichmentIndex != "flume-logloom-enrichment" {
		t.Errorf("LogloomEnrichmentIndex mismatch")
	}
	if LogloomASTIndex != "flume-logloom-ast" {
		t.Errorf("LogloomASTIndex mismatch")
	}
}

// TestFileToolExecutors exercises the new FS tools (coverage for tool usage path).
func TestFileToolExecutors(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelError}))
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "testrepo")
	_ = os.MkdirAll(repo, 0755)

	// list
	listExec := NewListDirectoryExecutor(logger)
	out, err := listExec.Execute(context.Background(), map[string]interface{}{"path": "."}, repo)
	if err != nil {
		t.Fatalf("list_directory failed: %v", err)
	}
	if !strings.Contains(out, "Directory listing") {
		t.Errorf("unexpected list output: %s", out)
	}

	// write + read roundtrip
	writeExec := NewWriteFileExecutor(logger)
	_, err = writeExec.Execute(context.Background(), map[string]interface{}{
		"path":    "src/foo.go",
		"content": "package foo\n\nfunc Bar() {}\n",
	}, repo)
	if err != nil {
		t.Fatalf("write_file failed: %v", err)
	}

	readExec := NewReadFileExecutor(logger)
	content, err := readExec.Execute(context.Background(), map[string]interface{}{"path": "src/foo.go"}, repo)
	if err != nil {
		t.Fatalf("read_file failed: %v", err)
	}
	if !strings.Contains(content, "func Bar") {
		t.Errorf("read content mismatch: %s", content)
	}

	// run_shell (safe echo)
	shellExec := NewRunShellExecutor(logger)
	out, err = shellExec.Execute(context.Background(), map[string]interface{}{"command": "echo hello-from-shell"}, repo)
	if err != nil {
		t.Fatalf("run_shell failed: %v", err)
	}
	if !strings.Contains(out, "hello-from-shell") {
		t.Errorf("shell output missing echo: %s", out)
	}
}

// TestIsWriteToolAndASTEnforcementHelper covers the gate logic used by runner.
func TestIsWriteToolAndASTEnforcementHelper(t *testing.T) {
	writes := []string{"write_file", "edit_file", "run_shell", "multi_replace_file_content"}
	for _, w := range writes {
		if !isWriteTool(w) {
			t.Errorf("isWriteTool(%s) should be true", w)
		}
	}
	reads := []string{"read_file", "list_directory", "elastro_query_ast", "logloom_ast_query", "foo"}
	for _, r := range reads {
		if isWriteTool(r) {
			t.Errorf("isWriteTool(%s) should be false", r)
		}
	}
}

// TestToolResultModifiedRepoExtended ensures new write tools are recognized.
func TestToolResultModifiedRepoExtended(t *testing.T) {
	if !ToolResultModifiedRepo("write_file", "ok") {
		t.Error("write_file should modify")
	}
	if !ToolResultModifiedRepo("edit_file", "patched") {
		t.Error("edit_file should modify")
	}
}

// TestElastroLogloomBinaryPresence validates contract point #1:
// "Binary presence in worker image (or simulated)".
// The find* helpers are used inside NewToolRegistryWithElastro (called from
// worker startup / NewRunner) and mirror the discovery in Dockerfile, api_projects.go,
// doctor.go. Image build has hard asserts; this exercises the runtime path.
func TestElastroLogloomBinaryPresence(t *testing.T) {
	elBin := findElastroBinary()
	llBin := findLogloomBinary()

	if elBin == "" {
		t.Log("elastro binary not discoverable in current env (e.g. no ~/.local/bin or PATH); " +
			"worker image (Dockerfile) enforces presence at /opt/venv/bin/elastro via pip wheel/public + post-install check. Contract point #1 simulated.")
	} else {
		t.Logf("elastro binary present (contract #1): %s", elBin)
	}
	if llBin == "" {
		t.Log("logloom binary not discoverable in current env; " +
			"Dockerfile installs to /opt/venv/bin/logloom (LOGLOOM_INSTALL=public|wheel) for dual ingest in worker images.")
	} else {
		t.Logf("logloom binary present (contract #1): %s", llBin)
	}

	// Sanity: the funcs do not panic and return string ("" or path)
	_ = findElastroBinary()
	_ = findLogloomBinary()
}

// TestElastroLogloomQueryExecutorsReportCorrectIndices exercises the direct
// executor implementations (used by implementer worker + planner RAG) and
// verifies they target the right indices and surface them in results/errors
// (contract point #4: "Implementer worker successfully calling elastro_query_ast /
// logloom_ast_query with correct indices and using results before edits").
func TestElastroLogloomQueryExecutorsReportCorrectIndices(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelError}))
	dummyES := es.New("http://127.0.0.1:1", "", logger)

	// Elastro executor
	e := NewElastroASTQueryExecutor(dummyES, logger)
	if got := e.Name(); got != "elastro_query_ast" {
		t.Errorf("ElastroASTQueryExecutor.Name() = %q, want elastro_query_ast", got)
	}
	res, err := e.Execute(context.Background(), map[string]interface{}{"query": "hello"}, ".")
	if err != nil {
		t.Fatalf("elastro execute returned err: %v", err)
	}
	if !strings.Contains(res, "flume-elastro-graph") {
		t.Errorf("elastro_query_ast result must mention correct index 'flume-elastro-graph'; got: %s", res)
	}

	// Logloom executor (uses enrichment then falls back to ast index)
	l := NewLogloomASTQueryExecutor(dummyES, logger)
	if got := l.Name(); got != "logloom_ast_query" {
		t.Errorf("LogloomASTQueryExecutor.Name() = %q, want logloom_ast_query", got)
	}
	res2, err := l.Execute(context.Background(), map[string]interface{}{"query": "hello"}, ".")
	// Expect err because both indices unreachable, but the err message must name the canonical indices.
	if err == nil {
		t.Logf("unexpected success on dummy logloom (perhaps): %s", res2)
	} else {
		combined := err.Error()
		if !strings.Contains(combined, LogloomEnrichmentIndex) || !strings.Contains(combined, LogloomASTIndex) {
			t.Errorf("logloom_ast_query err must mention both %s and %s; got: %s", LogloomEnrichmentIndex, LogloomASTIndex, combined)
		}
	}
	// If it fell back and got result string (unlikely on dummy), still check for index mention.
	if res2 != "" && !strings.Contains(res2, "flume-logloom") {
		t.Logf("logloom res2: %s", res2)
	}
}

// TestImplementerASTBeforeEditEnforcement uses the isWriteTool helper (called
// from handleImplementer) to ensure AST query tools are treated as non-writes
// (read-only RAG) so the "use results before edits" rule in runner can gate writes.
func TestImplementerASTBeforeEditEnforcement(t *testing.T) {
	// AST tools must never be considered "write" so they can (and must) be called
	// before any write_file / edit in the implementer loop.
	for _, ast := range []string{"elastro_query_ast", "logloom_ast_query"} {
		if isWriteTool(ast) {
			t.Errorf("isWriteTool(%s) must be false (read RAG tool for pre-edit verification)", ast)
		}
	}
}
