package gateway

import (
	"encoding/json"
	"testing"
)

func TestNormalizeMessagesForOpenAI_StringifiesObjectArguments(t *testing.T) {
	// Simulate what happens when Python sends back an assistant message
	// with tool_calls containing parsed-object arguments (not strings).
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
						"name": "elastro_query_ast",
						// Arguments as a parsed object — this is what Gemini rejects:
						"arguments": map[string]interface{}{
							"query":       "LinkedIn icon OR linkedin link",
							"target_path": "/tmp/flume-task-1",
						},
					},
				},
			},
		},
		{
			Role:       "tool",
			Content:    "AST Search: No matching nodes found.",
			ToolCallID: "call_0",
		},
	}

	result := normalizeMessagesForOpenAI(msgs)

	if len(result) != 4 {
		t.Fatalf("expected 4 messages, got %d", len(result))
	}

	// Check the assistant message (index 2) has stringified arguments
	assistantMsg := result[2].(map[string]interface{})
	toolCalls := assistantMsg["tool_calls"].([]interface{})
	if len(toolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(toolCalls))
	}

	tc := toolCalls[0].(map[string]interface{})
	fn := tc["function"].(map[string]interface{})
	args := fn["arguments"]

	// Arguments MUST be a string, not a map
	argsStr, ok := args.(string)
	if !ok {
		t.Fatalf("expected arguments to be a string, got %T: %v", args, args)
	}

	// Verify it's valid JSON
	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(argsStr), &parsed); err != nil {
		t.Fatalf("arguments string is not valid JSON: %v", err)
	}
	if parsed["query"] != "LinkedIn icon OR linkedin link" {
		t.Errorf("unexpected query: %v", parsed["query"])
	}

	// Check tool message (index 3) has tool_call_id
	toolMsg := result[3].(map[string]interface{})
	if toolMsg["tool_call_id"] != "call_0" {
		t.Errorf("expected tool_call_id 'call_0', got %v", toolMsg["tool_call_id"])
	}
}

func TestNormalizeMessagesForOpenAI_PreservesStringArguments(t *testing.T) {
	// When arguments are already strings, they should pass through unchanged.
	msgs := []Message{
		{
			Role:    "assistant",
			Content: "",
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

	result := normalizeMessagesForOpenAI(msgs)
	assistantMsg := result[0].(map[string]interface{})
	toolCalls := assistantMsg["tool_calls"].([]interface{})
	tc := toolCalls[0].(map[string]interface{})
	fn := tc["function"].(map[string]interface{})
	args := fn["arguments"]

	argsStr, ok := args.(string)
	if !ok {
		t.Fatalf("expected string arguments to remain string, got %T", args)
	}
	if argsStr != `{"path":"/tmp/foo.txt"}` {
		t.Errorf("arguments were modified: %s", argsStr)
	}
}

func TestNormalizeMessagesForOpenAI_PlainMessages(t *testing.T) {
	// Plain messages without tool_calls should pass through cleanly.
	msgs := []Message{
		{Role: "system", Content: "Hello"},
		{Role: "user", Content: "World"},
	}

	result := normalizeMessagesForOpenAI(msgs)
	if len(result) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(result))
	}

	m := result[0].(map[string]interface{})
	if m["role"] != "system" {
		t.Errorf("expected role 'system', got %v", m["role"])
	}
	if _, ok := m["tool_calls"]; ok {
		t.Error("plain message should not have tool_calls key")
	}
}
