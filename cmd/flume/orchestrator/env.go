package orchestrator

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/charmbracelet/log"
)

// NodeConfigEntry is a node collected during the CLI interactive wizard.
type NodeConfigEntry struct {
	ID       string
	Host     string  // IP or DNS name (without port)
	Port     string  // default "11434"
	ModelTag string
	MemoryGB float64
}

// CloudProviderEntry represents a native unified Cloud API provider binding
type CloudProviderEntry struct {
	Provider string
	Model    string
	APIKey   string
}

type EnvConfig struct {
	Provider           string
	APIKey             string
	BaseURL            string
	LocalOllamaBaseURL string
	Host               string
	AdminToken         string
	ElasticPassword    string
	Model              string
	IsNative           bool

	ExternalElastic bool
	ESUrl           string
	ESVerifyTLS     bool

	RepoType    string // "github" or "ado"
	GithubToken string
	ADOOrg      string
	ADOProject  string
	ADOToken    string

	CloudProviders []CloudProviderEntry
	Nodes          []NodeConfigEntry // Collected during node mesh wizard
}

// GenerateAdminToken creates a 256-bit cryptographically secure token.
func GenerateAdminToken() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return "flume_adm_" + hex.EncodeToString(bytes), nil
}

// GenerateElasticPassword creates a secure, deterministic password for the internal Elasticsearch 'elastic' user.
func GenerateElasticPassword() (string, error) {
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return "flume_es_" + hex.EncodeToString(bytes), nil
}

// RewriteLoopbackForDockerEnv replaces 127.0.0.1 / localhost with
// host.docker.internal for Docker container accessibility.
func RewriteLoopbackForDockerEnv(url string) string {
	if url == "" {
		return url
	}
	url = strings.ReplaceAll(url, "://127.0.0.1", "://host.docker.internal")
	url = strings.ReplaceAll(url, "://localhost", "://host.docker.internal")
	return url
}

// GenerateEnv dynamically builds the topology map consumed by docker-compose natively in-memory.
func GenerateEnv(config EnvConfig) []string {
	log.Debug("Constructing ecosystem telemetry environment purely via memory arrays natively...")

	var env []string

	if config.AdminToken != "" {
		env = append(env, "FLUME_ADMIN_TOKEN="+config.AdminToken)
	}
	if config.ElasticPassword != "" {
		env = append(env, "FLUME_ELASTIC_PASSWORD="+config.ElasticPassword)
	}

	if config.Provider != "" {
		env = append(env, "LLM_PROVIDER="+config.Provider)
	}

	baseURL := config.BaseURL
	ollamaURL := config.LocalOllamaBaseURL
	if !config.IsNative {
		baseURL = RewriteLoopbackForDockerEnv(baseURL)
		ollamaURL = RewriteLoopbackForDockerEnv(ollamaURL)
	}

	if baseURL != "" {
		env = append(env, "LLM_BASE_URL="+baseURL)
	}
	if ollamaURL != "" {
		env = append(env, "LOCAL_OLLAMA_BASE_URL="+ollamaURL)
	}
	if config.Host != "" {
		env = append(env, "LLM_HOST="+config.Host)
	}
	if strings.TrimSpace(config.APIKey) != "" {
		env = append(env, "LLM_API_KEY="+strings.TrimSpace(config.APIKey))
	}
	if config.Model != "" {
		env = append(env, "LLM_MODEL="+config.Model)
	}
	if config.ExternalElastic && config.ESUrl != "" {
		env = append(env, "FLUME_ES_URL="+config.ESUrl)
		env = append(env, "ES_URL="+config.ESUrl)
	}
	if config.ESVerifyTLS {
		env = append(env, "ES_VERIFY_TLS=true")
	} else {
		env = append(env, "ES_VERIFY_TLS=false")
	}

	if config.RepoType == "github" && config.GithubToken != "" {
		env = append(env, "GITHUB_TOKEN="+config.GithubToken)
	} else if config.RepoType == "ado" {
		if config.ADOOrg != "" {
			env = append(env, "ADO_ORGANIZATION="+config.ADOOrg)
		}
		if config.ADOToken != "" {
			env = append(env, "ADO_PERSONAL_ACCESS_TOKEN="+config.ADOToken)
		}
	}

	log.Info("Successfully compiled telemetry footprint securely in RAM bounds.", "isolation", "Memory-only variables injected into subprocess securely")
	return env
}

// NodeSeedEntry represents a node to register with the Gateway API.
type NodeSeedEntry struct {
	ID       string  `json:"id"`
	Host     string  `json:"host"`
	ModelTag string  `json:"model_tag"`
	Capabilities struct {
		MemoryGB       float64 `json:"memory_gb"`
		ReasoningScore int     `json:"reasoning_score"`
		MaxContext     int     `json:"max_context"`
	} `json:"capabilities"`
}

// SeedNodes registers a batch of Ollama nodes with the Gateway REST API.
// Called from `flume start` after the gateway container is healthy.
func SeedNodes(ctx context.Context, gatewayURL string, nodes []NodeSeedEntry) error {
	client := &http.Client{Timeout: 10 * time.Second}
	for _, node := range nodes {
		body, err := json.Marshal(node)
		if err != nil {
			log.Warn("node_seed: failed to marshal node", "node_id", node.ID, "error", err)
			continue
		}
		url := fmt.Sprintf("%s/api/nodes", strings.TrimRight(gatewayURL, "/"))
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			log.Warn("node_seed: failed to build request", "node_id", node.ID, "error", err)
			continue
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err != nil {
			log.Warn("node_seed: gateway unreachable", "node_id", node.ID, "error", err)
			continue
		}
		resp.Body.Close()

		if resp.StatusCode >= 400 {
			log.Warn("node_seed: gateway rejected node", "node_id", node.ID, "status", resp.StatusCode)
		} else {
			log.Info("node_seed: registered node", "node_id", node.ID, "host", node.Host, "model", node.ModelTag)
		}
	}
	return nil
}
