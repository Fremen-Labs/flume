package llm

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNewClient(t *testing.T) {
	c := New(nil)
	if c == nil {
		t.Fatal("New returned nil")
	}
	if c.gatewayURL == "" {
		t.Error("gateway URL should not be empty")
	}
}

func TestGatewayAvailableHealthy(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			w.WriteHeader(200)
			return
		}
	}))
	defer ts.Close()

	c := New(nil)
	c.gatewayURL = ts.URL

	if !c.gatewayAvailable(context.Background()) {
		t.Error("expected gateway to be available")
	}

	// Cached check should also return true
	if !c.gatewayAvailable(context.Background()) {
		t.Error("cached check should return true")
	}
}

func TestGatewayAvailableUnhealthy(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
	}))
	defer ts.Close()

	c := New(nil)
	c.gatewayURL = ts.URL

	if c.gatewayAvailable(context.Background()) {
		t.Error("expected gateway to be unavailable")
	}
}

func TestPostGateway(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/chat" {
			resp := map[string]interface{}{
				"message": map[string]interface{}{
					"role":    "assistant",
					"content": "Hello, world!",
				},
				"usage": map[string]interface{}{
					"prompt_tokens":     10,
					"completion_tokens": 5,
				},
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(resp)
			return
		}
		w.WriteHeader(404)
	}))
	defer ts.Close()

	c := New(nil)
	c.gatewayURL = ts.URL

	result, err := c.postGateway(context.Background(), "/v1/chat", map[string]interface{}{
		"messages": []map[string]string{{"role": "user", "content": "hi"}},
		"model":    "test-model",
	}, 10, "")

	if err != nil {
		t.Fatalf("postGateway failed: %v", err)
	}

	msg, _ := result["message"].(map[string]interface{})
	content, _ := msg["content"].(string)
	if content != "Hello, world!" {
		t.Errorf("expected 'Hello, world!', got '%s'", content)
	}
}

func TestPostGateway4xxNoRetry(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(400)
		w.Write([]byte(`{"error": "bad request"}`))
	}))
	defer ts.Close()

	c := New(nil)
	c.gatewayURL = ts.URL

	_, err := c.postGateway(context.Background(), "/v1/chat", map[string]interface{}{}, 5, "")
	if err == nil {
		t.Fatal("expected error for 400 response")
	}
}

func TestChatResponse(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/health":
			w.WriteHeader(200)
		case "/v1/chat":
			json.NewEncoder(w).Encode(map[string]interface{}{
				"message": map[string]interface{}{
					"role":    "assistant",
					"content": "The answer is 42",
				},
			})
		}
	}))
	defer ts.Close()

	c := New(nil)
	c.gatewayURL = ts.URL

	resp, err := c.Chat(context.Background(), ChatRequest{
		Messages: []Message{{Role: "user", Content: "what is the answer?"}},
		Model:    "test",
	})
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}
	if resp.Content != "The answer is 42" {
		t.Errorf("expected content 'The answer is 42', got '%s'", resp.Content)
	}
}

func TestChatWithToolsResponse(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/health":
			w.WriteHeader(200)
		case "/v1/chat/tools":
			json.NewEncoder(w).Encode(map[string]interface{}{
				"message": map[string]interface{}{
					"role":    "assistant",
					"content": "",
					"tool_calls": []map[string]interface{}{
						{
							"id":   "call_1",
							"type": "function",
							"function": map[string]interface{}{
								"name":      "get_weather",
								"arguments": map[string]interface{}{"city": "NYC"},
							},
						},
					},
				},
			})
		}
	}))
	defer ts.Close()

	c := New(nil)
	c.gatewayURL = ts.URL

	resp, err := c.ChatWithTools(context.Background(), ChatToolsRequest{
		Messages: []Message{{Role: "user", Content: "weather in NYC?"}},
		Tools: []Tool{{
			Type:     "function",
			Function: ToolFunction{Name: "get_weather"},
		}},
		Model: "test",
	})
	if err != nil {
		t.Fatalf("ChatWithTools failed: %v", err)
	}
	if len(resp.Message.ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(resp.Message.ToolCalls))
	}
	if resp.Message.ToolCalls[0].Function.Name != "get_weather" {
		t.Errorf("expected tool name 'get_weather', got '%s'", resp.Message.ToolCalls[0].Function.Name)
	}
}

func TestResolveFallbackCloud(t *testing.T) {
	tests := []struct {
		provider string
		model    string
		expected string
	}{
		{"openai", "gpt-4.5-turbo", "gpt-4o"},
		{"openai", "gpt-3.5-turbo", "gpt-4o-mini"},
		{"openai", "gpt-4o", "gpt-4o-mini"},
		{"anthropic", "claude-3-opus-20240229", "claude-3-7-sonnet"},
		{"gemini", "gemini-2.5-pro-preview", "gemini-2.5-flash"},
		{"openai", "gpt-4o-mini", ""}, // no further fallback
		{"unknown", "any-model", ""},
	}

	for _, tt := range tests {
		result := ResolveFallback(context.Background(), tt.provider, tt.model, "")
		if result != tt.expected {
			t.Errorf("ResolveFallback(%s, %s) = %s, want %s",
				tt.provider, tt.model, result, tt.expected)
		}
	}
}
