package gateway

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// FrontierProber periodically queries active frontier models in the routing policy
// to extract real-time token limit headspace and USD spend, avoiding the need for
// highly-permissioned organization management APIs.
type FrontierProber struct {
	config  *Config
	secrets *SecretStore
	router  *ProviderRouter
	client  *http.Client
}

// NewFrontierProber creates a new background prober.
func NewFrontierProber(config *Config, secrets *SecretStore, router *ProviderRouter) *FrontierProber {
	return &FrontierProber{
		config:  config,
		secrets: secrets,
		router:  router,
		client:  &http.Client{Timeout: 15 * time.Second},
	}
}

// Start launches the background goroutine to poll every 60 seconds.
func (p *FrontierProber) Start(ctx context.Context) {
	Log().Info("frontier_prober: started active telemetry polling", slog.Int("interval_sec", 60))
	go func() {
		ticker := time.NewTicker(60 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				Log().Info("frontier_prober: stopped")
				return
			case <-ticker.C:
				p.probeAll(ctx)
			}
		}
	}()
}

func (p *FrontierProber) probeAll(ctx context.Context) {
	policy := p.config.GetRoutingPolicy()
	if policy.Mode == RoutingModeLocalOnly || len(policy.FrontierMix) == 0 {
		return
	}

	for _, fm := range policy.FrontierMix {
		// Do not probe explicitly closed/disabled models
		if fm.CircuitOpen || fm.Weight <= 0 {
			continue
		}

		go p.probeModel(ctx, fm)
	}
}

func (p *FrontierProber) probeModel(ctx context.Context, fm FrontierModelWeight) {
	log := WithContext(ctx).With(slog.String("model", fm.Model), slog.String("provider", fm.Provider))

	apiKey, err := p.router.resolveAPIKey(ctx, fm.Provider, fm.CredentialID)
	if err != nil || apiKey == "" {
		log.Debug("frontier_prober: skipping probe due to missing api key")
		return
	}

	baseURL := p.config.GetBaseURL(fm.Provider)
	if baseURL == "" {
		if fm.Provider == ProviderGemini {
			baseURL = ProviderBaseURLs[ProviderGemini]
		} else if fm.Provider == ProviderOpenAI {
			baseURL = ProviderBaseURLs[ProviderOpenAI]
		} else if fm.Provider == ProviderAnthropic {
			baseURL = ProviderBaseURLs[ProviderAnthropic]
		} else if fm.Provider == ProviderXAI || fm.Provider == ProviderGrok {
			baseURL = ProviderBaseURLs[fm.Provider]
		} else {
			return
		}
	}

	var req *http.Request
	var errReq error

	// We send a minimal 1-token request purely to extract ratelimit headers safely.
	if fm.Provider == ProviderAnthropic {
		url := strings.TrimRight(baseURL, "/") + "/v1/messages"
		payload := `{"model":"` + fm.Model + `","max_tokens":1,"messages":[{"role":"user","content":"."}]}`
		req, errReq = http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(payload))
		if errReq == nil {
			req.Header.Set("x-api-key", apiKey)
			req.Header.Set("anthropic-version", "2023-06-01")
			req.Header.Set("Content-Type", "application/json")
		}
	} else {
		// OpenAI compatible format
		url := strings.TrimRight(baseURL, "/") + "/v1/chat/completions"
		payload := `{"model":"` + fm.Model + `","max_tokens":1,"messages":[{"role":"user","content":"."}]}`
		req, errReq = http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(payload))
		if errReq == nil {
			req.Header.Set("Authorization", "Bearer "+apiKey)
			req.Header.Set("Content-Type", "application/json")
		}
	}

	if errReq != nil {
		return
	}

	resp, err := p.client.Do(req)
	if err != nil {
		log.Warn("frontier_prober: request failed", slog.String("error", err.Error()))
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(io.LimitReader(resp.Body, 16*1024))
	status := "healthy"

	// Intercept strict credit depletion
	if resp.StatusCode == 402 || resp.StatusCode == 429 {
		bodyStr := string(body)
		// Check for specific credit exhaustion signatures.
		// OpenAI often uses error.code "insufficient_quota".
		// Anthropic uses HTTP 429 but specific error types (e.g. "no credits available", "credit limit reached").
		// xAI uses "spending limit" and "exhausted".
		if strings.Contains(bodyStr, "insufficient_quota") || strings.Contains(bodyStr, "no credits available") || strings.Contains(bodyStr, "credit limit reached") || strings.Contains(bodyStr, "spending limit") || resp.StatusCode == 402 {
			status = "credit_exhausted"
			log.Error("frontier_prober: credit exhaustion detected! Tripping circuit breaker.", slog.Int("status", resp.StatusCode), slog.String("body", bodyStr))
		} else {
			status = "rate_limited"
			log.Warn("frontier_prober: rate limit hit", slog.Int("status", resp.StatusCode), slog.String("body", bodyStr))
		}
	}

	telemetry := extractTelemetryHeaders(resp.Header, fm.Provider, status)

	// Update the global routing policy with the live telemetry
	p.config.GetRoutingPolicy().UpdateTelemetry(fm.Model, telemetry)

	// Update actual spend locally based on the 1-token output
	var usage Usage
	if resp.StatusCode == 200 {
		var data map[string]interface{}
		if err := json.Unmarshal(body, &data); err == nil {
			if u, ok := data["usage"].(map[string]interface{}); ok {
				if in, ok := u["prompt_tokens"].(float64); ok {
					usage.PromptTokens = int(in)
				}
				if out, ok := u["completion_tokens"].(float64); ok {
					usage.CompletionTokens = int(out)
				}
			}
		}
		p.config.GetRoutingPolicy().EnforceSpendBudget(fm.Model, usage)
	}
}

func extractTelemetryHeaders(h http.Header, provider, status string) FrontierTelemetry {
	t := FrontierTelemetry{APIStatus: status}

	if provider == ProviderAnthropic {
		t.LimitTokens = parseHeaderInt(h.Get("anthropic-ratelimit-tokens-limit"))
		t.RemainingTokens = parseHeaderInt(h.Get("anthropic-ratelimit-tokens-remaining"))
		t.LimitRequests = parseHeaderInt(h.Get("anthropic-ratelimit-requests-limit"))
		t.RemainingRequests = parseHeaderInt(h.Get("anthropic-ratelimit-requests-remaining"))
		t.LimitResetAt = h.Get("anthropic-ratelimit-tokens-reset")
	} else {
		// OpenAI compatible headers
		t.LimitTokens = parseHeaderInt(h.Get("x-ratelimit-limit-tokens"))
		t.RemainingTokens = parseHeaderInt(h.Get("x-ratelimit-remaining-tokens"))
		t.LimitRequests = parseHeaderInt(h.Get("x-ratelimit-limit-requests"))
		t.RemainingRequests = parseHeaderInt(h.Get("x-ratelimit-remaining-requests"))
		t.LimitResetAt = h.Get("x-ratelimit-reset-tokens")
	}

	return t
}

func parseHeaderInt(s string) int64 {
	if s == "" {
		return 0
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0
	}
	return v
}
