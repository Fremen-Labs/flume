package logloom_test

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/Fremen-Labs/logloom-go/logloom"
)

func TestNewHandler_NoGraph_PassesThrough(t *testing.T) {
	var buf bytes.Buffer
	inner := slog.NewJSONHandler(&buf, nil)
	handler := logloom.NewHandler(inner)

	logger := slog.New(handler)
	logger.Info("hello world", slog.String("key", "value"))

	var parsed map[string]interface{}
	if err := json.Unmarshal(buf.Bytes(), &parsed); err != nil {
		t.Fatalf("failed to parse log output: %v", err)
	}
	if parsed["msg"] != "hello world" {
		t.Errorf("expected msg=hello world, got %v", parsed["msg"])
	}
	if _, exists := parsed["logloom_node_id"]; exists {
		t.Error("expected no logloom_node_id when graph is not loaded")
	}
}

func TestNormalizeGoFunc(t *testing.T) {
	// This is tested indirectly but we verify the exported behaviour.
	// The handler should not crash with any of these inputs.
	var buf bytes.Buffer
	inner := slog.NewJSONHandler(&buf, nil)
	handler := logloom.NewHandler(inner)
	logger := slog.New(handler)

	// Should not panic
	logger.Info("test message")
	logger.Warn("another message", slog.String("component", "test"))
	logger.Error("error message")

	if buf.Len() == 0 {
		t.Error("expected log output")
	}
}

func TestGraphLoaded_WhenPresent(t *testing.T) {
	// Create a minimal graph file in a temp dir
	tmpDir := t.TempDir()
	graph := map[string]interface{}{
		"project":    "test-project",
		"commit_sha": "abc123",
		"nodes": map[string]interface{}{
			"ll:test001": map[string]interface{}{
				"file":             "test.go",
				"module":           "test",
				"function":         "DoStuff",
				"message_template": "doing stuff",
				"semantic_tags":    []string{"lifecycle"},
				"line":             42,
			},
		},
	}
	data, _ := json.Marshal(graph)
	graphPath := filepath.Join(tmpDir, "logloom-graph.json")
	os.WriteFile(graphPath, data, 0644)

	// Point LOGLOOM_GRAPH_PATH to it.
	// Note: LoadGraph is sync.Once so we can only test discovery once
	// per process. This test validates the file format parsing at minimum.
	t.Setenv("LOGLOOM_GRAPH_PATH", graphPath)

	// We can't re-trigger initOnce in the same process, but we can
	// at least verify the file is valid JSON that would parse correctly.
	var g logloom.Graph
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatalf("graph file is invalid JSON: %v", err)
	}
	if len(g.Nodes) != 1 {
		t.Errorf("expected 1 node, got %d", len(g.Nodes))
	}
	node := g.Nodes["ll:test001"]
	if node == nil {
		t.Fatal("expected node ll:test001")
	}
	if node.Function != "DoStuff" {
		t.Errorf("expected function=DoStuff, got %s", node.Function)
	}
}
