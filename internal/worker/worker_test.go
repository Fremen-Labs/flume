package worker

import (
	"log/slog"
	"os"
	"testing"

	"github.com/Fremen-Labs/flume/internal/config"
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
	c := NewClaimer(nil, logger, "test")

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
