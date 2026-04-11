package gateway

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"regexp"
	"strings"
)

// ─────────────────────────────────────────────────────────────────────────────
// Guardrails — gateway-level security and reliability enforcement.
//
// Responsibilities:
//   - Validate and normalise inbound ChatRequest fields before provider dispatch
//   - Deduplicate identical tool calls emitted in a single response
//   - Filter out tool calls with empty function names
//   - Validate that tool call arguments are well-formed JSON
//   - Apply response sanitization universally (plain chat + tool calls)
// ─────────────────────────────────────────────────────────────────────────────

// modelNameRe allows alphanumeric characters, hyphens, underscores, dots, colons,
// and forward slashes (for namespaced registry paths like "library/llama3.2").
// Any character outside this set is a signal of prompt injection or misconfiguration.
var modelNameRe = regexp.MustCompile(`^[a-zA-Z0-9_\-\.:/]+$`)

// maxModelNameLen caps model name length to prevent oversized strings from
// reaching provider APIs or log pipelines.
const maxModelNameLen = 128

// maxMessageContentLen caps individual message content to 512 KB.
// Larger payloads should use file references, not inline content.
const maxMessageContentLen = 512 * 1024

// allowedProviders is the closed set of provider strings the gateway accepts.
var allowedProviders = map[string]bool{
	ProviderOllama:       true,
	ProviderOpenAI:       true,
	ProviderOpenAICompat: true,
	ProviderAnthropic:    true,
	ProviderGemini:       true,
	"": true, // empty = resolved from config
}

// ValidateChatRequest validates and normalises an inbound ChatRequest.
// It returns an error if the request contains values that are structurally
// invalid or outside accepted bounds. Safe normalisation (e.g. clamping
// temperature) is applied in-place before returning.
//
// This is the outermost gate: it fires BEFORE any provider dispatch,
// config resolution, or secret lookup.
func ValidateChatRequest(req *ChatRequest) error {
	if req == nil {
		return fmt.Errorf("request must not be nil")
	}

	// ── Model name ─────────────────────────────────────────────────────────
	req.Model = strings.TrimSpace(req.Model)
	if req.Model != "" {
		if len(req.Model) > maxModelNameLen {
			return fmt.Errorf("model name too long: %d chars (max %d)", len(req.Model), maxModelNameLen)
		}
		if strings.Contains(req.Model, "..") {
			return fmt.Errorf("model name contains path traversal sequence: %q", req.Model)
		}
		if !modelNameRe.MatchString(req.Model) {
			return fmt.Errorf("model name contains invalid characters: %q", req.Model)
		}
	}

	// ── Provider ────────────────────────────────────────────────────────────
	req.Provider = strings.TrimSpace(strings.ToLower(req.Provider))
	if !allowedProviders[req.Provider] {
		return fmt.Errorf("unknown provider: %q", req.Provider)
	}

	// ── Temperature ─────────────────────────────────────────────────────────
	// Clamp rather than reject: callers may pass 0.0 (valid) or slightly
	// above 1.0 due to floating point rounding. Hard-reject NaN/Inf.
	if math.IsNaN(req.Temperature) || math.IsInf(req.Temperature, 0) {
		return fmt.Errorf("temperature must be a finite number, got %v", req.Temperature)
	}
	if req.Temperature < 0 {
		req.Temperature = 0
	}
	if req.Temperature > 2.0 {
		// Some providers accept up to 2.0; anything above is almost certainly
		// an error or an attempt to induce maximum randomness.
		return fmt.Errorf("temperature %v exceeds maximum (2.0)", req.Temperature)
	}

	// ── MaxTokens ───────────────────────────────────────────────────────────
	if req.MaxTokens < 0 {
		return fmt.Errorf("max_tokens must be non-negative, got %d", req.MaxTokens)
	}
	const hardMaxTokens = 128_000
	if req.MaxTokens > hardMaxTokens {
		return fmt.Errorf("max_tokens %d exceeds hard cap (%d)", req.MaxTokens, hardMaxTokens)
	}

	// ── Agent role ──────────────────────────────────────────────────────────
	req.AgentRole = strings.TrimSpace(req.AgentRole)

	// ── Messages ────────────────────────────────────────────────────────────
	if len(req.Messages) == 0 {
		return fmt.Errorf("messages must not be empty")
	}
	for i, m := range req.Messages {
		m.Role = strings.TrimSpace(strings.ToLower(m.Role))
		switch m.Role {
		case "system", "user", "assistant", "tool":
			// valid
		default:
			return fmt.Errorf("message[%d] has invalid role: %q", i, m.Role)
		}
		if len(m.Content) > maxMessageContentLen {
			return fmt.Errorf("message[%d] content exceeds max size (%d bytes)", i, maxMessageContentLen)
		}
		req.Messages[i] = m
	}

	// ── Tool message integrity ──────────────────────────────────────────────
	if err := ValidateToolMessages(req.Messages); err != nil {
		return err
	}

	return nil
}

// ValidateToolMessages checks that every role:"tool" message carries a
// non-empty tool_call_id, and that every role:"assistant" message which
// includes tool_calls has a matching "id" field on each call.
//
// This catches the exact class of bug that caused Gemini INVALID_ARGUMENT
// errors: the Python agent_runner was iterating over raw tool_calls (no id)
// instead of norm_calls (with synthetic id), sending empty tool_call_id on
// every tool result message.
//
// This function can be called standalone (e.g. from tests or pre-send
// assertions) or as part of ValidateChatRequest.
func ValidateToolMessages(messages []Message) error {
	for i, m := range messages {
		switch m.Role {
		case "tool":
			if strings.TrimSpace(m.ToolCallID) == "" {
				return fmt.Errorf(
					"message[%d] has role \"tool\" but tool_call_id is empty — "+
						"every tool result must reference the originating tool_call_id",
					i,
				)
			}
		case "assistant":
			for j, tc := range m.ToolCalls {
				id, _ := tc["id"].(string)
				if strings.TrimSpace(id) == "" {
					return fmt.Errorf(
						"message[%d].tool_calls[%d] has no \"id\" field — "+
							"OpenAI-compatible APIs require each tool_call to carry a unique id",
						i, j,
					)
				}
			}
		}
	}
	return nil
}
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
//
// This is called from Route() so it fires for EVERY response path
// (single-call, ensemble winner, frontier fallback) before the result
// propagates back to the HTTP handler. The handler calls it again as a
// defence-in-depth measure; SanitizeToolResponse is idempotent.
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
