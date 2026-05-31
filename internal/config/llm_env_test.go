package config

import (
	"context"
	"os"
	"testing"
)

func TestNormalizeGeminiModelID(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"gemini-1.5-flash", "gemini-2.5-flash"},
		{"gemini-1.5-flash-latest", "gemini-2.5-flash"},
		{"gemini-1.5-flash-8b", "gemini-2.5-flash"},
		{"gemini-1.5-pro", "gemini-2.5-pro"},
		{"gemini-1.5-pro-latest", "gemini-2.5-pro"},
		{"gemini-2.0-flash", "gemini-2.5-flash"},
		{"gemini-2.0-flash-lite", "gemini-2.5-flash-lite"},
		{"gemini-2.5-flash", "gemini-2.5-flash"}, // already current
		{"", "gemini-2.5-flash"},                  // empty → default
		{"custom-model", "custom-model"},          // unknown → passthrough
	}

	for _, tt := range tests {
		result := NormalizeGeminiModelID(tt.input)
		if result != tt.expected {
			t.Errorf("NormalizeGeminiModelID(%q) = %q, want %q", tt.input, result, tt.expected)
		}
	}
}

func TestResolveCloudAgentModel(t *testing.T) {
	tests := []struct {
		provider string
		stored   string
		global   string
		expected string
	}{
		// Cloud provider with stale Ollama default → use global model
		{"openai", "llama3.2", "gpt-4o", "gpt-4o"},
		{"anthropic", "llama3.2", "claude-3-opus", "claude-3-opus"},
		// Cloud provider with valid stored model → keep it
		{"openai", "gpt-4o-mini", "gpt-4o", "gpt-4o-mini"},
		// Ollama provider → keep stored model
		{"ollama", "llama3.2", "gpt-4o", "llama3.2"},
		// Gemini provider → normalize model ID
		{"gemini", "gemini-1.5-flash", "gemini-2.5-pro", "gemini-2.5-flash"},
		// Empty stored model → use global
		{"openai", "", "gpt-4o", "gpt-4o"},
		// Empty global → default
		{"openai", "llama3.2", "", "llama3.2"},
	}

	for _, tt := range tests {
		result := ResolveCloudAgentModel(tt.provider, tt.stored, tt.global)
		if result != tt.expected {
			t.Errorf("ResolveCloudAgentModel(%q, %q, %q) = %q, want %q",
				tt.provider, tt.stored, tt.global, result, tt.expected)
		}
	}
}

func TestCloudLLMProviders(t *testing.T) {
	cloud := []string{"openai", "anthropic", "gemini", "xai", "mistral", "cohere"}
	for _, p := range cloud {
		if !CloudLLMProviders[p] {
			t.Errorf("expected %q to be a cloud provider", p)
		}
	}

	local := []string{"ollama", "openai_compatible", "local", ""}
	for _, p := range local {
		if CloudLLMProviders[p] {
			t.Errorf("expected %q to NOT be a cloud provider", p)
		}
	}
}

func TestGetActiveLLMModel_ESFailureFallback(t *testing.T) {
	// When ES is unreachable or returns no document, should fall back to env or default
	os.Unsetenv("LLM_MODEL")
	defer os.Unsetenv("LLM_MODEL")

	// Use an invalid URL to force loadLLMConfigFromES to return empty map (failure path)
	got := GetActiveLLMModel(context.Background(), "http://127.0.0.1:1", "", nil)
	if got != DefaultOllamaModel {
		t.Errorf("expected fallback to default %q on ES failure, got %q", DefaultOllamaModel, got)
	}

	// Env var should still win even if ES fails
	os.Setenv("LLM_MODEL", "qwen2.5:72b")
	defer os.Unsetenv("LLM_MODEL")
	got = GetActiveLLMModel(context.Background(), "http://127.0.0.1:1", "", nil)
	if got != "qwen2.5:72b" {
		t.Errorf("expected env var to win on ES failure, got %q", got)
	}
}
