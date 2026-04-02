package ui

import (
	"net"
	"net/url"
	"regexp"
	"strings"
	"unicode"
)

// ---------------------------------------------------------------------------
// Shell-injection guard
// ---------------------------------------------------------------------------

// shellMeta is the set of characters that must never appear in free-text
// inputs that will later be passed to subprocesses or stored in Vault.
var shellMetaRe = regexp.MustCompile(`[` + regexp.QuoteMeta(`$(){};|&<>'"` + "`") + `]`)

// containsShellMeta reports whether s contains any shell-metacharacter.
func containsShellMeta(s string) bool {
	return shellMetaRe.MatchString(s)
}

// isPrintableASCII reports whether every rune in s is a printable, non-control
// ASCII character (0x20–0x7E inclusive).  Tabs and newlines are rejected.
func isPrintableASCII(s string) bool {
	for _, r := range s {
		if r < 0x20 || r > 0x7E {
			return false
		}
	}
	return true
}

// ---------------------------------------------------------------------------
// Per-step validators
// Each returns "" on success or a short human-readable error string.
// ---------------------------------------------------------------------------

// validateChoice validates a single-digit menu selection against an allowed set.
func validateChoice(val string, allowed []string) string {
	for _, a := range allowed {
		if val == a {
			return ""
		}
	}
	return "Invalid choice. Please enter one of: " + strings.Join(allowed, ", ")
}

// validateModel validates a model name or constraint string.
// Empty is allowed (means "use provider default").
func validateModel(val string) string {
	if val == "" {
		return ""
	}
	if len(val) > 80 {
		return "Model name too long (max 80 characters)"
	}
	if !isPrintableASCII(val) {
		return "Model name must contain only printable ASCII characters"
	}
	if containsShellMeta(val) {
		return "Model name contains invalid characters"
	}
	return ""
}

// validateHost validates a hostname or IPv4 address.
func validateHost(val string) string {
	if val == "" {
		return "Hostname or IP address is required"
	}
	if len(val) > 253 {
		return "Hostname too long (max 253 characters)"
	}
	// Must not contain whitespace
	for _, r := range val {
		if unicode.IsSpace(r) {
			return "Hostname must not contain spaces"
		}
	}
	if containsShellMeta(val) {
		return "Hostname contains invalid characters"
	}
	// Accept bare IPv4
	if net.ParseIP(val) != nil {
		return ""
	}
	// Validate as a hostname label sequence (RFC 1123)
	hostnameRe := regexp.MustCompile(`^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$`)
	if !hostnameRe.MatchString(val) {
		return "Invalid hostname or IP address"
	}
	return ""
}

// validateAPIKey validates a generic cloud API key (OpenAI, Anthropic, Gemini, Grok).
func validateAPIKey(val string) string {
	if val == "" {
		return "API key is required"
	}
	if len(val) > 512 {
		return "API key too long (max 512 characters)"
	}
	if !isPrintableASCII(val) {
		return "API key must contain only printable ASCII characters"
	}
	if containsShellMeta(val) {
		return "API key contains invalid characters"
	}
	return ""
}

// validateElasticURL validates an Elasticsearch HTTP/HTTPS URL.
func validateElasticURL(val string) string {
	if val == "" {
		return "Elasticsearch URL is required"
	}
	if len(val) > 512 {
		return "URL too long (max 512 characters)"
	}
	lower := strings.ToLower(val)
	if !strings.HasPrefix(lower, "http://") && !strings.HasPrefix(lower, "https://") {
		return "URL must start with http:// or https://"
	}
	parsed, err := url.ParseRequestURI(val)
	if err != nil || parsed.Host == "" {
		return "Invalid URL format (example: http://elasticsearch:9200)"
	}
	if containsShellMeta(val) {
		return "URL contains invalid characters"
	}
	return ""
}

// validateGitHubToken validates a GitHub Personal Access Token.
// Supports fine-grained (github_pat_…), OAuth (gho_…), server-to-server (ghs_…),
// classic PAT (ghp_…), and legacy 40-char hex tokens.
func validateGitHubToken(val string) string {
	if val == "" {
		return "GitHub Personal Access Token is required"
	}
	if len(val) > 512 {
		return "Token too long"
	}
	if !isPrintableASCII(val) || containsShellMeta(val) {
		return "Token contains invalid characters"
	}
	// Modern prefixed tokens
	for _, prefix := range []string{"ghp_", "gho_", "ghs_", "ghu_", "github_pat_"} {
		if strings.HasPrefix(val, prefix) {
			return ""
		}
	}
	// Legacy 40-char hex classic token
	legacyRe := regexp.MustCompile(`^[0-9a-fA-F]{40}$`)
	if legacyRe.MatchString(val) {
		return ""
	}
	return "Invalid GitHub token format (expected ghp_…, github_pat_…, or 40-char hex)"
}

// validateADOOrg validates an Azure DevOps organization name.
func validateADOOrg(val string) string {
	if val == "" {
		return "Azure DevOps organization name is required"
	}
	if len(val) > 100 {
		return "Organization name too long (max 100 characters)"
	}
	orgRe := regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$`)
	if !orgRe.MatchString(val) {
		return "Organization name must be alphanumeric with hyphens (no leading/trailing hyphens)"
	}
	return ""
}

// validateADOProject validates an Azure DevOps project name.
func validateADOProject(val string) string {
	if val == "" {
		return "Azure DevOps project name is required"
	}
	if len(val) > 100 {
		return "Project name too long (max 100 characters)"
	}
	if !isPrintableASCII(val) || containsShellMeta(val) {
		return "Project name contains invalid characters"
	}
	// ADO project names allow alphanumeric, spaces, hyphens, underscores, dots
	projRe := regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9 \-_.]*$`)
	if !projRe.MatchString(val) {
		return "Project name must start with alphanumeric and contain only letters, numbers, spaces, hyphens, underscores, or dots"
	}
	return ""
}

// validateADOToken validates an Azure DevOps Personal Access Token.
// ADO PATs are base64-encoded strings of fixed or variable length.
func validateADOToken(val string) string {
	if val == "" {
		return "Azure DevOps PAT is required"
	}
	if len(val) > 256 {
		return "Token too long (max 256 characters)"
	}
	if !isPrintableASCII(val) || containsShellMeta(val) {
		return "Token contains invalid characters"
	}
	// ADO PATs are base64url or standard base64 strings
	adoRe := regexp.MustCompile(`^[a-zA-Z0-9+/=]+$`)
	if !adoRe.MatchString(val) {
		return "Invalid ADO PAT format (expected base64-encoded string)"
	}
	if len(val) < 20 {
		return "ADO PAT appears too short — please check your token"
	}
	return ""
}

// validateForStep returns a validation error string for the given step and value.
// Returns "" if valid.
func validateForStep(step int, val string) string {
	switch step {
	case StepExoPrompt:
		if val == "" || val == "1" || val == "2" || val == "y" || val == "Y" || val == "n" || val == "N" {
			return ""
		}
		return "Please enter 1 (Yes) or 2 (No)"

	case StepProvider:
		if val == "" || val == "1" || val == "2" || val == "3" || val == "4" || val == "5" || val == "6" {
			return ""
		}
		return "Please select a provider: 1–6"

	case StepModel:
		return validateModel(val)

	case StepOllamaScope:
		if val == "" || val == "1" || val == "2" {
			return ""
		}
		return "Please enter 1 (Local) or 2 (Remote)"

	case StepOllamaIP:
		return validateHost(val)

	case StepAPIKey:
		return validateAPIKey(val)

	case StepElasticMenu:
		if val == "" || val == "1" || val == "2" {
			return ""
		}
		return "Please enter 1 or 2"

	case StepElasticURL:
		return validateElasticURL(val)

	case StepRepoMenu:
		if val == "" || val == "1" || val == "2" {
			return ""
		}
		return "Please enter 1 or 2"

	case StepRepoType:
		if val == "" || val == "1" || val == "2" {
			return ""
		}
		return "Please enter 1 (GitHub) or 2 (ADO)"

	case StepGithubToken:
		return validateGitHubToken(val)

	case StepADOOrg:
		return validateADOOrg(val)

	case StepADOProject:
		return validateADOProject(val)

	case StepADOToken:
		return validateADOToken(val)
	}
	return ""
}
