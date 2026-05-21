// fallback.go — Intelligent model fallback resolver.
//
// Direct port of Python: utils/llm_client_fallback.py (1 AST node).
// Provides dynamic fallback models for both remote APIs (OpenAI, Anthropic, Gemini)
// and local clusters (Ollama).
package llm

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

// CloudFallbacks maps provider → [(prefix, fallback_model)].
// Derived from Python: CLOUD_FALLBACKS dict.
var CloudFallbacks = map[string][][2]string{
	"openai": {
		{"gpt-4.5", "gpt-4o"},
		{"gpt-4-", "gpt-4o"},
		{"gpt-3.5", "gpt-4o-mini"},
		{"gpt-4o", "gpt-4o-mini"},
	},
	"anthropic": {
		{"claude-3-5", "claude-3-7-sonnet"},
		{"claude-3-opus", "claude-3-7-sonnet"},
		{"claude-3-7-sonnet", "claude-3-5-sonnet"},
		{"claude-3-5-sonnet", "claude-3-5-haiku"},
	},
	"gemini": {
		{"gemini-1.5", "gemini-2.5-flash"},
		{"gemini-1.0", "gemini-2.5-flash"},
		{"gemini-2.5-pro", "gemini-2.5-flash"},
	},
}

// OllamaModelTiers is the tiered ranking for local Ollama fallback selection.
// Derived from Python: tier_lists in resolve_ollama_fallback().
var OllamaModelTiers = [][]string{
	// Tier 1: Coding Agents
	{"deepseek-r1", "qwen2.5-coder", "deepseek-coder", "deepseek-coder-v2"},
	// Tier 2: Frontier Multipurpose
	{"llama3.3", "llama-3.3", "llama3.2", "llama3.1", "llama3"},
	// Tier 3: Mid-weight Local
	{"qwen2.5", "gemma2", "mistral", "mixtral", "phi4", "phi3"},
}

// ResolveFallback returns an intelligent fallback model for any provider.
// Derived from Python: llm_client_fallback.py resolve_fallback_model().
func ResolveFallback(provider, failedModel, baseURL string) string {
	prov := strings.TrimSpace(strings.ToLower(provider))
	if prov == "ollama" {
		return resolveOllamaFallback(failedModel, baseURL)
	}
	return resolveCloudFallback(prov, failedModel)
}

// resolveCloudFallback resolves a remote model fallback based on predefined mappings.
// Derived from Python: resolve_cloud_fallback().
func resolveCloudFallback(provider, failedModel string) string {
	p := strings.TrimSpace(strings.ToLower(provider))
	m := strings.TrimSpace(strings.ToLower(failedModel))

	mappings, ok := CloudFallbacks[p]
	if !ok {
		return ""
	}

	for _, pair := range mappings {
		prefix, fallback := pair[0], pair[1]
		if strings.HasPrefix(m, prefix) && m != fallback {
			return fallback
		}
	}
	return ""
}

// resolveOllamaFallback dynamically fetches available models from Ollama
// and returns the best fallback based on the tiered ranking system.
// Derived from Python: resolve_ollama_fallback().
func resolveOllamaFallback(failedModel, baseURL string) string {
	tags := fetchOllamaTags(baseURL)
	if len(tags) == 0 {
		return ""
	}

	// Filter out the failed model
	failedLower := strings.ToLower(failedModel)
	var available []string
	for _, t := range tags {
		if strings.ToLower(t) != failedLower {
			available = append(available, t)
		}
	}
	if len(available) == 0 {
		return ""
	}

	// Search through tiers for the best fallback
	for _, tier := range OllamaModelTiers {
		for _, keyword := range tier {
			for _, tag := range available {
				if strings.Contains(strings.ToLower(tag), keyword) {
					return tag
				}
			}
		}
	}

	// Tier 4: grab the first available model
	return available[0]
}

// fetchOllamaTags queries the Ollama API for available models.
// Derived from Python: _fetch_ollama_tags().
func fetchOllamaTags(baseURL string) []string {
	url := strings.TrimRight(baseURL, "/") + "/api/tags"

	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		slog.Warn("failed to fetch Ollama tags",
			slog.String("url", url),
			slog.String("error", err.Error()),
		)
		return nil
	}
	defer resp.Body.Close()

	var data struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		slog.Warn("failed to decode Ollama tags response",
			slog.String("error", err.Error()),
		)
		return nil
	}

	var names []string
	for _, m := range data.Models {
		if m.Name != "" {
			names = append(names, m.Name)
		}
	}
	return names
}

// Ensure fmt is used.
var _ = fmt.Sprintf
