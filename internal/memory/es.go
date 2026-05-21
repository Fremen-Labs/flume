// Package memory provides Elasticsearch-backed agent memory operations.
//
// Direct port of Python: memory/es/scripts/*.py (6 AST nodes).
// Provides CRUD for agent memory entries (decisions, lessons, constraints)
// and task/review metadata stored in the agent-memory-entries index.
//
// This eliminates the need for separate Python scripts called via subprocess.
package memory

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// Default index name — mirrors Python: ES_INDEX_MEMORY env var.
const DefaultMemoryIndex = "agent-memory-entries"

// MemoryEntry represents a single agent memory entry.
// Derived from Python: write_memory.py write_memory_entry() parameters.
type MemoryEntry struct {
	ID         string   `json:"id"`
	Title      string   `json:"title"`
	Content    string   `json:"content"`
	Type       string   `json:"type"`
	Project    string   `json:"project"`
	Repo       string   `json:"repo"`
	Scope      string   `json:"scope,omitempty"`
	Confidence float64  `json:"confidence"`
	Status     string   `json:"status"`
	Source     string   `json:"source"`
	Tags       []string `json:"tags"`
	CreatedAt  string   `json:"created_at"`
	UpdatedAt  string   `json:"updated_at"`
}

// Store provides agent memory operations via Elasticsearch.
type Store struct {
	esURL      string
	esAPIKey   string
	index      string
	httpClient *http.Client
	logger     *slog.Logger
}

// NewStore creates a new memory store.
func NewStore(logger *slog.Logger) *Store {
	if logger == nil {
		logger = slog.Default()
	}
	esURL := os.Getenv("ES_URL")
	if esURL == "" {
		esURL = "https://localhost:9200"
	}
	index := os.Getenv("ES_INDEX_MEMORY")
	if index == "" {
		index = DefaultMemoryIndex
	}

	verifyTLS := strings.ToLower(os.Getenv("ES_VERIFY_TLS")) == "true"

	return &Store{
		esURL:    strings.TrimRight(esURL, "/"),
		esAPIKey: os.Getenv("ES_API_KEY"),
		index:    index,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: !verifyTLS}, //nolint:gosec
			},
		},
		logger: logger,
	}
}

// WriteEntry writes a memory entry to Elasticsearch.
// Derived from Python: write_memory.py write_memory_entry().
func (s *Store) WriteEntry(ctx context.Context, entry *MemoryEntry) error {
	now := nowISO()
	if entry.CreatedAt == "" {
		entry.CreatedAt = now
	}
	entry.UpdatedAt = now
	if entry.Confidence == 0 {
		entry.Confidence = 0.5
	}
	if entry.Status == "" {
		entry.Status = "active"
	}
	if entry.Source == "" {
		entry.Source = "agent"
	}
	if entry.Tags == nil {
		entry.Tags = []string{}
	}

	path := fmt.Sprintf("/%s/_doc/%s", s.index, entry.ID)
	_, err := s.request(ctx, http.MethodPost, path, entry)
	if err != nil {
		s.logger.Error("failed to write memory entry",
			slog.String("id", entry.ID),
			slog.String("title", entry.Title),
			slog.String("error", err.Error()))
		return err
	}
	s.logger.Debug("memory entry written", slog.String("id", entry.ID), slog.String("title", entry.Title))
	return nil
}

// ReadEntry reads a memory entry by ID.
// Derived from Python: read_memory.py read_memory_entry().
func (s *Store) ReadEntry(ctx context.Context, entryID string) (*MemoryEntry, error) {
	path := fmt.Sprintf("/%s/_doc/%s", s.index, entryID)
	result, err := s.request(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	if result == nil {
		return nil, nil // not found
	}

	found, _ := result["found"].(bool)
	if !found {
		return nil, nil
	}

	src, _ := result["_source"].(map[string]interface{})
	if src == nil {
		return nil, nil
	}

	return mapToEntry(src), nil
}

// SearchEntries searches memory entries by query string.
// Derived from Python: search_memory.py search_memory_entries().
func (s *Store) SearchEntries(ctx context.Context, query string, maxResults int) ([]*MemoryEntry, error) {
	if maxResults == 0 {
		maxResults = 20
	}

	body := map[string]interface{}{
		"size": maxResults,
		"query": map[string]interface{}{
			"multi_match": map[string]interface{}{
				"query":  query,
				"fields": []string{"title^3", "content^2", "tags", "project", "repo"},
			},
		},
		"sort": []map[string]interface{}{
			{"_score": map[string]string{"order": "desc"}},
			{"updated_at": map[string]string{"order": "desc"}},
		},
	}

	path := fmt.Sprintf("/%s/_search", s.index)
	result, err := s.request(ctx, http.MethodPost, path, body)
	if err != nil {
		return nil, err
	}

	hits, _ := result["hits"].(map[string]interface{})
	hitArr, _ := hits["hits"].([]interface{})

	var entries []*MemoryEntry
	for _, h := range hitArr {
		hm, _ := h.(map[string]interface{})
		src, _ := hm["_source"].(map[string]interface{})
		if src != nil {
			entries = append(entries, mapToEntry(src))
		}
	}
	return entries, nil
}

// DeleteEntry deletes a memory entry by ID.
// Derived from Python: delete_memory.py delete_memory_entry().
func (s *Store) DeleteEntry(ctx context.Context, entryID string) error {
	path := fmt.Sprintf("/%s/_doc/%s", s.index, entryID)
	_, err := s.request(ctx, http.MethodDelete, path, nil)
	return err
}

// ListByProject returns all entries for a project.
// Derived from Python: list_memory.py list_memory_entries().
func (s *Store) ListByProject(ctx context.Context, project string, maxResults int) ([]*MemoryEntry, error) {
	if maxResults == 0 {
		maxResults = 50
	}

	body := map[string]interface{}{
		"size": maxResults,
		"query": map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []map[string]interface{}{
					{"term": map[string]string{"project": project}},
					{"term": map[string]string{"status": "active"}},
				},
			},
		},
		"sort": []map[string]interface{}{
			{"updated_at": map[string]string{"order": "desc"}},
		},
	}

	path := fmt.Sprintf("/%s/_search", s.index)
	result, err := s.request(ctx, http.MethodPost, path, body)
	if err != nil {
		return nil, err
	}

	hits, _ := result["hits"].(map[string]interface{})
	hitArr, _ := hits["hits"].([]interface{})

	var entries []*MemoryEntry
	for _, h := range hitArr {
		hm, _ := h.(map[string]interface{})
		src, _ := hm["_source"].(map[string]interface{})
		if src != nil {
			entries = append(entries, mapToEntry(src))
		}
	}
	return entries, nil
}

// WriteTask writes a task tracking entry.
// Derived from Python: write_task.py write_task_entry().
func (s *Store) WriteTask(ctx context.Context, taskID, title, description, project, repo, status string, priority int) error {
	entry := &MemoryEntry{
		ID:         taskID,
		Title:      title,
		Content:    description,
		Type:       "task",
		Project:    project,
		Repo:       repo,
		Confidence: float64(priority) / 10.0,
		Status:     status,
		Source:      "agent",
		Tags:       []string{"task"},
	}
	return s.WriteEntry(ctx, entry)
}

// WriteReview writes a code review memory entry.
// Derived from Python: write_review.py write_review_entry().
func (s *Store) WriteReview(ctx context.Context, reviewID, title, content, project, repo string, tags []string) error {
	entry := &MemoryEntry{
		ID:         reviewID,
		Title:      title,
		Content:    content,
		Type:       "review",
		Project:    project,
		Repo:       repo,
		Confidence: 0.8,
		Status:     "active",
		Source:      "agent",
		Tags:       tags,
	}
	return s.WriteEntry(ctx, entry)
}

// ─── Internal ───────────────────────────────────────────────────────────────

func (s *Store) request(ctx context.Context, method, path string, body interface{}) (map[string]interface{}, error) {
	url := s.esURL + path
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("memory: marshal failed: %w", err)
		}
		bodyReader = strings.NewReader(string(data))
	}

	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("memory: request build failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if s.esAPIKey != "" {
		req.Header.Set("Authorization", "ApiKey "+s.esAPIKey)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 404 {
		return nil, nil
	}
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("memory: %s %s returned HTTP %d: %s",
			method, path, resp.StatusCode, truncate(string(respBody), 200))
	}

	if len(respBody) == 0 {
		return map[string]interface{}{}, nil
	}

	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("memory: decode failed: %w", err)
	}
	return result, nil
}

func nowISO() string {
	return time.Now().UTC().Format(time.RFC3339)
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen]
}

func mapToEntry(src map[string]interface{}) *MemoryEntry {
	e := &MemoryEntry{
		ID:         sval(src["id"]),
		Title:      sval(src["title"]),
		Content:    sval(src["content"]),
		Type:       sval(src["type"]),
		Project:    sval(src["project"]),
		Repo:       sval(src["repo"]),
		Scope:      sval(src["scope"]),
		Status:     sval(src["status"]),
		Source:      sval(src["source"]),
		CreatedAt:  sval(src["created_at"]),
		UpdatedAt:  sval(src["updated_at"]),
	}
	if conf, ok := src["confidence"].(float64); ok {
		e.Confidence = conf
	}
	if tags, ok := src["tags"].([]interface{}); ok {
		for _, t := range tags {
			if ts, ok := t.(string); ok {
				e.Tags = append(e.Tags, ts)
			}
		}
	}
	return e
}

func sval(v interface{}) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}
