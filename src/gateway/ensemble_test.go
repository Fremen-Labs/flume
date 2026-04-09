package gateway

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// mockProvider Always returns the preconfigured responses sequentially
type mockProvider struct {
	responses []*ChatResponse
	errs      []error
	callCount int
}

func (m *mockProvider) Chat(ctx context.Context, req *ChatRequest) (*ChatResponse, error) {
	return nil, nil
}

func (m *mockProvider) ChatTools(ctx context.Context, req *ChatRequest) (*ChatResponse, error) {
	idx := m.callCount
	if idx >= len(m.responses) {
		idx = len(m.responses) - 1
	}
	m.callCount++

	// Optional delay to test timeout/groups
	time.Sleep(10 * time.Millisecond)

	return m.responses[idx], m.errs[idx]
}

func (m *mockProvider) Name() string {
	return "mock-provider"
}

func TestScoreResponse(t *testing.T) {
	tests := []struct {
		name          string
		resp          *ChatResponse
		expectedScore int
	}{
		{
			name: "No tool calls",
			resp: &ChatResponse{
				Message: ResponseMessage{
					Role:    "assistant",
					Content: "Hello",
				},
			},
			expectedScore: 60,
		},
		{
			name: "Valid Elastro Tool With Perfect JSON",
			resp: &ChatResponse{
				Message: ResponseMessage{
					ToolCalls: []ToolCall{
						{
							Function: ToolCallFunction{
								Name:      "elastro_query_ast",
								Arguments: `{"query": "User"}`,
							},
						},
					},
				},
			},
			expectedScore: 100, // 40 syntax + 30 safety + 30 elastro pattern
		},
		{
			name: "Destructive Shell Auth Nullified",
			resp: &ChatResponse{
				Message: ResponseMessage{
					ToolCalls: []ToolCall{
						{
							Function: ToolCallFunction{
								Name:      "run_shell_cmd",
								Arguments: `{"command": "rm -rf /"}`,
							},
						},
					},
				},
			},
			expectedScore: 55, // 40 syntax + 0 safety + 15 elastro base penalty (30/2)
		},
		{
			name: "Malformed JSON Arguments",
			resp: &ChatResponse{
				Message: ResponseMessage{
					ToolCalls: []ToolCall{
						{
							Function: ToolCallFunction{
								Name:      "view_file",
								Arguments: `{"file": "/valid"`, // Missing closing brace
							},
						},
					},
				},
			},
			expectedScore: 60, // 0 syntax + 30 safety + 30 elastro pattern
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			score := ScoreResponse(tc.resp)
			if score != tc.expectedScore {
				t.Errorf("expected score %d, got %d", tc.expectedScore, score)
			}
		})
	}
}

// TestEnsembleRouter_Escalation mocks the full ExecuteEnsemble method to assert graceful fallback behavior.
func TestEnsembleRouter_Escalation(t *testing.T) {
	// 1. Setup minimal server with mock ensemble config
	config := NewConfig("", time.Minute)
	config.EnsembleEnabled = true
	config.EnsembleSize = 3
	config.FrontierFallbackModel = "mock-frontier"

	// 2. We mock `Router.Route` by overriding the HTTP client or using a local test server?
	// The `Server` struct has `s.router`. We can inject a test `http.Client`?
	// Actually, the easiest way to test ExecuteEnsemble logic is to start a mock HTTP server doing the routing,
	// but ExecuteEnsemble calls Server.router.Route. Our `gateway` heavily uses API calls.

	// Let's create a stub server that intercepts the router responses.
	// We configure a test HTTP server for the default BaseURL that returns terrible responses to force escalation.
	var callCount int
	frontierHit := false
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reqBody ChatRequest
		_ = json.NewDecoder(r.Body).Decode(&reqBody)

		if reqBody.Model == "mock-frontier" {
			frontierHit = true
		}

		callCount++
		w.Header().Set("Content-Type", "application/json")

		// Return terrible responses that fail scoring (bestScore < 70)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"choices": []map[string]interface{}{
				{
					"message": map[string]interface{}{
						"role": "assistant",
						"tool_calls": []map[string]interface{}{
							{
								"function": map[string]interface{}{
									"name":      "run_shell_cmd",
									"arguments": `rm -rf`, // Bad syntax AND unsafe
								},
							},
						},
					},
				},
			},
		})
	}))
	defer ts.Close()

	config.DefaultBaseURL = ts.URL
	config.DefaultProvider = ProviderOpenAI // We use an OpenAI compat mock to bypass Ollama specifics in testing

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = ts.Client() // Use the test client

	srv := &Server{
		config: config,
		router: router,
	}

	req := &ChatRequest{
		Provider: ProviderOpenAI,
		Model:    "mock-local",
	}

	// 3. Execute ensemble
	ctx := context.Background()
	_, err := srv.ExecuteEnsemble(ctx, req)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// 4. Verify Behavior
	// Should hit local 3 times, then hit frontier 1 time.
	if callCount != 4 {
		t.Errorf("Expected 4 total calls (3 local + 1 frontier), got %d", callCount)
	}

	if !frontierHit {
		t.Errorf("Expected frontier model fallback to be triggered due to low score")
	}
}
