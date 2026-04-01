package orchestrator

import (
	"crypto/rand"
	"encoding/hex"
	"strings"

	"github.com/charmbracelet/log"
)

type EnvConfig struct {
	Provider           string
	APIKey             string
	BaseURL            string
	LocalOllamaBaseURL string
	Host               string
	AdminToken         string
	Model              string
	IsNative           bool

	ExternalElastic bool
	ESUrl           string

	RepoType    string // "github" or "ado"
	GithubToken string
	ADOOrg      string
	ADOProject  string
	ADOToken    string
}

// GenerateAdminToken creates a 256-bit cryptographically secure token.
func GenerateAdminToken() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return "flume_adm_" + hex.EncodeToString(bytes), nil
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
	env = append(env, "VAULT_TOKEN=flume-dev-token", "OPENBAO_TOKEN=flume-dev-token")

	if config.AdminToken != "" {
		env = append(env, "FLUME_ADMIN_TOKEN="+config.AdminToken)
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
	}

	if config.RepoType == "github" && config.GithubToken != "" {
		env = append(env, "GITHUB_TOKEN="+config.GithubToken)
	} else if config.RepoType == "ado" {
		if config.ADOOrg != "" {
			env = append(env, "ADO_ORGANIZATION="+config.ADOOrg)
		}
		if config.ADOProject != "" {
			env = append(env, "ADO_PROJECT="+config.ADOProject)
		}
		if config.ADOToken != "" {
			env = append(env, "ADO_PERSONAL_ACCESS_TOKEN="+config.ADOToken)
		}
	}

	log.Info("Successfully compiled telemetry footprint securely in RAM bounds.", "isolation", "Memory-only variables injected into subprocess securely")
	return env
}

