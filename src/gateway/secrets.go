package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// OpenBao KV integration for secret retrieval.
//
// API keys are stored at: secret/data/flume/llm_credentials/{id}
// ADO tokens are stored at: secret/data/flume/ado_tokens/{id}
//
// Secrets are cached in-memory with a configurable TTL (default: 60s).
// Secret values are NEVER logged — only key names appear in audit trails.
// ─────────────────────────────────────────────────────────────────────────────

// SecretStore handles OpenBao KV reads with in-memory caching.
type SecretStore struct {
	mu         sync.RWMutex
	cache      map[string]cachedSecret
	addr       string
	token      string
	cacheTTL   time.Duration
	httpClient *http.Client
	esURL      string // for audit logging
}

type cachedSecret struct {
	value     string
	expiresAt time.Time
}

// NewSecretStore creates a SecretStore pointed at the given OpenBao address.
func NewSecretStore(addr, token, esURL string, cacheTTL time.Duration) *SecretStore {
	if addr == "" {
		addr = os.Getenv("OPENBAO_ADDR")
	}
	if addr == "" {
		addr = "http://openbao:8200"
	}
	if token == "" {
		token = os.Getenv("OPENBAO_TOKEN")
	}
	if esURL == "" {
		esURL = os.Getenv("ES_URL")
	}
	if cacheTTL == 0 {
		cacheTTL = 60 * time.Second
	}
	return &SecretStore{
		cache:      make(map[string]cachedSecret),
		addr:       strings.TrimRight(addr, "/"),
		token:      token,
		cacheTTL:   cacheTTL,
		httpClient: &http.Client{Timeout: 5 * time.Second},
		esURL:      strings.TrimRight(esURL, "/"),
	}
}

// GetLLMKey retrieves an API key for the given credential ID.
// Returns empty string if not found or OpenBao is unreachable.
func (s *SecretStore) GetLLMKey(ctx context.Context, credentialID string) string {
	cacheKey := "llm_cred:" + credentialID
	if v, ok := s.getCached(cacheKey); ok {
		return v
	}

	log := WithContext(ctx)
	path := fmt.Sprintf("secret/data/flume/llm_credentials/%s", credentialID)
	data, err := s.readKV(ctx, path)
	if err != nil {
		log.Warn("failed to read LLM credential from OpenBao",
			slog.String("credential_id", credentialID),
			slog.String("error", err.Error()),
		)
		return ""
	}

	key := ""
	// Backwards compatibility for legacy monolithic structure:
	if v, ok := data[fmt.Sprintf("FLUME_CRED_%s", credentialID)].(string); ok {
		key = v
	}

	// Primary structure from direct LLM credentials path:
	if key == "" {
		if v, ok := data["api_key"].(string); ok {
			key = v
		}
	}

	if key != "" {
		s.setCache(cacheKey, key)
		s.auditAccess(ctx, path, data)
	}

	return key
}

// GetGlobalSecrets retrieves all secrets from the main flume/keys path.
// This is the equivalent of hydrate_secrets_from_openbao() in Python.
func (s *SecretStore) GetGlobalSecrets(ctx context.Context) map[string]string {
	cacheKey := "global:flume/keys"
	// Global secrets are fetched once and cached
	s.mu.RLock()
	if entry, ok := s.cache[cacheKey]; ok && time.Now().Before(entry.expiresAt) {
		s.mu.RUnlock()
		// Return the marker — actual values are in individual cache entries
		return nil
	}
	s.mu.RUnlock()

	log := WithContext(ctx)
	data, err := s.readKV(ctx, "secret/data/flume/keys")
	if err != nil {
		log.Warn("failed to read global secrets from OpenBao",
			slog.String("error", err.Error()),
		)
		return nil
	}

	result := make(map[string]string)
	for k, v := range data {
		if sv, ok := v.(string); ok && strings.TrimSpace(sv) != "" {
			result[k] = strings.TrimSpace(sv)
			s.setCache("global:"+k, sv)
		}
	}

	// Mark the global fetch as done
	s.setCache(cacheKey, "fetched")
	s.auditAccess(ctx, "secret/data/flume/keys", data)

	log.Info("hydrated global secrets from OpenBao",
		slog.Int("secrets_hydrated", len(result)),
	)

	return result
}

// GetGlobalValue retrieves a single global secret value by key name.
func (s *SecretStore) GetGlobalValue(ctx context.Context, key string) string {
	cacheKey := "global:" + key
	if v, ok := s.getCached(cacheKey); ok {
		return v
	}
	// If not cached individually, try a full global fetch
	s.GetGlobalSecrets(ctx)
	if v, ok := s.getCached(cacheKey); ok {
		return v
	}
	return ""
}

// ─────────────────────────────────────────────────────────────────────────────
// Internal helpers
// ─────────────────────────────────────────────────────────────────────────────

func (s *SecretStore) getCached(key string) (string, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entry, ok := s.cache[key]
	if !ok || time.Now().After(entry.expiresAt) {
		return "", false
	}
	return entry.value, true
}

func (s *SecretStore) setCache(key, value string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cache[key] = cachedSecret{
		value:     value,
		expiresAt: time.Now().Add(s.cacheTTL),
	}
}

func (s *SecretStore) readKV(ctx context.Context, path string) (map[string]interface{}, error) {
	if s.token == "" {
		return nil, fmt.Errorf("OPENBAO_TOKEN is empty")
	}

	url := fmt.Sprintf("%s/v1/%s", s.addr, path)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("X-Vault-Token", s.token)

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("openbao request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("secret not found: %s", path)
	}
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}

	var result struct {
		Data struct {
			Data map[string]interface{} `json:"data"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}

	if result.Data.Data == nil {
		return nil, fmt.Errorf("empty data at %s", path)
	}

	return result.Data.Data, nil
}

// auditAccess writes a security audit record to Elasticsearch.
// Only logs key NAMES, never values.
func (s *SecretStore) auditAccess(ctx context.Context, secretPath string, data map[string]interface{}) {
	if s.esURL == "" {
		return
	}

	keys := make([]string, 0, len(data))
	for k := range data {
		keys = append(keys, k)
	}

	doc := map[string]interface{}{
		"@timestamp":     time.Now().UTC().Format(time.RFC3339),
		"message":        fmt.Sprintf("OpenBao KV accessed at %s", secretPath),
		"service":        "flume-gateway",
		"secret_path":    secretPath,
		"keys_retrieved": keys,
	}

	body, err := json.Marshal(doc)
	if err != nil {
		return
	}

	url := s.esURL + "/agent-security-audits/_doc"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(string(body)))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")

	apiKey := os.Getenv("ES_API_KEY")
	if apiKey != "" {
		req.Header.Set("Authorization", "ApiKey "+apiKey)
	} else if esPass := os.Getenv("FLUME_ELASTIC_PASSWORD"); esPass != "" {
		req.SetBasicAuth("elastic", esPass)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		WithContext(ctx).Debug("audit log write failed", slog.String("error", err.Error()))
		return
	}
	resp.Body.Close()
}
