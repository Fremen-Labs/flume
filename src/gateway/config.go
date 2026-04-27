package gateway

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// rewriteLoopbackForDocker replaces 127.0.0.1/localhost with host.docker.internal
// when the gateway is running inside a Docker container. This mirrors the same
// fix that Python workers apply via workspace_llm_env._rewrite_loopback_for_docker.
//
// Dashboard users naturally enter "http://127.0.0.1:11434" in Settings because
// it's correct from their Mac. From inside Docker, 127.0.0.1 refers to the
// container's own loopback — connection refused — not the Mac host running Ollama.
//
// Detection: /.dockerenv is created by the Docker runtime on all containers.
// FLUME_NATIVE_MODE=1 disables the rewrite for bare-metal development.
func rewriteLoopbackForDocker(url string) string {
	if url == "" {
		return url
	}
	if os.Getenv("FLUME_NATIVE_MODE") == "1" {
		return url
	}
	if _, err := os.Stat("/.dockerenv"); err != nil {
		return url // not in a container
	}
	for _, loopback := range []string{"://127.0.0.1", "://localhost"} {
		if strings.Contains(url, loopback) {
			rewritten := strings.Replace(url, loopback, "://host.docker.internal", 1)
			Log().Info("config: rewrote loopback Ollama URL for Docker",
				slog.String("original", url),
				slog.String("rewritten", rewritten),
			)
			return rewritten
		}
	}
	return url
}

// ─────────────────────────────────────────────────────────────────────────────
// Elasticsearch configuration loader with TTL cache and multi-model routing.
//
// Reads:
//   - flume-llm-config       → global LLM provider/model/baseUrl
//   - flume-llm-credentials  → credential metadata (secrets in OpenBao)
//   - flume-agent-models     → per-role model overrides
//
// All reads are cached with a configurable TTL to avoid hammering ES.
// ─────────────────────────────────────────────────────────────────────────────

// Config holds the gateway's resolved configuration state.
type Config struct {
	mu sync.RWMutex

	// System settings (from flume-settings)
	PrometheusEnabled bool

	// Global LLM defaults (from flume-llm-config)
	DefaultProvider string
	DefaultModel    string
	DefaultBaseURL  string

	// Ensemble configuration (from flume-llm-config)
	EnsembleEnabled       bool
	EnsembleSize          int
	EnsembleTimeout       time.Duration // default 90s, guards both jury + frontier
	FrontierFallbackModel string

	// Per-role model overrides (from flume-agent-models)
	AgentModels map[string]AgentModelConfig

	// Credential metadata cache (from flume-llm-credentials)
	Credentials map[string]CredentialMeta

	// Timing
	lastRefresh time.Time
	cacheTTL    time.Duration
	esURL       string
	httpClient  *http.Client

	// Singleflight guard: prevents thundering-herd ES fetches when cache expires
	// under concurrent load. 0 = idle, 1 = refresh in progress.
	refreshing atomic.Int32

	// Routing policy (from flume-routing-policy)
	RoutingPolicy *RoutingPolicy
}

// CredentialMeta holds non-secret metadata about a saved LLM credential.
type CredentialMeta struct {
	ID       string `json:"id"`
	Label    string `json:"label"`
	Provider string `json:"provider"`
	BaseURL  string `json:"baseUrl"`
	HasKey   bool   `json:"hasKey"`
}

// NewConfig creates a Config with the given ES URL and cache TTL.
func NewConfig(esURL string, cacheTTL time.Duration) *Config {
	if esURL == "" {
		esURL = os.Getenv("ES_URL")
	}
	if esURL == "" {
		esURL = "http://elasticsearch:9200"
	}
	if cacheTTL == 0 {
		cacheTTL = 5 * time.Second
	}
	return &Config{
		PrometheusEnabled: true,
		AgentModels:     make(map[string]AgentModelConfig),
		Credentials:     make(map[string]CredentialMeta),
		cacheTTL:        cacheTTL,
		esURL:           strings.TrimRight(esURL, "/"),
		httpClient: &http.Client{
			Timeout: 3 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
			},
		},
		EnsembleTimeout: 90 * time.Second,
	}
}

// Refresh reloads configuration from Elasticsearch if the cache has expired.
// A singleflight guard ensures that at most one goroutine performs the ES fetch
// when the cache expires under concurrent load; other callers return immediately
// (they will serve the previous values until the refresh completes).
func (c *Config) Refresh(ctx context.Context) {
	c.mu.RLock()
	fresh := time.Since(c.lastRefresh) < c.cacheTTL
	c.mu.RUnlock()
	if fresh {
		return
	}

	// Only one goroutine performs the refresh; others bail out immediately.
	// They continue with stale-but-valid config until the refresh lands.
	if !c.refreshing.CompareAndSwap(0, 1) {
		return
	}
	defer c.refreshing.Store(0)

	c.mu.Lock()
	defer c.mu.Unlock()

	// Double-check after acquiring write lock (another goroutine may have just finished)
	if time.Since(c.lastRefresh) < c.cacheTTL {
		return
	}

	log := WithContext(ctx)
	defer LogDuration(ctx, "config_refresh")()

	c.loadSystemConfig(ctx, log)
	c.loadGlobalConfig(ctx, log)
	c.loadAgentModels(ctx, log)
	c.loadCredentials(ctx, log)
	c.loadRoutingPolicy(ctx, log)

	c.lastRefresh = time.Now()
	log.Debug("configuration refreshed",
		slog.String("provider", c.DefaultProvider),
		slog.String("model", c.DefaultModel),
		slog.Int("agent_models", len(c.AgentModels)),
		slog.Int("credentials", len(c.Credentials)),
	)
}

// ResolveModel determines the effective model + provider + credentialID for a
// request, applying agent-role overrides where configured.
func (c *Config) ResolveModel(req *ChatRequest) (model, provider, credID string) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	model = req.Model
	provider = req.Provider
	credID = req.CredentialID

	// Apply agent-role override if the request specifies a role and a mapping exists
	if req.AgentRole != "" {
		if override, ok := c.AgentModels[strings.ToLower(req.AgentRole)]; ok {
			if override.Model != "" && model == "" {
				model = override.Model
			}
			if override.Provider != "" && provider == "" {
				provider = override.Provider
			}
			if override.CredentialID != "" && credID == "" {
				credID = override.CredentialID
			}
		}
	}

	// Fallback to global defaults
	if model == "" {
		model = c.DefaultModel
	}
	if model == "" {
		model = os.Getenv("LLM_MODEL")
	}
	if model == "" {
		model = "llama3.2"
	}

	if provider == "" {
		provider = c.DefaultProvider
	}
	if provider == "" {
		provider = os.Getenv("LLM_PROVIDER")
	}
	if provider == "" {
		provider = ProviderOllama
	}

	provider = strings.ToLower(provider)

	// Normalize Gemini model aliases
	if provider == ProviderGemini {
		model = NormalizeGeminiModel(model)
	}

	return model, provider, credID
}

// GetBaseURL returns the effective base URL for a provider.
func (c *Config) GetBaseURL(provider string) string {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if c.DefaultBaseURL != "" && (provider == c.DefaultProvider || provider == ProviderOllama) {
		return c.DefaultBaseURL
	}
	if url, ok := ProviderBaseURLs[provider]; ok {
		return url
	}
	return os.Getenv("LLM_BASE_URL")
}

// GetOllamaBaseURL returns the Ollama-specific base URL, stripping any /v1 suffix.
func (c *Config) GetOllamaBaseURL() string {
	raw := c.GetBaseURL(ProviderOllama)
	if raw == "" {
		raw = os.Getenv("LOCAL_OLLAMA_BASE_URL")
	}
	if raw == "" {
		raw = "http://host.docker.internal:11434"
	}
	raw = strings.TrimRight(raw, "/")
	raw = strings.TrimSuffix(raw, "/v1")
	return raw
}

// IsPrometheusEnabled safely reads the current Prometheus toggled state.
func (c *Config) IsPrometheusEnabled() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.PrometheusEnabled
}

// ShouldThink returns whether thinking should be enabled for a request.
func (c *Config) ShouldThink(req *ChatRequest) bool {
	if req.Think {
		return true
	}
	// Check agent-role override
	c.mu.RLock()
	defer c.mu.RUnlock()
	if req.AgentRole != "" {
		if override, ok := c.AgentModels[strings.ToLower(req.AgentRole)]; ok {
			return override.Think
		}
	}
	return false
}

// GetRoutingPolicy returns the current routing policy, thread-safe.
// Returns DefaultRoutingPolicy() if no policy has been loaded yet.
func (c *Config) GetRoutingPolicy() *RoutingPolicy {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.RoutingPolicy == nil {
		return DefaultRoutingPolicy()
	}
	return c.RoutingPolicy
}

// loadRoutingPolicy loads the routing policy from ES.
func (c *Config) loadRoutingPolicy(ctx context.Context, log *slog.Logger) {
	policy := LoadRoutingPolicyFromES(ctx, c.esURL, c.httpClient)
	c.RoutingPolicy = policy
}

// IsKnownModel returns true if the model name is found in the static deployment
// configuration (e.g. DefaultModel, FrontierFallbackModel, or an Agent override).
func (c *Config) IsKnownModel(model string) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if model == "" || model == c.DefaultModel || model == c.FrontierFallbackModel {
		return true
	}
	if model == os.Getenv("LLM_MODEL") || model == "llama3.2" || model == "gpt-4o" {
		return true
	}
	for _, override := range c.AgentModels {
		if override.Model == model {
			return true
		}
	}
	return false
}

// ─────────────────────────────────────────────────────────────────────────────
// ES fetchers (private)
// ─────────────────────────────────────────────────────────────────────────────

func (c *Config) loadGlobalConfig(ctx context.Context, log *slog.Logger) {
	body, err := c.esGet(ctx, "/flume-llm-config/_doc/singleton")
	if err != nil {
		// Index may not exist yet or singleton not written — that's fine, falls back to .env
		log.Debug("flume-llm-config not found, using global defaults", slog.String("error", err.Error()))
		return
	}
	src, ok := extractSource(body)
	if !ok {
		return
	}
	if v, ok := src["LLM_PROVIDER"].(string); ok && v != "" {
		c.DefaultProvider = strings.TrimSpace(v)
	}
	if v, ok := src["LLM_MODEL"].(string); ok && v != "" {
		c.DefaultModel = strings.TrimSpace(v)
	}
	if v, ok := src["LLM_BASE_URL"].(string); ok && v != "" {
		c.DefaultBaseURL = rewriteLoopbackForDocker(strings.TrimSpace(v))
	}
	if enabled, ok := src["ENSEMBLE_ENABLED"].(bool); ok {
		c.EnsembleEnabled = enabled
	}
	if sizeRaw, ok := src["ENSEMBLE_SIZE"].(float64); ok {
		c.EnsembleSize = int(sizeRaw)
	} else {
		c.EnsembleSize = 2 // default
	}
	if timeoutSecs, ok := src["ENSEMBLE_TIMEOUT_SECONDS"].(float64); ok && timeoutSecs > 0 {
		c.EnsembleTimeout = time.Duration(timeoutSecs) * time.Second
	} else if c.EnsembleTimeout == 0 {
		c.EnsembleTimeout = 90 * time.Second
	}
	if fallback, ok := src["FRONTIER_FALLBACK_MODEL"].(string); ok && fallback != "" {
		c.FrontierFallbackModel = strings.TrimSpace(fallback)
	} else if c.FrontierFallbackModel == "" {
		c.FrontierFallbackModel = "gpt-4o"
	}
}

func (c *Config) loadSystemConfig(ctx context.Context, log *slog.Logger) {
	body, err := c.esGet(ctx, "/flume-settings/_doc/system")
	if err != nil {
		log.Debug("flume-settings system doc not found, using defaults", slog.String("error", err.Error()))
		return
	}
	src, ok := extractSource(body)
	if !ok {
		return
	}
	
	if enabled, ok := src["prometheus_enabled"].(bool); ok {
		c.PrometheusEnabled = enabled
	} else if enabledStr, ok := src["prometheus_enabled"].(string); ok {
		c.PrometheusEnabled = strings.ToLower(enabledStr) == "true"
	}
}

func (c *Config) loadAgentModels(ctx context.Context, log *slog.Logger) {
	body, err := c.esGet(ctx, "/flume-agent-models/_doc/singleton")
	if err != nil {
		// Index may not exist yet — that's fine, role overrides are optional
		log.Debug("flume-agent-models not found, using global defaults",
			slog.String("error", err.Error()))
		return
	}
	src, ok := extractSource(body)
	if !ok {
		return
	}
	rolesRaw, ok := src["roles"]
	if !ok {
		return
	}
	rolesBytes, err := json.Marshal(rolesRaw)
	if err != nil {
		log.Warn("failed to marshal agent-models roles", slog.String("error", err.Error()))
		return
	}
	var roles []AgentModelConfig
	if err := json.Unmarshal(rolesBytes, &roles); err != nil {
		log.Warn("failed to parse agent-models roles", slog.String("error", err.Error()))
		return
	}
	newMap := make(map[string]AgentModelConfig, len(roles))
	for _, r := range roles {
		key := strings.ToLower(strings.TrimSpace(r.Role))
		if key != "" {
			newMap[key] = r
		}
	}
	c.AgentModels = newMap
	log.Info("loaded agent model overrides",
		slog.Int("count", len(newMap)),
	)
}

func (c *Config) loadCredentials(ctx context.Context, log *slog.Logger) {
	body, err := c.esGet(ctx, "/flume-llm-credentials/_doc/singleton")
	if err != nil {
		// Index may not exist yet or singleton not written — that's fine, falls back to local/OpenBao
		log.Debug("flume-llm-credentials not found, using global defaults", slog.String("error", err.Error()))
		return
	}
	src, ok := extractSource(body)
	if !ok {
		return
	}
	credsRaw, ok := src["credentials"]
	if !ok {
		return
	}
	credsBytes, err := json.Marshal(credsRaw)
	if err != nil {
		log.Warn("failed to marshal credential metadata", slog.String("error", err.Error()))
		return
	}
	var creds []CredentialMeta
	if err := json.Unmarshal(credsBytes, &creds); err != nil {
		log.Warn("failed to parse credential metadata", slog.String("error", err.Error()))
		return
	}
	newMap := make(map[string]CredentialMeta, len(creds))
	for _, cr := range creds {
		if cr.ID != "" {
			newMap[cr.ID] = cr
		}
	}
	c.Credentials = newMap
}

func (c *Config) esGet(ctx context.Context, path string) (map[string]interface{}, error) {
	url := c.esURL + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	apiKey := os.Getenv("ES_API_KEY")
	if apiKey != "" && !strings.Contains(apiKey, "bypass") {
		req.Header.Set("Authorization", "ApiKey "+apiKey)
	} else if esPass := os.Getenv("FLUME_ELASTIC_PASSWORD"); esPass != "" {
		req.SetBasicAuth("elastic", esPass)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("es request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("not found: %s", path)
	}
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}
	return result, nil
}

// extractSource pulls the _source field from an ES GET response.
func extractSource(body map[string]interface{}) (map[string]interface{}, bool) {
	if body == nil {
		return nil, false
	}
	found, _ := body["found"].(bool)
	if !found {
		return nil, false
	}
	src, ok := body["_source"].(map[string]interface{})
	return src, ok
}

// EnsureAgentModelsIndex verifies the flume-agent-models index exists.
//
// Index creation is centralized in the CLI `flume start` orchestrator.
// This function only performs a HEAD check and logs a warning if the index
// is missing, allowing operators to diagnose boot sequence issues.
func (c *Config) EnsureAgentModelsIndex(ctx context.Context) error {
	url := c.esURL + "/flume-agent-models"
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode == 200 {
		return nil // index exists
	}

	Log().Warn("flume-agent-models index not found — expected to be pre-created by `flume start`")
	return fmt.Errorf("flume-agent-models index missing — run `flume start` to bootstrap")
}
