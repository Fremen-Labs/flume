package ui

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/log"
)

// ─────────────────────────────────────────────────────────────────────────────
// Model Suggestion Engine — Tab-autocomplete for the CLI wizard
// ─────────────────────────────────────────────────────────────────────────────

// frontierModels returns well-known model identifiers for each cloud provider.
// These are kept as a static catalog since frontier providers publish fixed
// model names that change infrequently.
var frontierModels = map[string][]string{
	"openai": {
		// Codex models (OAuth / Codex scope required)
		"codex-mini-latest",
		"codex-mini-2025-01-01",
		"gpt-5.4",
		"gpt-5.4-mini",
		"gpt-5.3-codex",
		"gpt-5.3-codex-spark",
		"gpt-5.3",
		// GPT-4.1 series (April 2026)
		"gpt-4.1",
		"gpt-4.1-mini",
		"gpt-4.1-nano",
		// Reasoning models
		"o4-mini",
		"o3",
		"o3-mini",
		// GPT-4o series
		"gpt-4o",
		"gpt-4o-mini",
		// Legacy
		"gpt-4-turbo",
		"gpt-4",
		"gpt-3.5-turbo",
		"o1",
		"o1-mini",
	},
	"anthropic": {
		// Claude 4.7 (current flagship — April 2026)
		"claude-opus-4-7",
		// Claude 4.6 (Feb 2026)
		"claude-sonnet-4-6",
		// Claude 4.5
		"claude-opus-4-5",
		"claude-sonnet-4-5",
		"claude-haiku-4-5",
		// Legacy
		"claude-3-5-sonnet-latest",
		"claude-3-5-haiku-latest",
	},
	"gemini": {
		// Gemini 3.1 series (current — April 2026)
		"gemini-3.1-pro-preview",
		"gemini-3.1-flash-lite-preview",
		// Gemini 3 series
		"gemini-3-flash-preview",
		// Gemini 2.5 series (legacy, still active on some endpoints)
		"gemini-2.5-pro",
		"gemini-2.5-flash",
		"gemini-2.0-flash",
	},
	"grok": {
		// Grok 4.x series (current — 2026)
		"grok-4.20",
		"grok-4.1-fast",
		"grok-4",
		// Grok 3 (legacy)
		"grok-3",
		"grok-3-mini",
	},
}

// commonOllamaModels is a fallback list of popular Ollama model tags used when
// the local Ollama instance is unreachable (e.g., not running yet).
// Updated April 2026 — includes latest Llama 4, Qwen 3.x, Gemma 4, and
// DeepSeek V3 families alongside proven staples.
var commonOllamaModels = []string{
	// Meta Llama 4 (April 2025+)
	"llama4:scout",
	"llama4:maverick",
	"llama4:8b",
	// Meta Llama 3.x (widely deployed)
	"llama3.3:70b",
	"llama3.2:3b",
	"llama3.1:8b",
	"llama3.1:70b",
	// Alibaba Qwen 3.x (2025-2026)
	"qwen3.6:35b-a3b",
	"qwen3.5:35b-a3b",
	"qwen3:32b",
	"qwen3:8b",
	"qwen3:0.6b",
	"qwen2.5-coder:32b",
	"qwen2.5-coder:14b",
	"qwen2.5-coder:7b",
	// DeepSeek (2025-2026)
	"deepseek-v3.2-exp:7b",
	"deepseek-r1:70b",
	"deepseek-r1:32b",
	"deepseek-r1:14b",
	"deepseek-r1:7b",
	// Google Gemma (2025-2026)
	"gemma4:12b",
	"gemma4:27b",
	"gemma3:12b",
	"gemma3:27b",
	"gemma3:1b",
	// Microsoft Phi (2025-2026)
	"phi4-reasoning:14b",
	"phi4:14b",
	// Mistral
	"mistral:latest",
	"mistral-nemo:latest",
	"mixtral:8x7b",
	// Code-specific
	"codellama:7b",
	"codellama:34b",
	"starcoder2:7b",
}

// ModelSuggestionsForProvider returns a sorted list of model name suggestions
// for the given LLM provider. For Ollama/Exo providers, it combines live model
// discovery from the local Ollama instance with a static fallback catalog.
func ModelSuggestionsForProvider(provider, ollamaHost string) []string {
	switch provider {
	case "ollama", "exo":
		return ollamaSuggestions(ollamaHost)
	default:
		if models, ok := frontierModels[provider]; ok {
			return models
		}
		return nil
	}
}

// ollamaSuggestions tries to discover models from a live Ollama instance,
// then merges with the static common model list for comprehensive coverage.
func ollamaSuggestions(host string) []string {
	seen := make(map[string]bool)
	var suggestions []string

	// Live discovery: try the configured host first, then localhost.
	for _, h := range uniqueHosts(host) {
		models := fetchOllamaModels(h)
		for _, m := range models {
			if !seen[m] {
				seen[m] = true
				suggestions = append(suggestions, m)
			}
		}
	}

	// Merge static fallback catalog (fills gaps when Ollama isn't running).
	for _, m := range commonOllamaModels {
		if !seen[m] {
			seen[m] = true
			suggestions = append(suggestions, m)
		}
	}

	sort.Strings(suggestions)
	return suggestions
}

// fetchOllamaModels calls GET /api/tags on the given Ollama host and returns
// all discovered model names. Returns nil on any error (non-fatal).
func fetchOllamaModels(host string) []string {
	if host == "" {
		return nil
	}

	url := fmt.Sprintf("http://%s/api/tags", host)
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		log.Debug("fetchOllamaModels: network error while fetching tags", "host", host, "error", err)
		return nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Debug("fetchOllamaModels: non-200 status code", "host", host, "status", resp.StatusCode)
		return nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		log.Debug("fetchOllamaModels: error reading response body", "host", host, "error", err)
		return nil
	}

	var tagsResp struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &tagsResp); err != nil {
		log.Debug("fetchOllamaModels: JSON parse failure", "host", host, "error", err)
		return nil
	}

	models := make([]string, 0, len(tagsResp.Models))
	for _, m := range tagsResp.Models {
		if m.Name != "" {
			models = append(models, m.Name)
		}
	}
	return models
}

// uniqueHosts returns a de-duplicated list of Ollama hosts to try, normalising
// port-less entries to include :11434.
func uniqueHosts(primary string) []string {
	normalise := func(h string) string {
		h = strings.TrimSpace(h)
		if h == "" {
			return ""
		}
		if !strings.Contains(h, ":") {
			h += ":11434"
		}
		return h
	}

	seen := make(map[string]bool)
	var hosts []string
	for _, h := range []string{primary, "127.0.0.1:11434"} {
		n := normalise(h)
		if n != "" && !seen[n] {
			seen[n] = true
			hosts = append(hosts, n)
		}
	}
	return hosts
}
