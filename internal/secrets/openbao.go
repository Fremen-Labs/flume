// Package secrets provides OpenBao (Vault) integration and credential management.
//
// Replaces Python:
//   - flume_secrets.py — fetch_openbao_kv, hydrate_secrets_from_openbao (11 nodes)
//   - llm_credentials_store.py — LlmCredential, LlmMetadataDoc, upsert/delete (8 nodes)
//   - ado_tokens_store.py — load_document, save_document, _safe_bao_put (5 nodes)
//   - github_tokens_store.py — load_document, save_document, _safe_bao_put (5 nodes)
//   - openai_oauth_state.py — load_state_from_env_or_file, save_state_to_env_or_file (6 nodes)
//   - es_credential_store.py — ensure_credential_indices (6 nodes)
//
// All secret material is stored exclusively in OpenBao KV-V2.
// Metadata (labels, providers, active status) lives in Elasticsearch.
// API keys never touch disk or environment variables in production.
package secrets

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

// OpenBaoClient wraps the OpenBao (HashiCorp Vault-compatible) HTTP API.
type OpenBaoClient struct {
	addr       string
	token      string
	httpClient *http.Client
	logger     *slog.Logger
}

// NewOpenBaoClient creates a new OpenBao client.
func NewOpenBaoClient(addr, token string, logger *slog.Logger) *OpenBaoClient {
	return &OpenBaoClient{
		addr:  strings.TrimRight(addr, "/"),
		token: token,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		logger: logger,
	}
}

// ─── KV-V2 Operations ──────────────────────────────────────────────────────

// KVGet reads a secret from the KV-V2 engine.
// Derived from Python: flume_secrets.fetch_openbao_kv() (8 nodes)
func (c *OpenBaoClient) KVGet(ctx context.Context, path string) (map[string]interface{}, error) {
	url := fmt.Sprintf("%s/v1/secret/data/%s", c.addr, strings.TrimLeft(path, "/"))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("openbao: request build failed: %w", err)
	}
	req.Header.Set("X-Vault-Token", c.token)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.logger.Error("Error fetching from OpenBao",
			slog.String("path", path),
			slog.String("error", err.Error()))
		return nil, fmt.Errorf("openbao: get %s failed: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 404 {
		return nil, nil // secret doesn't exist
	}
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("openbao: get %s returned HTTP %d: %s", path, resp.StatusCode, string(body))
	}

	var result struct {
		Data struct {
			Data map[string]interface{} `json:"data"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("openbao: decode %s failed: %w", path, err)
	}
	return result.Data.Data, nil
}

// KVPut writes a secret to the KV-V2 engine.
// Derived from Python: _safe_bao_put() in ado_tokens_store.py, github_tokens_store.py
func (c *OpenBaoClient) KVPut(ctx context.Context, path string, data map[string]interface{}) error {
	payload := map[string]interface{}{
		"data": data,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("openbao: marshal failed: %w", err)
	}

	url := fmt.Sprintf("%s/v1/secret/data/%s", c.addr, strings.TrimLeft(path, "/"))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(string(body)))
	if err != nil {
		return fmt.Errorf("openbao: request build failed: %w", err)
	}
	req.Header.Set("X-Vault-Token", c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.logger.Error("OpenBao credential upsert failed",
			slog.String("path", path),
			slog.String("error", err.Error()))
		return fmt.Errorf("openbao: put %s failed: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		c.logger.Error("Failed to write credential to OpenBao",
			slog.String("path", path),
			slog.Int("status", resp.StatusCode),
			slog.String("body", string(respBody)))
		return fmt.Errorf("openbao: put %s returned HTTP %d", path, resp.StatusCode)
	}
	return nil
}

// KVDelete removes a secret from the KV-V2 engine.
func (c *OpenBaoClient) KVDelete(ctx context.Context, path string) error {
	url := fmt.Sprintf("%s/v1/secret/metadata/%s", c.addr, strings.TrimLeft(path, "/"))
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, url, nil)
	if err != nil {
		return fmt.Errorf("openbao: request build failed: %w", err)
	}
	req.Header.Set("X-Vault-Token", c.token)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("openbao: delete %s failed: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 && resp.StatusCode != 404 {
		return fmt.Errorf("openbao: delete %s returned HTTP %d", path, resp.StatusCode)
	}
	return nil
}

// ─── AppRole Authentication ─────────────────────────────────────────────────

// HydrateFromAppRole authenticates with AppRole and retrieves secrets.
// Derived from Python: flume_secrets.hydrate_secrets_from_openbao() (3 nodes)
func (c *OpenBaoClient) HydrateFromAppRole(ctx context.Context, roleID, secretID string) (string, error) {
	payload := fmt.Sprintf(`{"role_id":"%s","secret_id":"%s"}`, roleID, secretID)
	url := fmt.Sprintf("%s/v1/auth/approle/login", c.addr)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(payload))
	if err != nil {
		return "", fmt.Errorf("openbao: approle request failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.logger.Warn("AppRole native fetch failed", slog.String("error", err.Error()))
		return "", fmt.Errorf("openbao: approle login failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return "", fmt.Errorf("openbao: approle login returned HTTP %d", resp.StatusCode)
	}

	var result struct {
		Auth struct {
			ClientToken string `json:"client_token"`
		} `json:"auth"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("openbao: approle decode failed: %w", err)
	}

	c.token = result.Auth.ClientToken
	return c.token, nil
}

// ─── KV-V2 Mount Setup ─────────────────────────────────────────────────────

// EnableKV enables the KV-V2 secrets engine at the default mount path.
// Derived from Python: docker_bootstrap.py module-level (8 nodes)
func (c *OpenBaoClient) EnableKV(ctx context.Context) error {
	payload := `{"type":"kv","options":{"version":"2"}}`
	url := fmt.Sprintf("%s/v1/sys/mounts/secret", c.addr)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(payload))
	if err != nil {
		return fmt.Errorf("openbao: mount request failed: %w", err)
	}
	req.Header.Set("X-Vault-Token", c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("openbao: mount request failed: %w", err)
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode == 204 || resp.StatusCode == 200:
		c.logger.Info("OpenBao KV-V2 engine enabled at secret/")
	case resp.StatusCode == 400:
		// Already mounted — not an error
		c.logger.Debug("OpenBao KV-V2 mount already exists (HTTP 400) — skipping")
	default:
		return fmt.Errorf("openbao: enable KV returned HTTP %d", resp.StatusCode)
	}
	return nil
}

// Ping checks if OpenBao is reachable.
func (c *OpenBaoClient) Ping(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	url := fmt.Sprintf("%s/v1/sys/health", c.addr)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("openbao: ping request failed: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("openbao: ping failed: %w", err)
	}
	defer resp.Body.Close()

	// 200 = initialized+unsealed, 429 = unsealed+standby, 472 = DR mode
	// 501 = uninitialized, 503 = sealed
	if resp.StatusCode == 501 || resp.StatusCode == 503 {
		return fmt.Errorf("openbao: not ready (HTTP %d)", resp.StatusCode)
	}
	return nil
}
