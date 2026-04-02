package commands

import (
	"strings"
)

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

// maskSecret redacts a secret string, showing only the first 4 and last 4
// characters. Strings shorter than 12 chars are fully redacted. This
// prevents the original 8-char prefix leak flagged by OWASP review.
func maskSecret(s string) string {
	if s == "" {
		return "—"
	}
	runes := []rune(s)
	if len(runes) < 12 {
		return "[REDACTED]"
	}
	return string(runes[:4]) + "…" + string(runes[len(runes)-4:])
}
