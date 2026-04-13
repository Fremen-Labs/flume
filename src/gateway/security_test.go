package gateway

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Security hardening tests (feature/gateway-security-hardening)
//
// Covers three fixes:
//   Fix 1 — SanitizeToolResponse fires at routing layer (not just handler)
//   Fix 2 — resolveAPIKey audit logging + fail-closed for managed providers
//   Fix 3 — ValidateChatRequest rejects malicious / malformed payloads
// ─────────────────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────────────────
// Fix 3: ValidateChatRequest
// ─────────────────────────────────────────────────────────────────────────────

func TestValidateChatRequest_NilRequest(t *testing.T) {
	if err := ValidateChatRequest(nil); err == nil {
		t.Error("expected error for nil request")
	}
}

func TestValidateChatRequest_EmptyMessages(t *testing.T) {
	req := &ChatRequest{Model: "llama3.2:3b"}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error when messages is empty")
	}
}

func TestValidateChatRequest_ValidRequest(t *testing.T) {
	req := &ChatRequest{
		Model:       "qwen2.5-coder:7b",
		Provider:    "ollama",
		Temperature: 0.7,
		MaxTokens:   512,
		Messages:    []Message{{Role: "user", Content: "hello"}},
	}
	if err := ValidateChatRequest(req); err != nil {
		t.Errorf("unexpected error for valid request: %v", err)
	}
}

func TestValidateChatRequest_TemperatureNaN(t *testing.T) {
	req := &ChatRequest{
		Temperature: math.NaN(),
		Messages:    []Message{{Role: "user", Content: "test"}},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for NaN temperature")
	}
}

func TestValidateChatRequest_TemperatureNegative(t *testing.T) {
	req := &ChatRequest{
		Temperature: -0.1,
		Messages:    []Message{{Role: "user", Content: "test"}},
	}
	// Negative temperature should be clamped to 0, not rejected.
	if err := ValidateChatRequest(req); err != nil {
		t.Errorf("expected negative temperature to be clamped, got error: %v", err)
	}
	if req.Temperature != 0 {
		t.Errorf("expected temperature clamped to 0, got %v", req.Temperature)
	}
}

func TestValidateChatRequest_TemperatureTooHigh(t *testing.T) {
	req := &ChatRequest{
		Temperature: 3.5,
		Messages:    []Message{{Role: "user", Content: "test"}},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for temperature > 2.0")
	}
}

func TestValidateChatRequest_MaxTokensNegative(t *testing.T) {
	req := &ChatRequest{
		MaxTokens: -1,
		Messages:  []Message{{Role: "user", Content: "test"}},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for negative max_tokens")
	}
}

func TestValidateChatRequest_MaxTokensExceedsHardCap(t *testing.T) {
	req := &ChatRequest{
		MaxTokens: 200_000,
		Messages:  []Message{{Role: "user", Content: "test"}},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for max_tokens exceeding hard cap")
	}
}

func TestValidateChatRequest_InvalidModelName(t *testing.T) {
	tests := []string{
		"model; rm -rf /",        // shell injection
		"../../../etc/passwd",    // path traversal
		strings.Repeat("a", 200), // too long
		"model name with spaces", // spaces not allowed
		"model\x00null",          // null byte
	}
	for _, model := range tests {
		req := &ChatRequest{
			Model:    model,
			Messages: []Message{{Role: "user", Content: "test"}},
		}
		if err := ValidateChatRequest(req); err == nil {
			t.Errorf("expected error for model name %q, got nil", model)
		}
	}
}

func TestValidateChatRequest_ValidModelNames(t *testing.T) {
	valid := []string{
		"llama3.2:3b",
		"qwen2.5-coder:7b",
		"deepseek-r1:14b",
		"library/model:latest",
		"gemini-2.5-pro",
		"", // empty = resolved from config
	}
	for _, model := range valid {
		req := &ChatRequest{
			Model:    model,
			Messages: []Message{{Role: "user", Content: "test"}},
		}
		if err := ValidateChatRequest(req); err != nil {
			t.Errorf("unexpected error for valid model name %q: %v", model, err)
		}
	}
}

func TestValidateChatRequest_UnknownProvider(t *testing.T) {
	req := &ChatRequest{
		Provider: "evil_provider",
		Messages: []Message{{Role: "user", Content: "test"}},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for unknown provider")
	}
}

func TestValidateChatRequest_InvalidMessageRole(t *testing.T) {
	req := &ChatRequest{
		Messages: []Message{
			{Role: "hacker", Content: "inject"},
		},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for invalid message role")
	}
}

func TestValidateChatRequest_ProviderNormalised(t *testing.T) {
	// Provider should be lowercased and trimmed
	req := &ChatRequest{
		Provider: "  OLLAMA  ",
		Messages: []Message{{Role: "user", Content: "test"}},
	}
	if err := ValidateChatRequest(req); err != nil {
		t.Errorf("unexpected error: %v", err)
	}
	if req.Provider != "ollama" {
		t.Errorf("expected provider normalised to 'ollama', got %q", req.Provider)
	}
}

func TestValidateChatRequest_MessageContentTooLarge(t *testing.T) {
	req := &ChatRequest{
		Messages: []Message{
			{Role: "user", Content: strings.Repeat("x", maxMessageContentLen+1)},
		},
	}
	if err := ValidateChatRequest(req); err == nil {
		t.Error("expected error for oversized message content")
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Fix 1: SanitizeToolResponse fires at the routing layer
// ─────────────────────────────────────────────────────────────────────────────

// TestSanitizeAppliedBeforeHandlerReturn verifies that duplicate tool calls
// are deduplicated inside Route() before the response reaches the handler.
// The mock server returns 2 identical tool calls; after routing, only 1 should remain.
func TestSanitizeAppliedBeforeHandlerReturn(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// Intentionally return two identical tool calls — the kind of
		// hallucination that Gemma/small models produce.
		json.NewEncoder(w).Encode(map[string]interface{}{
			"choices": []map[string]interface{}{
				{
					"message": map[string]interface{}{
						"role": "assistant",
						"tool_calls": []map[string]interface{}{
							{"function": map[string]interface{}{"name": "view_file", "arguments": `{"path": "/foo"}`}},
							{"function": map[string]interface{}{"name": "view_file", "arguments": `{"path": "/foo"}`}},
						},
					},
				},
			},
		})
	}))
	defer ts.Close()

	config := NewConfig("", time.Minute)
	config.DefaultProvider = ProviderOpenAICompat
	config.DefaultBaseURL = ts.URL
	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = ts.Client()

	req := &ChatRequest{
		Provider: ProviderOpenAICompat,
		Model:    "test-model",
		Messages: []Message{{Role: "user", Content: "call view_file"}},
	}

	resp, err := router.Route(context.Background(), req, true)
	if err != nil {
		t.Fatalf("unexpected route error: %v", err)
	}
	if len(resp.Message.ToolCalls) != 1 {
		t.Errorf("expected 1 deduped tool call after routing, got %d", len(resp.Message.ToolCalls))
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Fix 2: resolveAPIKey audit + fail-closed
// ─────────────────────────────────────────────────────────────────────────────

// TestResolveAPIKey_OpenAICompatGetssDummyKey verifies the dummy-key path
// still works for local openai-compatible endpoints.
func TestResolveAPIKey_OpenAICompatGetsDummyKey(t *testing.T) {
	config := NewConfig("", time.Minute)
	secrets := NewSecretStore("", "", "", time.Minute) // no OpenBao
	router := NewProviderRouter(config, secrets)

	key, err := router.resolveAPIKey(context.Background(), ProviderOpenAICompat, "")
	if err != nil {
		t.Fatalf("expected no error for OpenAICompat, got: %v", err)
	}
	if key != "sk-local-dummy-key" {
		t.Errorf("expected dummy key for OpenAICompat, got %q", key)
	}
}

// TestResolveAPIKey_OllamaReturnsEmpty confirms Ollama always gets an empty key.
func TestResolveAPIKey_OllamaReturnsEmpty(t *testing.T) {
	config := NewConfig("", time.Minute)
	secrets := NewSecretStore("", "", "", time.Minute)
	router := NewProviderRouter(config, secrets)

	key, err := router.resolveAPIKey(context.Background(), ProviderOllama, "")
	if err != nil {
		t.Fatalf("expected no error for Ollama, got: %v", err)
	}
	if key != "" {
		t.Errorf("expected empty key for Ollama, got %q", key)
	}
}

// TestResolveAPIKey_ManagedProviderFailsClosed verifies that OpenAI/Anthropic/Gemini
// return an error (and do NOT send an empty Authorization header) when no key is found.
func TestResolveAPIKey_ManagedProviderFailsClosed(t *testing.T) {
	managedProviders := []string{ProviderOpenAI, ProviderAnthropic, ProviderGemini, ProviderXAI, ProviderGrok}
	for _, p := range managedProviders {
		t.Run(p, func(t *testing.T) {
			config := NewConfig("", time.Minute)
			secrets := NewSecretStore("", "", "", time.Minute) // no OpenBao, no env
			router := NewProviderRouter(config, secrets)

			// Ensure env is not set for this test
			t.Setenv("LLM_API_KEY", "")

			_, err := router.resolveAPIKey(context.Background(), p, "")
			if err == nil {
				t.Errorf("expected error for managed provider %q with no key, got nil", p)
			}
		})
	}
}

// TestResolveAPIKey_EnvFallback verifies that LLM_API_KEY env is used when OpenBao is unavailable.
func TestResolveAPIKey_EnvFallback(t *testing.T) {
	t.Setenv("LLM_API_KEY", "env-test-key-12345")

	config := NewConfig("", time.Minute)
	secrets := NewSecretStore("", "", "", time.Minute) // no OpenBao
	router := NewProviderRouter(config, secrets)

	key, err := router.resolveAPIKey(context.Background(), ProviderOpenAI, "")
	if err != nil {
		t.Fatalf("expected no error when env key present, got: %v", err)
	}
	if key != "env-test-key-12345" {
		t.Errorf("expected env key, got %q", key)
	}
}
