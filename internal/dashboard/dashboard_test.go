package dashboard

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHealthEndpoint(t *testing.T) {
	cfg := DefaultConfig()
	srv := New(cfg, nil)
	if srv == nil {
		t.Fatal("New returned nil")
	}

	// Use the mux directly to test routes
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/health", nil)
	srv.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var resp map[string]interface{}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	if resp["status"] != "ok" {
		t.Errorf("expected status=ok, got %v", resp["status"])
	}
}

func TestTaskTransitionValidation(t *testing.T) {
	cfg := DefaultConfig()
	srv := New(cfg, nil)

	// Test invalid status
	body := `{"status": "invalid_status"}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/test-1/transition", strings.NewReader(body))
	srv.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}

	var resp map[string]interface{}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}

	if _, ok := resp["error"]; !ok {
		t.Error("expected error field in response")
	}
}

func TestBulkRequeueValidation(t *testing.T) {
	cfg := DefaultConfig()
	srv := New(cfg, nil)

	// Empty task_ids
	body := `{"task_ids": []}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/bulk-requeue", strings.NewReader(body))
	srv.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestBulkUpdateValidation(t *testing.T) {
	cfg := DefaultConfig()
	srv := New(cfg, nil)

	// Invalid action
	body := `{"ids": ["task-1"], "action": "invalid"}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/bulk-update", strings.NewReader(body))
	srv.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}

	// Empty ids
	body = `{"ids": [], "action": "archive"}`
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/api/tasks/bulk-update", strings.NewReader(body))
	srv.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for empty ids, got %d", rec.Code)
	}
}

func TestCORSHeaders(t *testing.T) {
	cfg := DefaultConfig()
	srv := New(cfg, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodOptions, "/api/health", nil)
	req.Header.Set("Origin", "http://localhost:8080")
	srv.withMiddleware(srv.mux).ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204 for OPTIONS, got %d", rec.Code)
	}

	if rec.Header().Get("Access-Control-Allow-Origin") != "http://localhost:8080" {
		t.Error("expected CORS origin header")
	}
}

func TestCORSRejectsUnknownOrigin(t *testing.T) {
	cfg := DefaultConfig()
	srv := New(cfg, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/health", nil)
	req.Header.Set("Origin", "http://evil.com")
	srv.withMiddleware(srv.mux).ServeHTTP(rec, req)

	if rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Error("should not set CORS for unknown origin")
	}
}

func TestEnvHelpers(t *testing.T) {
	if envOr("DEFINITELY_NOT_SET_12345", "fallback") != "fallback" {
		t.Error("envOr fallback failed")
	}

	if envInt("DEFINITELY_NOT_SET_12345", 42) != 42 {
		t.Error("envInt fallback failed")
	}
}

func TestStringHelpers(t *testing.T) {
	if str(nil) != "" {
		t.Error("str(nil) should be empty")
	}
	if str("hello") != "hello" {
		t.Error("str(hello) should be hello")
	}
	if orStr("", "b") != "b" {
		t.Error("orStr empty should return b")
	}
	if orStr("a", "b") != "a" {
		t.Error("orStr non-empty should return a")
	}
	if truncate("hello world", 5) != "hello" {
		t.Error("truncate should work")
	}
}
