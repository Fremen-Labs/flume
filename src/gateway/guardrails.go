package gateway

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
)

// ─────────────────────────────────────────────────────────────────────────────
// Guardrails — gateway-level intelligence for small-model reliability.
//
// Responsibilities:
//   - Deduplicate identical tool calls emitted in a single response
//   - Filter out tool calls with empty function names
//   - Validate that tool call arguments are well-formed JSON
// ─────────────────────────────────────────────────────────────────────────────

// DeduplicateToolCalls removes duplicate tool calls based on function name +
// arguments content hash. Gemma and other small models frequently emit the
// same tool call 2-3 times in a single response.
func DeduplicateToolCalls(calls []ToolCall) []ToolCall {
	if len(calls) <= 1 {
		return calls
	}

	seen := make(map[string]struct{}, len(calls))
	deduped := make([]ToolCall, 0, len(calls))

	for _, tc := range calls {
		key := toolCallFingerprint(tc)
		if _, exists := seen[key]; exists {
			continue
		}
		seen[key] = struct{}{}
		deduped = append(deduped, tc)
	}

	if removed := len(calls) - len(deduped); removed > 0 {
		Log().Info("deduplicated tool calls",
			slog.Int("original", len(calls)),
			slog.Int("unique", len(deduped)),
			slog.Int("removed", removed),
		)
	}

	return deduped
}

// FilterInvalidToolCalls removes tool calls with empty function names and
// validates that arguments are parseable JSON (when they're a string).
// Returns the valid calls and a count of filtered calls.
func FilterInvalidToolCalls(calls []ToolCall) ([]ToolCall, int) {
	if len(calls) == 0 {
		return calls, 0
	}

	valid := make([]ToolCall, 0, len(calls))
	filtered := 0

	for _, tc := range calls {
		name := strings.TrimSpace(tc.Function.Name)
		if name == "" {
			Log().Warn("filtered tool call with empty function name")
			filtered++
			continue
		}

		// Normalize the function name (strip whitespace)
		tc.Function.Name = name

		// Validate arguments if they're a string (should be valid JSON)
		if argsStr, ok := tc.Function.Arguments.(string); ok {
			argsStr = strings.TrimSpace(argsStr)
			if argsStr == "" {
				// Empty string → empty object
				tc.Function.Arguments = map[string]interface{}{}
			} else {
				var parsed interface{}
				if err := json.Unmarshal([]byte(argsStr), &parsed); err != nil {
					Log().Warn("tool call has malformed JSON arguments",
						slog.String("function", name),
						slog.String("error", err.Error()),
						slog.Int("args_len", len(argsStr)),
					)
					// Attempt to salvage: wrap in a generic object
					tc.Function.Arguments = map[string]interface{}{
						"raw_input": argsStr,
					}
				} else {
					tc.Function.Arguments = parsed
				}
			}
		}

		valid = append(valid, tc)
	}

	return valid, filtered
}

// SanitizeToolResponse applies all guardrails to a tool-call response:
// filter invalid calls → deduplicate → return clean calls.
func SanitizeToolResponse(resp *ChatResponse) {
	if resp == nil || len(resp.Message.ToolCalls) == 0 {
		return
	}

	original := len(resp.Message.ToolCalls)

	// Step 1: Filter invalid calls
	valid, filtered := FilterInvalidToolCalls(resp.Message.ToolCalls)

	// Step 2: Deduplicate
	deduped := DeduplicateToolCalls(valid)

	resp.Message.ToolCalls = deduped

	total := original - len(deduped)
	if total > 0 {
		Log().Info("guardrails sanitized tool response",
			slog.Int("original_calls", original),
			slog.Int("final_calls", len(deduped)),
			slog.Int("filtered_invalid", filtered),
			slog.Int("deduplicated", original-filtered-len(deduped)),
		)
	}
}

// toolCallFingerprint creates a deterministic hash for deduplication.
func toolCallFingerprint(tc ToolCall) string {
	argsBytes, _ := json.Marshal(tc.Function.Arguments)
	raw := fmt.Sprintf("%s:%s", tc.Function.Name, string(argsBytes))
	h := sha256.Sum256([]byte(raw))
	return fmt.Sprintf("%x", h[:8])
}
