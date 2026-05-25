// bootstrap.go — Container bootstrap: ES API key minting + OpenBao seeding.
//
// Direct port of Python: docker_bootstrap.py (8 AST nodes).
// Runs as part of `flume start` to:
//   1. Enable OpenBao KV-V2 mount
//   2. Mint an Elasticsearch API key via xpack.security
//   3. Seed the API key into OpenBao KV
//
// In Go, this is called from the CLI entrypoint rather than as a separate
// Python script. The OpenBaoClient already provides EnableKV().
package secrets

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// BootstrapConfig holds the configuration for container bootstrap.
type BootstrapConfig struct {
	ESURL           string
	ESPassword      string
	OpenBaoAddr     string
	OpenBaoToken    string
	WaitSeconds     int
}

// DefaultBootstrapConfig returns bootstrap config from environment.
func DefaultBootstrapConfig() *BootstrapConfig {
	esURL := os.Getenv("ES_URL")
	if esURL == "" {
		esURL = "http://elasticsearch:9200"
	}
	return &BootstrapConfig{
		ESURL:        strings.TrimRight(esURL, "/"),
		ESPassword:   envOrDefault("ELASTIC_PASSWORD", "flume-elastic-pass"),
		OpenBaoAddr:  envOrDefault("OPENBAO_ADDR", "http://openbao:8200"),
		OpenBaoToken: envOrDefault("VAULT_TOKEN", "flume-dev-token"),
		WaitSeconds:  5,
	}
}

// Bootstrap performs the container bootstrap sequence.
// Derived from Python: docker_bootstrap.py __main__ block.
func Bootstrap(ctx context.Context, cfg *BootstrapConfig, logger *slog.Logger) error {
	if logger == nil {
		logger = slog.Default()
	}

	logger.Info("waiting for Elasticsearch and OpenBao to fully bootstrap...",
		slog.Int("wait_seconds", cfg.WaitSeconds))
	time.Sleep(time.Duration(cfg.WaitSeconds) * time.Second)

	// 1. Enable OpenBao KV-V2
	logger.Info("enabling OpenBao KV-V2 engine at secret/...")
	bao := NewOpenBaoClient(cfg.OpenBaoAddr, cfg.OpenBaoToken, logger)
	if err := bao.EnableKV(ctx); err != nil {
		logger.Warn("OpenBao KV-V2 enablement issue (may already exist)",
			slog.String("error", err.Error()))
	}

	// 2. Mint ES API key
	logger.Info("minting dynamic Elasticsearch API Key via xpack.security...")
	esAPIKey, err := mintESAPIKey(ctx, cfg, logger)
	if err != nil {
		return fmt.Errorf("bootstrap: ES API key minting failed: %w", err)
	}
	logger.Info("Elasticsearch API key minted successfully")

	// 3. Seed credentials into OpenBao
	logger.Info("flushing final credentials into OpenBao Vault...")
	seedData := map[string]interface{}{
		"ES_API_KEY":     esAPIKey,
		"OPENAI_API_KEY": os.Getenv("OPENAI_API_KEY"),
	}
	if err := bao.KVPut(ctx, "flume/keys", seedData); err != nil {
		return fmt.Errorf("bootstrap: OpenBao seed failed: %w", err)
	}

	logger.Info("bootstrap complete — ecosystem ready for execution")
	return nil
}

// mintESAPIKey creates an Elasticsearch API key via xpack.security.
// Derived from Python: docker_bootstrap.py call_es("/_security/api_key", ...).
func mintESAPIKey(ctx context.Context, cfg *BootstrapConfig, logger *slog.Logger) (string, error) {
	payload := map[string]interface{}{
		"name": "flume-swarm-key",
		"role_descriptors": map[string]interface{}{
			"flume_role": map[string]interface{}{
				"cluster": []string{"all"},
				"index": []interface{}{
					map[string]interface{}{
						"names":      []string{"*"},
						"privileges": []string{"all"},
					},
				},
			},
		},
	}

	data, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}

	url := fmt.Sprintf("%s/_security/api_key", cfg.ESURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(string(data)))
	if err != nil {
		return "", err
	}

	// Basic auth with elastic:password
	authStr := fmt.Sprintf("elastic:%s", cfg.ESPassword)
	b64Auth := base64.StdEncoding.EncodeToString([]byte(authStr))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Basic "+b64Auth)

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("ES API key request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("ES API key minting failed (HTTP %d): %s", resp.StatusCode, string(body))
	}

	var result struct {
		Encoded string `json:"encoded"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("ES API key decode failed: %w", err)
	}
	return result.Encoded, nil
}

func envOrDefault(key, defaultVal string) string {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	return v
}
