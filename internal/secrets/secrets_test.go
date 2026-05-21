package secrets

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestGenerateShortID(t *testing.T) {
	id1 := GenerateShortID()
	id2 := GenerateShortID()

	if len(id1) != 12 {
		t.Errorf("expected 12-char ID, got %d chars: %s", len(id1), id1)
	}
	if id1 == id2 {
		t.Error("two generated IDs should not be equal")
	}
}

func TestScrubSecrets(t *testing.T) {
	doc := map[string]interface{}{
		"version": 1,
		"credentials": []interface{}{
			map[string]interface{}{
				"id":     "abc123",
				"label":  "Test Key",
				"apiKey": "sk-secret-12345",
			},
			map[string]interface{}{
				"id":     "def456",
				"label":  "Already Masked",
				"apiKey": OpenbaoMask,
			},
		},
	}

	scrubbed := scrubSecrets(doc)

	creds, _ := scrubbed["credentials"].([]interface{})
	if len(creds) != 2 {
		t.Fatalf("expected 2 credentials, got %d", len(creds))
	}

	first, _ := creds[0].(map[string]interface{})
	if first["apiKey"] != OpenbaoMask {
		t.Errorf("expected secret to be scrubbed, got %v", first["apiKey"])
	}

	second, _ := creds[1].(map[string]interface{})
	if second["apiKey"] != OpenbaoMask {
		t.Errorf("already-masked key should remain, got %v", second["apiKey"])
	}
}

func TestNormalizeProviderID(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"google", "gemini"},
		{"google-ai", "gemini"},
		{"google_ai", "gemini"},
		{"googleaistudio", "gemini"},
		{"generativelanguage", "gemini"},
		{"openai", "openai"},
		{"anthropic", "anthropic"},
		{"GEMINI", "gemini"},
		{"", ""},
	}

	for _, tt := range tests {
		result := NormalizeProviderID(tt.input)
		if result != tt.expected {
			t.Errorf("NormalizeProviderID(%q) = %q, want %q", tt.input, result, tt.expected)
		}
	}
}

func TestKeySuffix(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{"", ""},
		{"ab", "••••"},
		{"abcd", "••••"},
		{"sk-1234567890abcdef", "cdef"},
	}

	for _, tt := range tests {
		result := keySuffix(tt.input)
		if result != tt.expected {
			t.Errorf("keySuffix(%q) = %q, want %q", tt.input, result, tt.expected)
		}
	}
}

func TestValidateGitHubToken(t *testing.T) {
	tests := []struct {
		token string
		valid bool
	}{
		{"ghp_AbCdEfGhIjKlMnOpQrSt", true},
		{"github_pat_AbCdEfGhIjKlMnOpQrSt", true},
		{"ghs_AbCdEfGhIjKlMnOpQrSt", true},
		{"gho_AbCdEfGhIjKlMnOpQrSt", true},
		{"ghu_AbCdEfGhIjKlMnOpQrSt", true},
		{"sk-not-a-github-token", false},
		{"", false},
		{"ghp_short", false},
	}

	for _, tt := range tests {
		result := ValidateGitHubToken(tt.token)
		if result != tt.valid {
			t.Errorf("ValidateGitHubToken(%q) = %v, want %v", tt.token, result, tt.valid)
		}
	}
}

func TestESStoreLoadSave(t *testing.T) {
	stored := make(map[string]string)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == "GET" && r.URL.Path == "/flume-llm-config/_doc/singleton":
			if val, ok := stored["llm-config"]; ok {
				json.NewEncoder(w).Encode(map[string]interface{}{
					"found":   true,
					"_source": json.RawMessage(val),
				})
			} else {
				w.WriteHeader(404)
			}
		case r.Method == "PUT" && r.URL.Path == "/flume-llm-config/_doc/singleton":
			body, _ := json.Marshal(map[string]interface{}{})
			json.NewDecoder(r.Body).Decode(&body)
			stored["llm-config"] = string(body)
			w.WriteHeader(200)
			json.NewEncoder(w).Encode(map[string]interface{}{"result": "updated"})
		case r.Method == "HEAD":
			w.WriteHeader(200)
		default:
			w.WriteHeader(404)
		}
	}))
	defer ts.Close()

	store := &ESStore{
		esURL:      ts.URL,
		esAPIKey:   "test-key",
		httpClient: ts.Client(),
		logger:     nil,
	}
	// Use default logger
	store.logger = defaultLogger()

	// Test IndexExists
	if !store.IndexExists(context.Background(), "any-index") {
		t.Error("expected IndexExists to return true for mocked HEAD 200")
	}

	// Test LoadLLMConfig when empty
	cfg := store.LoadLLMConfig(context.Background())
	if cfg != nil {
		t.Errorf("expected nil config when index empty, got %v", cfg)
	}
}

func TestLLMCredentialStoreDefaults(t *testing.T) {
	doc := DefaultLLMDoc()
	if doc.Version != 1 {
		t.Errorf("expected version 1, got %d", doc.Version)
	}
	if len(doc.Credentials) != 0 {
		t.Errorf("expected 0 credentials, got %d", len(doc.Credentials))
	}
}

func TestGHTokenStoreDefaults(t *testing.T) {
	doc := DefaultGHDoc()
	if doc.Version != 1 {
		t.Errorf("expected version 1, got %d", doc.Version)
	}
	if len(doc.Tokens) != 0 {
		t.Errorf("expected 0 tokens, got %d", len(doc.Tokens))
	}
}

func TestGHLabelTaken(t *testing.T) {
	tokens := []GHCredential{
		{ID: "a1", Label: "Production"},
		{ID: "b2", Label: "Development"},
	}

	if !ghLabelTaken(tokens, "production", "") {
		t.Error("expected case-insensitive match")
	}
	if ghLabelTaken(tokens, "production", "a1") {
		t.Error("should exclude ID a1")
	}
	if ghLabelTaken(tokens, "staging", "") {
		t.Error("staging should not match")
	}
}

func TestDocToLLMMetadata(t *testing.T) {
	raw := map[string]interface{}{
		"version":             float64(2),
		"activeCredentialId":  "abc",
		"defaultCredentialId": "def",
		"credentials": []interface{}{
			map[string]interface{}{
				"id":       "abc",
				"label":    "Test",
				"provider": "openai",
				"apiKey":   "sk-test",
				"baseUrl":  "https://api.openai.com",
			},
		},
	}

	doc := docToLLMMetadata(raw)
	if doc.Version != 2 {
		t.Errorf("expected version 2, got %d", doc.Version)
	}
	if doc.ActiveCredentialID != "abc" {
		t.Errorf("expected activeCredentialId 'abc', got %q", doc.ActiveCredentialID)
	}
	if len(doc.Credentials) != 1 {
		t.Fatalf("expected 1 credential, got %d", len(doc.Credentials))
	}
	if doc.Credentials[0].Provider != "openai" {
		t.Errorf("expected provider 'openai', got %q", doc.Credentials[0].Provider)
	}
}

func defaultLogger() *slog.Logger {
	return slog.Default()
}
