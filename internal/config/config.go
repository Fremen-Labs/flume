// Package config provides typed Flume configuration management.
//
// Replaces Python: flume_secrets.FlumeSettings (BaseSettings)
// LogLoom AST source: flume_secrets module (11 nodes)
//
// Configuration is resolved in priority order:
//  1. Environment variables (highest priority)
//  2. Elasticsearch flume-settings document
//  3. Compiled defaults (lowest priority)
package config

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Config holds all Flume runtime configuration.
// Mirrors Python's FlumeSettings(BaseSettings) from flume_secrets.py.
type Config struct {
	// ── LLM ──────────────────────────────────────────────────
	LLMProvider string `json:"llm_provider"`
	LLMModel    string `json:"llm_model"`
	LLMBaseURL  string `json:"llm_base_url"`
	LLMAPIKey   string `json:"llm_api_key"`

	// ── Git ──────────────────────────────────────────────────
	GitUserName  string `json:"git_user_name"`
	GitUserEmail string `json:"git_user_email"`

	// ── Elasticsearch ────────────────────────────────────────
	ESURL       string `json:"es_url"`
	ESAPIKey    string `json:"es_api_key"`
	ESVerifyTLS bool   `json:"es_verify_tls"`

	// ── OpenBao (Vault) ──────────────────────────────────────
	OpenBaoAddr  string `json:"openbao_addr"`
	OpenBaoToken string `json:"openbao_token"`

	// ── Dashboard ────────────────────────────────────────────
	DashboardHost string `json:"dashboard_host"`
	DashboardPort int    `json:"dashboard_port"`

	// ── Worker Manager ───────────────────────────────────────
	WorkerManagerPollSeconds int `json:"worker_manager_poll_seconds"`
	WorkersPerRole           int `json:"workers_per_role"` // 0 = auto

	// ── Runtime Flags ────────────────────────────────────────
	NativeMode bool `json:"native_mode"` // FLUME_NATIVE_MODE=1
}

// DefaultConfig returns a Config with compiled defaults matching
// Python's FlumeSettings field defaults.
func DefaultConfig() *Config {
	native := os.Getenv("FLUME_NATIVE_MODE") == "1"

	esURL := "http://elasticsearch:9200"
	openBaoAddr := "http://openbao:8200"
	if native {
		esURL = "http://localhost:9200"
		openBaoAddr = "http://localhost:8200"
	}
	if envES := os.Getenv("ES_URL"); envES != "" {
		esURL = strings.TrimRight(envES, "/")
	}

	return &Config{
		LLMProvider:              "ollama",
		LLMModel:                 "llama3.2",
		LLMBaseURL:               "http://host.docker.internal:11434",
		LLMAPIKey:                "",
		GitUserName:              "FlumeAgent",
		GitUserEmail:             "agent@flume.local",
		ESURL:                    esURL,
		ESAPIKey:                 "",
		ESVerifyTLS:              false,
		OpenBaoAddr:              openBaoAddr,
		OpenBaoToken:             "",
		DashboardHost:            "0.0.0.0",
		DashboardPort:            8765,
		WorkerManagerPollSeconds: 2,
		WorkersPerRole:           0,
		NativeMode:               native,
	}
}

// envKeys maps Config field names → environment variable names.
// Mirrors Python's FLUME_ENV_KEYS frozenset.
var envKeys = map[string]string{
	"LLMProvider":              "LLM_PROVIDER",
	"LLMModel":                 "LLM_MODEL",
	"LLMBaseURL":               "LLM_BASE_URL",
	"LLMAPIKey":                "LLM_API_KEY",
	"GitUserName":              "GIT_USER_NAME",
	"GitUserEmail":             "GIT_USER_EMAIL",
	"ESURL":                    "ES_URL",
	"ESAPIKey":                 "ES_API_KEY",
	"ESVerifyTLS":              "ES_VERIFY_TLS",
	"OpenBaoAddr":              "OPENBAO_ADDR",
	"OpenBaoToken":             "OPENBAO_TOKEN",
	"DashboardHost":            "DASHBOARD_HOST",
	"DashboardPort":            "DASHBOARD_PORT",
	"WorkerManagerPollSeconds": "WORKER_MANAGER_POLL_SECONDS",
	"WorkersPerRole":           "WORKERS_PER_ROLE",
}

// OverlayFromEnv applies environment variable overrides to the config.
// String fields are overwritten if the env var is non-empty.
// Int fields are parsed; invalid values are silently ignored.
func (c *Config) OverlayFromEnv() {
	if v := os.Getenv("LLM_PROVIDER"); v != "" {
		c.LLMProvider = v
	}
	if v := os.Getenv("LLM_MODEL"); v != "" {
		c.LLMModel = v
	}
	if v := os.Getenv("LLM_BASE_URL"); v != "" {
		c.LLMBaseURL = v
	}
	if v := os.Getenv("LLM_API_KEY"); v != "" {
		c.LLMAPIKey = v
	}
	if v := os.Getenv("GIT_USER_NAME"); v != "" {
		c.GitUserName = v
	}
	if v := os.Getenv("GIT_USER_EMAIL"); v != "" {
		c.GitUserEmail = v
	}
	if v := os.Getenv("ES_URL"); v != "" {
		c.ESURL = strings.TrimRight(v, "/")
	}
	if v := os.Getenv("ES_API_KEY"); v != "" {
		c.ESAPIKey = v
	}
	if v := os.Getenv("ES_VERIFY_TLS"); v != "" {
		c.ESVerifyTLS = v == "true" || v == "1"
	}
	if v := os.Getenv("OPENBAO_ADDR"); v != "" {
		c.OpenBaoAddr = v
	}
	if v := os.Getenv("OPENBAO_TOKEN"); v != "" {
		c.OpenBaoToken = v
	}
	if v := os.Getenv("DASHBOARD_HOST"); v != "" {
		c.DashboardHost = v
	}
	if v := os.Getenv("DASHBOARD_PORT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			c.DashboardPort = n
		}
	}
	if v := os.Getenv("WORKER_MANAGER_POLL_SECONDS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			c.WorkerManagerPollSeconds = n
		}
	}
	if v := os.Getenv("WORKERS_PER_ROLE"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			c.WorkersPerRole = n
		}
	}
	c.NativeMode = os.Getenv("FLUME_NATIVE_MODE") == "1"
}

// OverlayFromES attempts to read configuration from Elasticsearch.
// Mirrors Python's load_elastic_config() from flume_secrets.py.
// Returns silently if ES is unreachable — env vars always take precedence.
func (c *Config) OverlayFromES(ctx context.Context, logger *slog.Logger) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	url := fmt.Sprintf("%s/flume-settings/_doc/system", c.ESURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return
	}
	if c.ESAPIKey != "" {
		req.Header.Set("Authorization", "ApiKey "+c.ESAPIKey)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		logger.Debug("ES config unreachable, using env defaults", slog.String("error", err.Error()))
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode == 404 {
		logger.Debug("flume-settings not found, using environment defaults")
		return
	}
	if resp.StatusCode != 200 {
		logger.Warn("Failed to bootstrap configuration from Elasticsearch",
			slog.Int("status", resp.StatusCode))
		return
	}

	var esDoc struct {
		Source struct {
			ESURL      string `json:"es_url"`
			ESAPIKey   string `json:"es_api_key"`
			OpenBaoURL string `json:"openbao_url"`
		} `json:"_source"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&esDoc); err != nil {
		logger.Warn("Failed to parse ES config document", slog.String("error", err.Error()))
		return
	}

	// Only apply if values are meaningful
	if esDoc.Source.ESURL != "" {
		c.ESURL = esDoc.Source.ESURL
	}
	if esDoc.Source.ESAPIKey != "" && esDoc.Source.ESAPIKey != "***" {
		c.ESAPIKey = esDoc.Source.ESAPIKey
	}
	if esDoc.Source.OpenBaoURL != "" {
		c.OpenBaoAddr = esDoc.Source.OpenBaoURL
	}

	// Native mode: rewrite container hostnames to localhost
	if c.NativeMode && strings.Contains(c.ESURL, "elasticsearch") {
		c.ESURL = strings.Replace(c.ESURL, "elasticsearch", "localhost", 1)
	}
}

// ─── Singleton ──────────────────────────────────────────────────────────────

var (
	globalConfig *Config
	configOnce   sync.Once
)

// Load initializes the global configuration singleton.
// Safe to call multiple times; only the first call has effect.
func Load(ctx context.Context, logger *slog.Logger) *Config {
	configOnce.Do(func() {
		globalConfig = DefaultConfig()
		globalConfig.OverlayFromES(ctx, logger)
		globalConfig.OverlayFromEnv() // Env always wins
	})
	return globalConfig
}

// Get returns the global config. Panics if Load() hasn't been called.
func Get() *Config {
	if globalConfig == nil {
		panic("config.Load() must be called before config.Get()")
	}
	return globalConfig
}

// RewriteLoopbackForDocker replaces 127.0.0.1/localhost with
// host.docker.internal when running inside Docker.
// Mirrors gateway.rewriteLoopbackForDocker and
// Python workspace_llm_env._rewrite_loopback_for_docker.
func RewriteLoopbackForDocker(url string) string {
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
			return strings.Replace(url, loopback, "://host.docker.internal", 1)
		}
	}
	return url
}
