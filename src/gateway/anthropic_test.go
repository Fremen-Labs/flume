package gateway

import (
	"encoding/json"
	"testing"
)

// ─────────────────────────────────────────────────────────────────────────────
// Tests for normalizeMessagesForAnthropic
//
// Anthropic (Claude) uses a fundamentally different message format from
// the OpenAI-compatible providers:
//   - System messages are extracted to a top-level "system" field
//   - Tool results use role:"user" with tool_result content blocks
//     (not role:"tool" with tool_call_id)
//   - Tool calls use tool_use content blocks with tool_use_id
//     (not tool_calls array with id)
//   - Strict user/assistant alternation is required
//
// These tests verify the translation layer is correct.
// ─────────────────────────────────────────────────────────────────────────────

func TestNormalizeAnthropic_PlainConversation(t *testing.T) {
	msgs := []Message{
		{Role: "system", Content: "You are a helpful assistant."},
		{Role: "user", Content: "Hello"},
		{Role: "assistant", Content: "Hi there!"},
	}

	system, out := normalizeMessagesForAnthropic(msgs)

	if system != "You are a helpful assistant." {
		t.Errorf("system = %q, want \"You are a helpful assistant.\"", system)
	}
	if len(out) != 2 {
		t.Fatalf("expected 2 messages (system extracted), got %d", len(out))
	}

	// User message preserved
	userMsg := out[0].(map[string]interface{})
	if userMsg["role"] != "user" || userMsg["content"] != "Hello" {
		t.Errorf("user message = %v, want role:user content:Hello", userMsg)
	}

	// Assistant message preserved
	asstMsg := out[1].(map[string]interface{})
	if asstMsg["role"] != "assistant" || asstMsg["content"] != "Hi there!" {
		t.Errorf("assistant message = %v, want role:assistant content:Hi there!", asstMsg)
	}
}

func TestNormalizeAnthropic_ToolCallTranslation(t *testing.T) {
	// An assistant message with tool_calls should become content blocks
	// with tool_use entries.
	msgs := []Message{
		{Role: "user", Content: "What's the weather in NYC?"},
		{
			Role:    "assistant",
			Content: "Let me check the weather.",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "toolu_01",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "get_weather",
						"arguments": map[string]interface{}{"location": "NYC"},
					},
				},
			},
		},
	}

	_, out := normalizeMessagesForAnthropic(msgs)
	if len(out) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(out))
	}

	asstMsg := out[1].(map[string]interface{})
	if asstMsg["role"] != "assistant" {
		t.Fatalf("expected role:assistant, got %v", asstMsg["role"])
	}

	// Content should be an array of content blocks
	blocks, ok := asstMsg["content"].([]interface{})
	if !ok {
		t.Fatalf("expected content to be []interface{}, got %T", asstMsg["content"])
	}
	if len(blocks) != 2 {
		t.Fatalf("expected 2 content blocks (text + tool_use), got %d", len(blocks))
	}

	// First block: text
	textBlock := blocks[0].(map[string]interface{})
	if textBlock["type"] != "text" || textBlock["text"] != "Let me check the weather." {
		t.Errorf("text block = %v", textBlock)
	}

	// Second block: tool_use
	toolBlock := blocks[1].(map[string]interface{})
	if toolBlock["type"] != "tool_use" {
		t.Errorf("tool_use block type = %v, want tool_use", toolBlock["type"])
	}
	if toolBlock["id"] != "toolu_01" {
		t.Errorf("tool_use id = %v, want toolu_01", toolBlock["id"])
	}
	if toolBlock["name"] != "get_weather" {
		t.Errorf("tool_use name = %v, want get_weather", toolBlock["name"])
	}
}

func TestNormalizeAnthropic_ToolResultTranslation(t *testing.T) {
	// role:"tool" messages must become role:"user" with tool_result blocks.
	msgs := []Message{
		{Role: "user", Content: "Check the weather."},
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"id":       "toolu_01",
					"type":     "function",
					"function": map[string]interface{}{"name": "get_weather", "arguments": map[string]interface{}{"location": "NYC"}},
				},
			},
		},
		{Role: "tool", Content: "72°F, sunny", ToolCallID: "toolu_01"},
	}

	_, out := normalizeMessagesForAnthropic(msgs)
	if len(out) != 3 {
		t.Fatalf("expected 3 messages, got %d", len(out))
	}

	toolResultMsg := out[2].(map[string]interface{})

	// Must be role:user (Claude's requirement)
	if toolResultMsg["role"] != "user" {
		t.Errorf("tool result role = %v, want user", toolResultMsg["role"])
	}

	// Content must be an array of tool_result blocks
	blocks, ok := toolResultMsg["content"].([]interface{})
	if !ok {
		t.Fatalf("expected content as []interface{}, got %T", toolResultMsg["content"])
	}
	if len(blocks) != 1 {
		t.Fatalf("expected 1 tool_result block, got %d", len(blocks))
	}

	block := blocks[0].(map[string]interface{})
	if block["type"] != "tool_result" {
		t.Errorf("block type = %v, want tool_result", block["type"])
	}
	if block["tool_use_id"] != "toolu_01" {
		t.Errorf("tool_use_id = %v, want toolu_01", block["tool_use_id"])
	}
	if block["content"] != "72°F, sunny" {
		t.Errorf("content = %v, want '72°F, sunny'", block["content"])
	}
}

func TestNormalizeAnthropic_ConsecutiveToolResultsMerged(t *testing.T) {
	// Multiple consecutive role:"tool" messages must be merged into a
	// single role:"user" message (Claude requires strict alternation).
	msgs := []Message{
		{Role: "user", Content: "Read two files."},
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{"id": "call_0", "type": "function", "function": map[string]interface{}{"name": "read_file", "arguments": `{"path":"a.go"}`}},
				{"id": "call_1", "type": "function", "function": map[string]interface{}{"name": "read_file", "arguments": `{"path":"b.go"}`}},
			},
		},
		{Role: "tool", Content: "package a", ToolCallID: "call_0"},
		{Role: "tool", Content: "package b", ToolCallID: "call_1"},
		{Role: "assistant", Content: "Both files read."},
	}

	_, out := normalizeMessagesForAnthropic(msgs)

	// user → assistant (tool_use) → user (merged tool_results) → assistant
	if len(out) != 4 {
		t.Fatalf("expected 4 messages (tool results merged), got %d", len(out))
	}

	// The merged tool results message
	mergedMsg := out[2].(map[string]interface{})
	if mergedMsg["role"] != "user" {
		t.Errorf("merged message role = %v, want user", mergedMsg["role"])
	}

	blocks, ok := mergedMsg["content"].([]interface{})
	if !ok {
		t.Fatalf("expected content as []interface{}, got %T", mergedMsg["content"])
	}
	if len(blocks) != 2 {
		t.Fatalf("expected 2 tool_result blocks, got %d", len(blocks))
	}

	// Verify each tool_result block
	b0 := blocks[0].(map[string]interface{})
	if b0["tool_use_id"] != "call_0" || b0["content"] != "package a" {
		t.Errorf("first tool_result = %v", b0)
	}
	b1 := blocks[1].(map[string]interface{})
	if b1["tool_use_id"] != "call_1" || b1["content"] != "package b" {
		t.Errorf("second tool_result = %v", b1)
	}

	// Final assistant message
	finalMsg := out[3].(map[string]interface{})
	if finalMsg["role"] != "assistant" || finalMsg["content"] != "Both files read." {
		t.Errorf("final message = %v", finalMsg)
	}
}

func TestNormalizeAnthropic_StringArgumentsParsed(t *testing.T) {
	// tool_calls with string-encoded JSON arguments must be parsed
	// into objects for Claude (Claude expects input as an object).
	msgs := []Message{
		{Role: "user", Content: "test"},
		{
			Role: "assistant",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "call_0",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "read_file",
						"arguments": `{"path":"/tmp/foo.txt"}`,
					},
				},
			},
		},
	}

	_, out := normalizeMessagesForAnthropic(msgs)
	asstMsg := out[1].(map[string]interface{})
	blocks := asstMsg["content"].([]interface{})
	toolBlock := blocks[0].(map[string]interface{})

	// input should be a parsed object, not a JSON string
	input, ok := toolBlock["input"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected input as map, got %T: %v", toolBlock["input"], toolBlock["input"])
	}
	if input["path"] != "/tmp/foo.txt" {
		t.Errorf("parsed input.path = %v, want /tmp/foo.txt", input["path"])
	}
}

func TestNormalizeAnthropic_MultipleSystemMessages(t *testing.T) {
	// Multiple system messages should be concatenated with double newlines.
	msgs := []Message{
		{Role: "system", Content: "You are an expert coder."},
		{Role: "system", Content: "Always use Go."},
		{Role: "user", Content: "Hello"},
	}

	system, out := normalizeMessagesForAnthropic(msgs)

	if system != "You are an expert coder.\n\nAlways use Go." {
		t.Errorf("system = %q", system)
	}
	if len(out) != 1 {
		t.Errorf("expected 1 message (both systems extracted), got %d", len(out))
	}
}

func TestNormalizeAnthropic_EmptyMessages(t *testing.T) {
	system, out := normalizeMessagesForAnthropic(nil)
	if system != "" {
		t.Errorf("expected empty system, got %q", system)
	}
	if len(out) != 0 {
		t.Errorf("expected 0 messages, got %d", len(out))
	}
}

func TestNormalizeAnthropic_FullRoundTrip(t *testing.T) {
	// Simulate a complete multi-turn tool conversation as it flows
	// from the Python agent_runner through the gateway to Claude.
	msgs := []Message{
		{Role: "system", Content: "You are a helpful assistant."},
		{Role: "user", Content: "Find LinkedIn links in the repo."},
		{
			Role:    "assistant",
			Content: "I'll search the codebase for LinkedIn links.",
			ToolCalls: []map[string]interface{}{
				{
					"id":   "toolu_01abc",
					"type": "function",
					"function": map[string]interface{}{
						"name":      "elastro_query_ast",
						"arguments": map[string]interface{}{"query": "LinkedIn"},
					},
				},
			},
		},
		{Role: "tool", Content: "AST Search: No matching nodes found.", ToolCallID: "toolu_01abc"},
		{Role: "assistant", Content: "I couldn't find any LinkedIn links in the codebase."},
	}

	system, out := normalizeMessagesForAnthropic(msgs)

	// Verify structure
	if system != "You are a helpful assistant." {
		t.Errorf("system = %q", system)
	}
	if len(out) != 4 {
		t.Fatalf("expected 4 messages, got %d", len(out))
	}

	// Verify it serializes to valid JSON (the payload that would be sent to Claude)
	payload := map[string]interface{}{
		"model":    "claude-sonnet-4-20250514",
		"system":   system,
		"messages": out,
	}
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("failed to marshal Anthropic payload: %v", err)
	}

	// Verify the JSON structure
	var parsed map[string]interface{}
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	messages := parsed["messages"].([]interface{})

	// Message[0]: user
	m0 := messages[0].(map[string]interface{})
	if m0["role"] != "user" {
		t.Errorf("messages[0].role = %v", m0["role"])
	}

	// Message[1]: assistant with tool_use content blocks
	m1 := messages[1].(map[string]interface{})
	if m1["role"] != "assistant" {
		t.Errorf("messages[1].role = %v", m1["role"])
	}
	m1Blocks := m1["content"].([]interface{})
	if len(m1Blocks) != 2 { // text + tool_use
		t.Errorf("messages[1] blocks = %d, want 2", len(m1Blocks))
	}
	toolUse := m1Blocks[1].(map[string]interface{})
	if toolUse["type"] != "tool_use" || toolUse["id"] != "toolu_01abc" {
		t.Errorf("tool_use block = %v", toolUse)
	}

	// Message[2]: user with tool_result
	m2 := messages[2].(map[string]interface{})
	if m2["role"] != "user" {
		t.Errorf("messages[2].role = %v, want user", m2["role"])
	}
	m2Blocks := m2["content"].([]interface{})
	toolResult := m2Blocks[0].(map[string]interface{})
	if toolResult["type"] != "tool_result" {
		t.Errorf("tool_result type = %v", toolResult["type"])
	}
	if toolResult["tool_use_id"] != "toolu_01abc" {
		t.Errorf("tool_use_id = %v, want toolu_01abc", toolResult["tool_use_id"])
	}

	// Message[3]: assistant (plain)
	m3 := messages[3].(map[string]interface{})
	if m3["role"] != "assistant" {
		t.Errorf("messages[3].role = %v", m3["role"])
	}
}

// --- ToolCall struct ID propagation ---

func TestToolCall_IDSerialization(t *testing.T) {
	// Verify that the ID and Type fields serialize correctly to JSON,
	// allowing the Python agent_runner to receive them and preserve
	// them for follow-up turns.
	tc := ToolCall{
		ID:   "toolu_01abc",
		Type: "function",
		Function: ToolCallFunction{
			Name:      "get_weather",
			Arguments: map[string]interface{}{"location": "NYC"},
		},
	}

	data, err := json.Marshal(tc)
	if err != nil {
		t.Fatalf("failed to marshal ToolCall: %v", err)
	}

	var parsed map[string]interface{}
	if err := json.Unmarshal(data, &parsed); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	if parsed["id"] != "toolu_01abc" {
		t.Errorf("id = %v, want toolu_01abc", parsed["id"])
	}
	if parsed["type"] != "function" {
		t.Errorf("type = %v, want function", parsed["type"])
	}
}

func TestToolCall_ID_OmittedWhenEmpty(t *testing.T) {
	// When ID/Type are empty (Ollama responses), they should be omitted.
	tc := ToolCall{
		Function: ToolCallFunction{Name: "read_file", Arguments: map[string]interface{}{}},
	}

	data, err := json.Marshal(tc)
	if err != nil {
		t.Fatalf("failed to marshal ToolCall: %v", err)
	}

	var parsed map[string]interface{}
	json.Unmarshal(data, &parsed)

	if _, exists := parsed["id"]; exists {
		t.Error("id should be omitted when empty (omitempty)")
	}
	if _, exists := parsed["type"]; exists {
		t.Error("type should be omitted when empty (omitempty)")
	}
}
