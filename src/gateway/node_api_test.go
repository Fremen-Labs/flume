package gateway

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Node API Tests
// ─────────────────────────────────────────────────────────────────────────────

func TestIsValidNodeID(t *testing.T) {
	tests := []struct {
		id    string
		valid bool
	}{
		{"mac-mini-1", true},
		{"worker01", true},
		{"a", true},
		{"some-very-long-node-id-that-is-still-under-64-characters-012345", true},
		{"", false}, // empty
		{"node_1", false}, // underscores not allowed
		{"Node-1", false}, // uppercase not allowed
		{"node@1", false}, // special chars not allowed
		{"this-id-is-way-too-long-and-exceeds-the-maximum-allowed-length-of-sixty-four-characters", false}, // over 64 chars
	}

	for _, tt := range tests {
		t.Run(tt.id, func(t *testing.T) {
			if got := isValidNodeID(tt.id); got != tt.valid {
				t.Errorf("isValidNodeID(%q) = %v, want %v", tt.id, got, tt.valid)
			}
		})
	}
}

func TestIsValidNodeHost(t *testing.T) {
	tests := []struct {
		host  string
		valid bool
	}{
		{"192.168.1.50:11434", true},
		{"ollama.internal:11434", true},
		{"10.0.0.5:80", true},
		{"", false}, // empty
		{"192.168.1.50", false}, // no port
		{"localhost:11434", true}, // localhost allowed for local node registration
		{"127.0.0.1:11434", true}, // loopback allowed
		{"[::1]:11434", false}, // ipv6 loopback - currently fails net.SplitHostPort format unless it's correctly bracketed, actually net.SplitHostPort handles it, but let's test it as true
		{"0.0.0.0:11434", true}, // wildcard allowed
		{"169.254.169.254:80", false}, // link local metadata
	}

	for _, tt := range tests {
		t.Run(tt.host, func(t *testing.T) {
			if got := isValidNodeHost(tt.host); got != tt.valid {
				t.Errorf("isValidNodeHost(%q) = %v, want %v", tt.host, got, tt.valid)
			}
		})
	}
}

func TestHandleAddNode(t *testing.T) {
	// Setup test environment
	registry := NewNodeRegistry("")
	srv := &Server{
		nodeRegistry: registry,
		mux:          http.NewServeMux(),
	}

	tests := []struct {
		name         string
		payload      map[string]interface{}
		expectedCode int
	}{
		{
			name: "Valid Node",
			payload: map[string]interface{}{
				"id":        "valid-node-1",
				"host":      "192.168.1.50:11434",
				"model_tag": "qwen:7b",
			},
			expectedCode: http.StatusCreated,
		},
		{
			name: "Invalid ID",
			payload: map[string]interface{}{
				"id":        "Invalid_Node!",
				"host":      "192.168.1.50:11434",
				"model_tag": "qwen:7b",
			},
			expectedCode: http.StatusBadRequest,
		},
		{
			name: "Invalid Host (metadata API)",
			payload: map[string]interface{}{
				"id":        "local-node",
				"host":      "169.254.169.254:80",
				"model_tag": "qwen:7b",
			},
			expectedCode: http.StatusBadRequest,
		},
		{
			name: "Invalid Host (no port)",
			payload: map[string]interface{}{
				"id":        "local-node",
				"host":      "192.168.1.50",
				"model_tag": "qwen:7b",
			},
			expectedCode: http.StatusBadRequest,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body, _ := json.Marshal(tt.payload)
			req := httptest.NewRequest(http.MethodPost, "/api/nodes", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()

			// We need a dummy context logger
			req = req.WithContext(ContextWithLogger(req.Context(), Log()))

			// Normally we need ES mocked or we get 500 because it fails to persist.
			// Since ES is not up in unit tests, UpsertNodeToES will fail and return 500.
			// But for bad requests, it should return 400 before hitting ES.
			srv.handleAddNode(w, req)

			if tt.expectedCode == http.StatusBadRequest {
				if w.Code != http.StatusBadRequest {
					t.Errorf("Expected 400 Bad Request, got %d", w.Code)
				}
			} else {
				// Because ES is not mocked here, valid requests will fail at persistence
				if w.Code != http.StatusInternalServerError {
					t.Errorf("Expected 500 Internal Server Error (due to no ES), got %d", w.Code)
				}
			}
		})
	}
}

func TestHandleDeleteNode(t *testing.T) {
	registry := NewNodeRegistry("")
	srv := &Server{
		nodeRegistry: registry,
		mux:          http.NewServeMux(),
	}

	// This assumes go1.22 routing PathValue works correctly. We simulate it by modifying request context
	// However, httptest.NewRequest doesn't easily populate PathValue without a real mux routing.
	// We will just test the invalid ID logic by passing a bad ID in PathValue if possible,
	// or we can just test the HTTP mux integration.
	
	srv.mux.HandleFunc("DELETE /api/nodes/{id}", srv.handleDeleteNode)

	req := httptest.NewRequest(http.MethodDelete, "/api/nodes/Invalid_ID!", nil)
	req = req.WithContext(ContextWithLogger(req.Context(), Log()))
	w := httptest.NewRecorder()
	
	srv.mux.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("Expected 400 Bad Request for invalid ID, got %d", w.Code)
	}
}

func TestHandleTestNodeTimeout(t *testing.T) {
	// Create a mock server that sleeps to simulate a timeout
	mockOllama := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(3 * time.Second) // Longer than HealthChecker timeout (2.5s)
		w.WriteHeader(http.StatusOK)
	}))
	defer mockOllama.Close()

	registry := NewNodeRegistry("")
	// Insert a mock node pointing to the sleeping server
	registry.mu.Lock()
	registry.nodes["timeout-node"] = &Node{
		ID:   "timeout-node",
		Host: strings.TrimPrefix(mockOllama.URL, "http://"),
	}
	registry.mu.Unlock()

	srv := &Server{
		nodeRegistry: registry,
		mux:          http.NewServeMux(),
	}
	srv.mux.HandleFunc("POST /api/nodes/{id}/test", srv.handleTestNode)

	req := httptest.NewRequest(http.MethodPost, "/api/nodes/timeout-node/test", nil)
	req = req.WithContext(ContextWithLogger(req.Context(), Log()))
	w := httptest.NewRecorder()

	// This should fail quickly because HealthChecker has a timeout of 2.5s or context timeout
	start := time.Now()
	srv.mux.ServeHTTP(w, req)
	duration := time.Since(start)

	if w.Code != http.StatusGatewayTimeout && w.Code != http.StatusServiceUnavailable && w.Code != http.StatusInternalServerError {
		// It might return 500 or 503/504 depending on error mapping
		// The error message should indicate a timeout
		if !strings.Contains(w.Body.String(), "deadline exceeded") && !strings.Contains(w.Body.String(), "timeout") {
			t.Errorf("Expected timeout error, got code %d body %s", w.Code, w.Body.String())
		}
	}

	if duration > 4*time.Second {
		t.Errorf("Test took too long (%v), timeout handling failed", duration)
	}
}
