package gateway

import (
	"bytes"
	"context"
	"os"
	"strings"
	"testing"
)

func TestSecureHandlerRedacts(t *testing.T) {
	var buf bytes.Buffer
	log := NewTestLogger(&buf)

	log.Info("test message",
		"api_key", "sk-secret-12345",
		"model", "gemma4:26b",
		"token", "hvs.mytoken",
		"normal_field", "visible",
	)

	output := buf.String()

	if strings.Contains(output, "sk-secret-12345") {
		t.Error("api_key value was not redacted")
	}
	if strings.Contains(output, "hvs.mytoken") {
		t.Error("token value was not redacted")
	}
	if !strings.Contains(output, maskedValue) {
		t.Error("redacted placeholder not found in output")
	}
	if !strings.Contains(output, "gemma4:26b") {
		t.Error("model value should not be redacted")
	}
	if !strings.Contains(output, "visible") {
		t.Error("normal_field value should not be redacted")
	}
}

func TestParseLogLevel(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"debug", "DEBUG"},
		{"info", "INFO"},
		{"warn", "WARN"},
		{"warning", "WARN"},
		{"error", "ERROR"},
		{"", "INFO"},
		{"unknown", "INFO"},
	}
	for _, tt := range tests {
		level := parseLogLevel(tt.input)
		if level.String() != tt.want {
			t.Errorf("parseLogLevel(%q) = %s, want %s", tt.input, level.String(), tt.want)
		}
	}
}

func TestIsThinkingModel(t *testing.T) {
	tests := []struct {
		model string
		want  bool
	}{
		{"gemma4:26b", true},
		{"gemma3:2b", true},
		{"qwq:32b", true},
		{"deepseek-r1:14b", true},
		{"llama3.2", false},
		{"qwen2.5-coder:7b", false},
		{"gpt-4o", false},
	}
	for _, tt := range tests {
		got := IsThinkingModel(tt.model)
		if got != tt.want {
			t.Errorf("IsThinkingModel(%q) = %v, want %v", tt.model, got, tt.want)
		}
	}
}

func TestNormalizeGeminiModel(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"gemini-1.5-flash", "gemini-2.5-flash"},
		{"gemini-2.0-flash", "gemini-2.5-flash"},
		{"gemini-2.5-pro", "gemini-2.5-pro"},
		{"", "gemini-2.5-flash"},
	}
	for _, tt := range tests {
		got := NormalizeGeminiModel(tt.input)
		if got != tt.want {
			t.Errorf("NormalizeGeminiModel(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestSecretStoreTokenResolution(t *testing.T) {
	// Clear the environment to ensure isolation
	os.Unsetenv("OPENBAO_TOKEN")

	t.Run("explicit token", func(t *testing.T) {
		store := NewSecretStore("http://openbao:8200", "test-token", "", 0)
		if store.token != "test-token" {
			t.Errorf("Expected token to be 'test-token', got %q", store.token)
		}
	})

	t.Run("env fallback", func(t *testing.T) {
		os.Setenv("OPENBAO_TOKEN", "env-token")
		defer os.Unsetenv("OPENBAO_TOKEN")

		store := NewSecretStore("http://openbao:8200", "", "", 0)
		if store.token != "env-token" {
			t.Errorf("Expected token to fall back to 'env-token', got %q", store.token)
		}
	})

	t.Run("empty token error path", func(t *testing.T) {
		store := NewSecretStore("http://openbao:8200", "", "", 0)
		// Assuming readKV is internal, we can test GetLLMKey which uses readKV
		ctx := context.Background()
		key := store.GetLLMKey(ctx, "test-id")
		if key != "" {
			t.Errorf("Expected empty key due to missing OPENBAO_TOKEN, got %q", key)
		}
	})
}
