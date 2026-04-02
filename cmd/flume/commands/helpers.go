package commands

import (
	"fmt"
	"regexp"
	"strings"
)

// projectIDPattern is the strict allowlist for project identifiers.
// Only alphanumerics, hyphens, and underscores are permitted.
// This blocks path traversal sequences (e.g. "../", "%2F") at input level,
// before the value ever reaches url.JoinPath or the HTTP layer.
var projectIDPattern = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)

// validateProjectID returns an error if projectID contains any character
// outside the strict [a-zA-Z0-9_-] allowlist. Call this at the top of every
// function that embeds a user-supplied ID into an API path.
func validateProjectID(id string) error {
	if id == "" {
		return fmt.Errorf("project ID must not be empty")
	}
	if !projectIDPattern.MatchString(id) {
		return fmt.Errorf(
			"invalid project ID %q: only letters, digits, hyphens, and underscores are allowed",
			sanitizeForTerminal(id),
		)
	}
	return nil
}

// stringVal safely extracts a string value from a map[string]any.
// Returns def if the key is absent or the value is not a non-empty string.
func stringVal(m map[string]any, key, def string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return def
}

// stringValFromKeys tries each key in order and returns the first non-empty
// string found. Falls back to def if none match. Replaces the verbose nested
// stringVal(m, k1, stringVal(m, k2, def)) pattern throughout the codebase.
func stringValFromKeys(m map[string]any, def string, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok && s != "" {
				return s
			}
		}
	}
	return def
}

// truncate shortens s to at most n runes, appending "…" if truncated.
func truncate(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n-1]) + "…"
}

// sanitizeForTerminal strips ASCII control characters (except tab/newline) from
// user-supplied strings before printing them, preventing ANSI injection attacks.
func sanitizeForTerminal(s string) string {
	var b strings.Builder
	for _, r := range s {
		// Allow printable chars, tab, and newline; strip all other control chars.
		if r >= 0x20 || r == '\t' || r == '\n' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// maskSecret redacts a secret string completely.
// Returns "[SET]" if the secret is non-empty (confirming it is configured
// without leaking any characters), or "—" if absent.
// Previous implementations that exposed prefix/suffix characters were
// flagged as an OWASP information-disclosure risk.
func maskSecret(s string) string {
	if s == "" {
		return "—"
	}
	return "[SET]"
}
