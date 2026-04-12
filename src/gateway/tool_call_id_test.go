package gateway

import (
	"strings"
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Regression tests for tool_call_id integrity.
//
// Root cause (fixed in commit 0864e984):
//   In agent_runner.py, the tool execution loop iterated over the raw
//   `tool_calls` list (which lacks an `id` field) instead of `norm_calls`
//   (which carries synthetic `call_0`, `call_1`, etc.). This caused every
//   tool result message to have an empty tool_call_id, which Gemini's
//   OpenAI-compatible endpoint rejects as HTTP 400 INVALID_ARGUMENT.
//
// These tests assert the guardrails at the Go gateway layer so that even
// if the Python side regresses, the gateway will reject the malformed
// payload before forwarding it to any provider.
// ─────────────────────────────────────────────────────────────────────────────

// --- ValidateToolMessages (standalone) ---

func TestValidateToolMessages_ToolMessageWithValidID(t *testing.T) {
	msgs := []Message{
		{Role: "user", Content: "hello"},
		{
			Role:    "assistant",
			Content: "",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "call_0",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "read_file",
						"arguments": `{"path":"/foo"}`,
					},
				},
			},
		},
		{Role: "tool", Content: "file contents here", ToolCallID: "call_0"},
	}

	if err := ValidateToolMessages(msgs); err != nil {
		t.Errorf("expected no error for valid tool messages, got: %v", err)
	}
}

func TestValidateToolMessages_ToolMessageEmptyID(t *testing.T) {
	// This is the EXACT bug scenario: role="tool" with tool_call_id=""
	msgs := []Message{
		{Role: "user", Content: "hello"},
		{Role: "tool", Content: "AST Search: No matching nodes found.", ToolCallID: ""},
	}

	err := ValidateToolMessages(msgs)
	if err == nil {
		t.Fatal("expected error for tool message with empty tool_call_id, got nil")
	}
	if !strings.Contains(err.Error(), "tool_call_id is empty") {
		t.Errorf("error should mention empty tool_call_id, got: %v", err)
	}
}

func TestValidateToolMessages_ToolMessageWhitespaceID(t *testing.T) {
	// Whitespace-only IDs are functionally empty and must be rejected.
	msgs := []Message{
		{Role: "tool", Content: "result", ToolCallID: "   "},
	}

	err := ValidateToolMessages(msgs)
	if err == nil {
		t.Fatal("expected error for tool message with whitespace-only tool_call_id")
	}
}

func TestValidateToolMessages_AssistantToolCallMissingID(t *testing.T) {
	// Assistant messages with tool_calls that lack an "id" field must fail.
	msgs := []Message{
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"type": "function",
					"function": map[string]interface{}{
						"name":      "read_file",
						"arguments": `{"path":"/tmp"}`,
					},
					// NOTE: no "id" key — this is the raw tool_calls bug
				},
			},
		},
	}

	err := ValidateToolMessages(msgs)
	if err == nil {
		t.Fatal("expected error for assistant tool_call with no id")
	}
	if !strings.Contains(err.Error(), "no \"id\" field") {
		t.Errorf("error should mention missing id, got: %v", err)
	}
}

func TestValidateToolMessages_AssistantToolCallEmptyID(t *testing.T) {
	msgs := []Message{
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "read_file",
						"arguments": `{"path":"/tmp"}`,
					},
				},
			},
		},
	}

	err := ValidateToolMessages(msgs)
	if err == nil {
		t.Fatal("expected error for assistant tool_call with empty id")
	}
}

func TestValidateToolMessages_MultipleToolCallsMixedIDs(t *testing.T) {
	// First tool call has a valid id, second does not.
	msgs := []Message{
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"id":       "call_0",
					"type":     "function",
					"function": map[string]interface{}{"name": "read_file", "arguments": "{}"},
				},
				{
					// missing id
					"type":     "function",
					"function": map[string]interface{}{"name": "write_file", "arguments": "{}"},
				},
			},
		},
	}

	err := ValidateToolMessages(msgs)
	if err == nil {
		t.Fatal("expected error when one of multiple tool_calls lacks an id")
	}
	if !strings.Contains(err.Error(), "tool_calls[1]") {
		t.Errorf("error should identify the second tool_call (index 1), got: %v", err)
	}
}

func TestValidateToolMessages_PlainMessagesPass(t *testing.T) {
	// Messages without tool-related fields should always pass.
	msgs := []Message{
		{Role: "system", Content: "You are a helpful assistant."},
		{Role: "user", Content: "What is 2+2?"},
		{Role: "assistant", Content: "4"},
	}

	if err := ValidateToolMessages(msgs); err != nil {
		t.Errorf("plain messages (no tool calls) should pass, got: %v", err)
	}
}

func TestValidateToolMessages_EmptySlice(t *testing.T) {
	if err := ValidateToolMessages(nil); err != nil {
		t.Errorf("nil message slice should pass, got: %v", err)
	}
	if err := ValidateToolMessages([]Message{}); err != nil {
		t.Errorf("empty message slice should pass, got: %v", err)
	}
}

// --- Full multi-turn conversation regression tests ---

func TestValidateToolMessages_FullConversation_ValidNormCalls(t *testing.T) {
	// Simulates the FIXED agent_runner: norm_calls with synthetic IDs.
	msgs := []Message{
		{Role: "system", Content: "You are a helpful assistant."},
		{Role: "user", Content: "Find LinkedIn links in the repo."},
		{
			Role:    "assistant",
			Content: "",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "call_0",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "elastro_query_ast",
						"arguments": `{"query":"LinkedIn icon OR linkedin link","target_path":"/tmp/flume-task-1"}`,
					},
				},
			},
		},
		{Role: "tool", Content: "AST Search: No matching nodes found.", ToolCallID: "call_0"},
		{Role: "assistant", Content: "I couldn't find any LinkedIn links in the codebase."},
	}

	if err := ValidateToolMessages(msgs); err != nil {
		t.Errorf("valid multi-turn conversation should pass: %v", err)
	}
}

func TestValidateToolMessages_FullConversation_BuggyRawToolCalls(t *testing.T) {
	// Simulates the BUGGY agent_runner: raw tool_calls (no id) and empty tool_call_id.
	// This is the exact payload that triggered Gemini's INVALID_ARGUMENT.
	msgs := []Message{
		{Role: "system", Content: "You are a helpful assistant."},
		{Role: "user", Content: "Find LinkedIn links in the repo."},
		{
			Role:    "assistant",
			Content: "",
			ToolCalls: []map[string]interface{}{
				{
					// BUG: no "id" field (raw tool_calls from the model)
					"type": "function",
					"function": map[string]interface{}{
						"name":      "elastro_query_ast",
						"arguments": `{"query":"LinkedIn"}`,
					},
				},
			},
		},
		// BUG: tool_call_id is empty because call.get('id', '') returned ''
		{Role: "tool", Content: "AST Search: No matching nodes found.", ToolCallID: ""},
	}

	err := ValidateToolMessages(msgs)
	if err == nil {
		t.Fatal("buggy conversation with raw tool_calls should be rejected")
	}
	// The error could fire on either the assistant's missing id or the tool's empty id.
	// Both are detected — whichever comes first wins.
	if !strings.Contains(err.Error(), "tool_calls[0]") && !strings.Contains(err.Error(), "tool_call_id is empty") {
		t.Errorf("error should flag the missing id or empty tool_call_id, got: %v", err)
	}
}

func TestValidateToolMessages_MultipleToolCalls_AllValid(t *testing.T) {
	// Simulates multiple tool calls in a single assistant turn — all with valid ids.
	msgs := []Message{
		{Role: "user", Content: "Read two files."},
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"id":       "call_0",
					"type":     "function",
					"function": map[string]interface{}{"name": "read_file", "arguments": `{"path":"a.go"}`},
				},
				{
					"id":       "call_1",
					"type":     "function",
					"function": map[string]interface{}{"name": "read_file", "arguments": `{"path":"b.go"}`},
				},
			},
		},
		{Role: "tool", Content: "package a", ToolCallID: "call_0"},
		{Role: "tool", Content: "package b", ToolCallID: "call_1"},
		{Role: "assistant", Content: "Both files read successfully."},
	}

	if err := ValidateToolMessages(msgs); err != nil {
		t.Errorf("multi-tool conversation with valid ids should pass: %v", err)
	}
}

// --- Integration with ValidateChatRequest ---

func TestValidateChatRequest_RejectsToolMessageWithEmptyID(t *testing.T) {
	req := &ChatRequest{
		Model: "gemini-2.5-flash",
		Messages: []Message{
			{Role: "user", Content: "hello"},
			{Role: "tool", Content: "result", ToolCallID: ""},
		},
	}

	err := ValidateChatRequest(req)
	if err == nil {
		t.Fatal("ValidateChatRequest should reject tool messages with empty tool_call_id")
	}
	if !strings.Contains(err.Error(), "tool_call_id") {
		t.Errorf("error should reference tool_call_id, got: %v", err)
	}
}

func TestValidateChatRequest_AcceptsToolMessageWithValidID(t *testing.T) {
	req := &ChatRequest{
		Messages: []Message{
			{Role: "user", Content: "hello"},
			{
				Role: "assistant",
				ToolCalls: []map[string]interface{}{
					{"id": "call_0", "type": "function", "function": map[string]interface{}{"name": "read_file", "arguments": "{}"}},
				},
			},
			{Role: "tool", Content: "file contents", ToolCallID: "call_0"},
		},
	}

	if err := ValidateChatRequest(req); err != nil {
		t.Errorf("ValidateChatRequest should accept valid tool messages: %v", err)
	}
}

// --- normalizeMessagesForOpenAI integration ---

func TestNormalizeMessagesForOpenAI_ToolCallIDPreserved(t *testing.T) {
	// Verifies that normalizeMessagesForOpenAI propagates tool_call_id
	// correctly (the gateway-side of the fix).
	msgs := []Message{
		{Role: "user", Content: "test"},
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "call_42",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "view_file",
						"arguments": map[string]interface{}{"path": "/tmp"},
					},
				},
			},
		},
		{Role: "tool", Content: "contents", ToolCallID: "call_42"},
	}

	result := normalizeMessagesForOpenAI(msgs)
	if len(result) != 3 {
		t.Fatalf("expected 3 messages, got %d", len(result))
	}

	// Tool message must retain tool_call_id
	toolMsg := result[2].(map[string]interface{})
	if toolMsg["tool_call_id"] != "call_42" {
		t.Errorf("tool_call_id was not preserved: got %v", toolMsg["tool_call_id"])
	}

	// Assistant message tool_calls must retain id
	assistantMsg := result[1].(map[string]interface{})
	toolCalls := assistantMsg["tool_calls"].([]interface{})
	tc := toolCalls[0].(map[string]interface{})
	if tc["id"] != "call_42" {
		t.Errorf("tool_call id was not preserved: got %v", tc["id"])
	}
}

func TestNormalizeMessagesForOpenAI_EmptyToolCallIDNotEmitted(t *testing.T) {
	// When tool_call_id is empty, normalizeMessagesForOpenAI should NOT
	// emit a "tool_call_id" key (it checks for != "").
	msgs := []Message{
		{Role: "user", Content: "test"},
	}

	result := normalizeMessagesForOpenAI(msgs)
	userMsg := result[0].(map[string]interface{})
	if _, exists := userMsg["tool_call_id"]; exists {
		t.Error("user message should not have a tool_call_id key")
	}
}
