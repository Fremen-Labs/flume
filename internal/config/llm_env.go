// llm_env.go — Keep worker processes aligned with cluster-native LLM settings.
//
// Direct port of Python: workspace_llm_env.py (5 AST nodes).
//
// Workers use GetActiveLLMModel() to read the current model directly from ES
// (flume-llm-config) rather than relying on os.environ baked in at container start.
// This enables hot-reloading the model without a container restart.
package config

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// LLM provider constants — mirrors Python constants.
const (
	ProviderOllama           = "ollama"
	ProviderOpenAICompatible = "openai_compatible"
	ProviderGemini           = "gemini"

	DefaultOllamaModel = "llama3.2"
	DefaultGeminiModel = "gemini-2.5-flash"
)

// CloudLLMProviders are providers that use hosted APIs.
// Derived from Python: _CLOUD_LLM_PROVIDER_IDS.
var CloudLLMProviders = map[string]bool{
	"openai":    true,
	"anthropic": true,
	"gemini":    true,
	"xai":       true,
	"mistral":   true,
	"cohere":    true,
}

// GeminiModelAliases maps deprecated Gemini model IDs to current stable IDs.
// Derived from Python: _GEMINI_MODEL_ALIASES.
var GeminiModelAliases = map[string]string{
	"gemini-1.5-flash":        "gemini-2.5-flash",
	"gemini-1.5-flash-latest": "gemini-2.5-flash",
	"gemini-1.5-flash-8b":     "gemini-2.5-flash",
	"gemini-1.5-pro":          "gemini-2.5-pro",
	"gemini-1.5-pro-latest":   "gemini-2.5-pro",
	"gemini-2.0-flash":        "gemini-2.5-flash",
	"gemini-2.0-flash-lite":   "gemini-2.5-flash-lite",
}

// NormalizeGeminiModelID maps deprecated Gemini API model strings to current stable IDs.
// Derived from Python: normalize_gemini_model_id().
func NormalizeGeminiModelID(modelID string) string {
	m := strings.TrimSpace(modelID)
	if m == "" {
		return DefaultGeminiModel
	}
	if alias, ok := GeminiModelAliases[m]; ok {
		return alias
	}
	return m
}

// ResolveCloudAgentModel handles stale per-role agent_models.json entries.
// When a role stores provider=openai with model=llama3.2 (the Ollama default),
// it replaces the model with the global Settings model.
// Derived from Python: resolve_cloud_agent_model().
func ResolveCloudAgentModel(providerID, storedModel, globalLLMModel string) string {
	pid := strings.TrimSpace(strings.ToLower(providerID))
	sm := strings.TrimSpace(storedModel)
	gm := strings.TrimSpace(globalLLMModel)
	if gm == "" {
		gm = DefaultOllamaModel
	}

	out := sm
	if CloudLLMProviders[pid] && sm == DefaultOllamaModel {
		out = gm
	}
	if out == "" {
		out = gm
	}
	if pid == ProviderGemini {
		out = NormalizeGeminiModelID(out)
	}
	return out
}

// GetActiveLLMModel returns the current LLM model from ES (flume-llm-config),
// falling back to the process-env value baked in at container start.
// This allows hot-reloading the model without a container restart.
// Derived from Python: get_active_llm_model().
func GetActiveLLMModel(ctx context.Context, esURL, esAPIKey string, logger *slog.Logger) string {
	if logger == nil {
		logger = slog.Default()
	}

	// Try reading from ES first
	cfg := loadLLMConfigFromES(ctx, esURL, esAPIKey)
	if model := strings.TrimSpace(cfg["LLM_MODEL"]); model != "" {
		return model
	}

	// ES config was unavailable or had no LLM_MODEL (best-effort hot-reload path).
	// This is normal in many environments; we fall back gracefully.
	// Structured log for diagnostics (affects worker LLM behavior).
	if logger != nil {
		logger.Debug("LLM hot-reload config not found in ES, using env/default",
			slog.String("es_url", esURL))
	}

	// Fall back to env var
	if model := strings.TrimSpace(os.Getenv("LLM_MODEL")); model != "" {
		return model
	}
	return DefaultOllamaModel
}

// loadLLMConfigFromES reads LLM config from the flume-llm-config index.
// This is a lightweight helper that avoids importing the secrets package
// to prevent import cycles (config ← secrets → config).
func loadLLMConfigFromES(ctx context.Context, esURL, esAPIKey string) map[string]string {
	if esURL == "" {
		return make(map[string]string)
	}

	url := fmt.Sprintf("%s/flume-llm-config/_doc/singleton", strings.TrimRight(esURL, "/"))
	reqCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, url, nil)
	if err != nil {
		return make(map[string]string)
	}
	if esAPIKey != "" {
		req.Header.Set("Authorization", "ApiKey "+esAPIKey)
	}

	resp, err := configHTTPClient.Do(req)
	if err != nil {
		return make(map[string]string)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return make(map[string]string)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return make(map[string]string)
	}

	var esDoc struct {
		Found  bool                   `json:"found"`
		Source map[string]interface{} `json:"_source"`
	}
	if err := json.Unmarshal(body, &esDoc); err != nil || !esDoc.Found {
		return make(map[string]string)
	}

	result := make(map[string]string)
	for k, v := range esDoc.Source {
		if v != nil {
			result[k] = fmt.Sprintf("%v", v)
		}
	}
	return result
}
