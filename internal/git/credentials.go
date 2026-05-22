// credentials.go — Git credential embedding and repo type detection.
//
// Direct port of Python: utils/git_credentials.py (4 AST nodes).
// Provides a provider-agnostic adapter for embedding PATs into HTTPS remote
// URLs before clone/push operations.
package git

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"regexp"
	"strings"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/secrets"
)

// ─── Provider Detection ─────────────────────────────────────────────────────

// DetectRepoType classifies a repository URL into a provider type string.
// Returns one of: "local", "github", "ado", "generic_https", "ssh".
// Derived from Python: detect_repo_type().
func DetectRepoType(repoURL string) string {
	if repoURL == "" {
		return "local"
	}

	stripped := strings.TrimSpace(repoURL)

	// Absolute local filesystem paths
	if strings.HasPrefix(stripped, "/") || strings.HasPrefix(stripped, "./") || strings.HasPrefix(stripped, "~") {
		return "local"
	}
	// Windows-style path (rare in containers)
	if len(stripped) > 1 && stripped[1] == ':' {
		return "local"
	}
	// No scheme and no SSH prefix → likely local
	if !strings.Contains(stripped, "://") && !strings.HasPrefix(stripped, "git@") {
		return "local"
	}

	// SSH remotes
	if strings.HasPrefix(stripped, "git@") || strings.HasPrefix(stripped, "ssh://") {
		return "ssh"
	}

	lower := strings.ToLower(stripped)
	if strings.Contains(lower, "github.com") {
		return "github"
	}
	if strings.Contains(lower, "dev.azure.com") || strings.Contains(lower, "visualstudio.com") {
		return "ado"
	}
	if strings.HasPrefix(stripped, "http://") || strings.HasPrefix(stripped, "https://") {
		return "generic_https"
	}

	return "local"
}

// ─── Credential Embedding ───────────────────────────────────────────────────

// EmbedCredentials rewrites an HTTPS remote URL to embed a PAT using x-access-token format.
// Derived from Python: embed_credentials().
func EmbedCredentials(repoURL string, repoType string) string {
	if repoType == "" {
		repoType = DetectRepoType(repoURL)
	}

	if repoType == "local" || repoType == "ssh" || repoType == "generic_https" {
		return repoURL
	}

	token := resolveToken(repoType)
	if token == "" {
		return repoURL
	}

	// Strip existing userinfo before embedding
	return rewriteURL(StripCredentials(repoURL), token)
}

// StripCredentials removes any embedded userinfo from an HTTPS URL.
// Derived from Python: strip_credentials().
func StripCredentials(repoURL string) string {
	parsed, err := url.Parse(repoURL)
	if err != nil {
		slog.Debug("URL parse failed in StripCredentials — returning original",
			slog.String("error", err.Error()),
		)
		return repoURL
	}
	// Remove userinfo
	parsed.User = nil
	return parsed.String()
}

// ─── Internal ───────────────────────────────────────────────────────────────

const delegatedSentinel = "OPENBAO_DELEGATED"

// resolveToken resolves the PAT for a git provider.
// Priority: OpenBao KV → environment variable fallback.
// Derived from Python: _resolve_token().
func resolveToken(repoType string) string {
	ctx := context.Background()
	logger := slog.Default()

	var cfg *config.Config
	// Recover from panic if config.Get() hasn't been initialized (e.g. in tests)
	defer func() {
		_ = recover()
	}()
	cfg = config.Get()

	var esStore *secrets.ESStore
	var baoClient *secrets.OpenBaoClient
	if cfg != nil {
		if cfg.ESURL != "" {
			os.Setenv("ES_URL", cfg.ESURL)
		}
		if cfg.ESAPIKey != "" {
			os.Setenv("ES_API_KEY", cfg.ESAPIKey)
		}
		esStore = secrets.NewESStore(logger)
		if cfg.OpenBaoAddr != "" && cfg.OpenBaoToken != "" {
			baoClient = secrets.NewOpenBaoClient(cfg.OpenBaoAddr, cfg.OpenBaoToken, logger)
		}
	}

	if repoType == "ado" {
		if esStore != nil {
			store := secrets.NewADOTokenStore(esStore, baoClient, logger)
			token := store.GetActiveTokenPlain(ctx)
			if token != "" {
				return token
			}
		}
		token := envOr("ADO_TOKEN", envOr("ADO_PERSONAL_ACCESS_TOKEN", ""))
		if strings.Contains(token, delegatedSentinel) {
			return ""
		}
		return token
	}

	if repoType == "github" {
		if esStore != nil {
			store := secrets.NewGHTokenStore(esStore, baoClient, logger)
			token := store.GetActiveTokenPlain(ctx)
			if token != "" {
				return token
			}
		}
		token := envOr("GH_TOKEN", envOr("GITHUB_TOKEN", ""))
		if strings.Contains(token, delegatedSentinel) {
			return ""
		}
		return token
	}

	return ""
}

// rewriteURL inserts x-access-token:<encoded_token>@ into the URL.
// Derived from Python: _rewrite_url().
func rewriteURL(repoURL, token string) string {
	encoded := url.PathEscape(token)
	// Strip existing userinfo first
	clean := StripCredentials(repoURL)
	re := regexp.MustCompile(`^(https?://)`)
	return re.ReplaceAllString(clean, fmt.Sprintf("${1}x-access-token:%s@", encoded))
}

func envOr(key, fallback string) string {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	return v
}

// ─── Factory ────────────────────────────────────────────────────────────────

// GetClient returns the appropriate HostClient for a project document.
// proj must contain "repoUrl" or "repo_url".
// Derived from Python: get_git_client().
func GetClient(proj map[string]interface{}) (HostClient, error) {
	repoURL := strVal(proj["repoUrl"])
	if repoURL == "" {
		repoURL = strVal(proj["repo_url"])
	}
	repoURL = strings.TrimSpace(repoURL)

	repoType := DetectRepoType(repoURL)

	switch repoType {
	case "github":
		owner, repo, ok := ParseGitHubOwnerRepo(repoURL)
		if !ok {
			return nil, &HostError{Message: fmt.Sprintf("cannot parse GitHub owner/repo from: %s", repoURL)}
		}
		token := resolveToken("github")
		if token == "" {
			return nil, &AuthError{HostError{Message: "no GitHub PAT configured — add a token in Settings → Repositories"}}
		}
		return &GitHubClient{Owner: owner, Repo: repo, Token: token}, nil

	case "ado":
		org, project, repo, ok := ParseADOComponents(repoURL)
		if !ok {
			return nil, &HostError{Message: fmt.Sprintf("cannot parse ADO org/project/repo from: %s", repoURL)}
		}
		token := resolveToken("ado")
		if token == "" {
			return nil, &AuthError{HostError{Message: "no Azure DevOps PAT configured — add a token in Settings → Repositories"}}
		}
		return &AzureDevOpsClient{Org: org, Project: project, Repo: repo, Token: token}, nil

	default:
		return nil, &HostError{Message: fmt.Sprintf(
			"no HostClient implementation for provider type '%s' (URL: %s); supported: github, ado",
			repoType, repoURL)}
	}
}

// EnsureIntegrationBranchForProject is a convenience wrapper around GetClient + EnsureIntegrationBranch.
// Derived from Python: ensure_integration_branch_for_project().
func EnsureIntegrationBranchForProject(proj map[string]interface{}, branchName string) bool {
	branchName = strings.TrimSpace(branchName)
	if branchName == "" {
		return false
	}
	client, err := GetClient(proj)
	if err != nil {
		slog.Warn("ensure_integration_branch: no client",
			slog.String("error", truncate(err.Error(), 200)),
		)
		return false
	}
	ok, err := client.EnsureIntegrationBranch(branchName)
	if err != nil {
		slog.Warn("ensure_integration_branch failed",
			slog.String("branch", branchName),
			slog.String("error", truncate(err.Error(), 200)),
		)
		return false
	}
	return ok
}
