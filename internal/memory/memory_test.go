package memory

import (
	"encoding/json"
	"testing"
	"time"
)

func TestNowISO(t *testing.T) {
	iso := nowISO()
	_, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		t.Errorf("nowISO() returned invalid RFC3339: %s — %v", iso, err)
	}
}

func TestMapToEntry(t *testing.T) {
	src := map[string]interface{}{
		"id":         "test-123",
		"title":      "Test Entry",
		"content":    "This is test content",
		"type":       "decision",
		"project":    "flume",
		"repo":       "flume",
		"confidence": float64(0.9),
		"status":     "active",
		"source":     "agent",
		"tags":       []interface{}{"test", "unit"},
		"created_at": "2026-05-21T00:00:00Z",
		"updated_at": "2026-05-21T00:00:00Z",
	}

	entry := mapToEntry(src)
	if entry.ID != "test-123" {
		t.Errorf("expected ID 'test-123', got %q", entry.ID)
	}
	if entry.Title != "Test Entry" {
		t.Errorf("expected Title 'Test Entry', got %q", entry.Title)
	}
	if entry.Confidence != 0.9 {
		t.Errorf("expected Confidence 0.9, got %f", entry.Confidence)
	}
	if len(entry.Tags) != 2 {
		t.Fatalf("expected 2 tags, got %d", len(entry.Tags))
	}
	if entry.Tags[0] != "test" || entry.Tags[1] != "unit" {
		t.Errorf("unexpected tags: %v", entry.Tags)
	}
}

func TestMemoryEntryJSON(t *testing.T) {
	entry := &MemoryEntry{
		ID:         "json-test",
		Title:      "JSON Test",
		Content:    "test",
		Type:       "lesson",
		Project:    "flume",
		Repo:       "flume",
		Confidence: 0.75,
		Status:     "active",
		Source:     "agent",
		Tags:       []string{"json"},
		CreatedAt:  "2026-05-21T00:00:00Z",
		UpdatedAt:  "2026-05-21T00:00:00Z",
	}

	data, err := json.Marshal(entry)
	if err != nil {
		t.Fatalf("Marshal failed: %v", err)
	}

	var decoded MemoryEntry
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal failed: %v", err)
	}
	if decoded.ID != "json-test" {
		t.Errorf("expected ID 'json-test', got %q", decoded.ID)
	}
	if decoded.Confidence != 0.75 {
		t.Errorf("expected Confidence 0.75, got %f", decoded.Confidence)
	}
}

func TestNewStore(t *testing.T) {
	store := NewStore(nil)
	if store == nil {
		t.Fatal("NewStore returned nil")
	}
	if store.index != DefaultMemoryIndex {
		t.Errorf("expected default index %q, got %q", DefaultMemoryIndex, store.index)
	}
}

func TestTruncate(t *testing.T) {
	if truncate("hello world", 5) != "hello" {
		t.Error("truncate should truncate long strings")
	}
	if truncate("hi", 10) != "hi" {
		t.Error("truncate should not truncate short strings")
	}
}
