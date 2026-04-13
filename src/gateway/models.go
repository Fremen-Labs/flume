package gateway

import (
	"strings"
)

// ─────────────────────────────────────────────────────────────────────────────
// Shared request/response types, model aliases, and thinking-model detection.
// ─────────────────────────────────────────────────────────────────────────────

// ChatRequest is the unified inbound request for both /v1/chat and /v1/chat/tools.
type ChatRequest struct {
	Messages     []Message `json:"messages"`
	Model        string    `json:"model,omitempty"`
	Provider     string    `json:"provider,omitempty"`
	Temperature  float64   `json:"temperature,omitempty"`
	MaxTokens    int       `json:"max_tokens,omitempty"`
	Tools        []Tool    `json:"tools,omitempty"`
	Think        bool      `json:"think,omitempty"`
	CredentialID string    `json:"credential_id,omitempty"`
	AgentRole    string    `json:"agent_role,omitempty"`
	Stream       bool      `json:"stream,omitempty"`
}

// Message represents a single chat message.
// Fields beyond Role/Content carry OpenAI tool-calling metadata required by
// Gemini and other providers for multi-turn tool conversations.
type Message struct {
	Role       string                   `json:"role"`
	Content    string                   `json:"content"`
	ToolCalls  []map[string]interface{} `json:"tool_calls,omitempty"`
	ToolCallID string                   `json:"tool_call_id,omitempty"`
	Name       string                   `json:"name,omitempty"`
}

// Tool represents an OpenAI-format tool definition.
type Tool struct {
	Type     string       `json:"type"`
	Function ToolFunction `json:"function"`
}

// ToolFunction describes a callable function.
type ToolFunction struct {
	Name        string                 `json:"name"`
	Description string                 `json:"description,omitempty"`
	Parameters  map[string]interface{} `json:"parameters,omitempty"`
}

// ChatResponse is the unified outbound response (Ollama-compatible).
type ChatResponse struct {
	Message ResponseMessage `json:"message"`
	Usage   Usage           `json:"usage,omitempty"`
}

// ResponseMessage holds the assistant's reply.
type ResponseMessage struct {
	Role      string     `json:"role"`
	Content   string     `json:"content"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`
	Thoughts  string     `json:"thoughts,omitempty"`
}

// ToolCall represents a single function invocation from the model.
type ToolCall struct {
	ID       string           `json:"id,omitempty"`
	Type     string           `json:"type,omitempty"`
	Function ToolCallFunction `json:"function"`
}

// ToolCallFunction has the name and arguments of a tool call.
type ToolCallFunction struct {
	Name      string      `json:"name"`
	Arguments interface{} `json:"arguments"`
}

// Usage tracks token consumption.
type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// ─────────────────────────────────────────────────────────────────────────────
// Gemini model alias resolution
// ─────────────────────────────────────────────────────────────────────────────

// geminiModelAliases maps deprecated Gemini model IDs to current stable names.
var geminiModelAliases = map[string]string{
	"gemini-1.5-flash":        "gemini-2.5-flash",
	"gemini-1.5-flash-latest": "gemini-2.5-flash",
	"gemini-1.5-flash-8b":     "gemini-2.5-flash",
	"gemini-1.5-pro":          "gemini-2.5-pro",
	"gemini-1.5-pro-latest":   "gemini-2.5-pro",
	"gemini-2.0-flash":        "gemini-2.5-flash",
	"gemini-2.0-flash-lite":   "gemini-2.5-flash-lite",
}

// NormalizeGeminiModel maps deprecated model IDs to current stable names.
func NormalizeGeminiModel(model string) string {
	m := strings.TrimSpace(model)
	if m == "" {
		return "gemini-2.5-flash"
	}
	if alias, ok := geminiModelAliases[m]; ok {
		return alias
	}
	return m
}

// ─────────────────────────────────────────────────────────────────────────────
// Thinking-model detection
// ─────────────────────────────────────────────────────────────────────────────

// thinkingModelFragments are substrings in model names that indicate
// built-in reasoning/thinking mode (unbounded <think> blocks).
var thinkingModelFragments = []string{
	"gemma3", "gemma4", "qwq", "deepseek-r1", "marco-o1",
}

// IsThinkingModel returns true when the model is a known reasoning model
// that emits <think>...</think> blocks.
func IsThinkingModel(model string) bool {
	m := strings.ToLower(strings.ReplaceAll(strings.ReplaceAll(model, ":", "-"), " ", "-"))
	for _, frag := range thinkingModelFragments {
		if strings.Contains(m, frag) {
			return true
		}
	}
	return false
}

// NoThinkSystemMessage is injected for thinking models when the caller
// has NOT opted in to the reasoning phase.
const NoThinkSystemMessage = "IMPORTANT: Do NOT output any <think>...</think> reasoning blocks. " +
	"Respond directly with your answer only — no chain-of-thought, no reasoning " +
	"trace, no internal monologue. Begin your response immediately."

// InjectNoThinkSystem prepends or augments a system message to suppress
// chain-of-thought output from thinking models.
func InjectNoThinkSystem(messages []Message) []Message {
	out := make([]Message, len(messages))
	copy(out, messages)
	if len(out) > 0 && out[0].Role == "system" {
		out[0] = Message{
			Role:    "system",
			Content: out[0].Content + "\n\n" + NoThinkSystemMessage,
		}
	} else {
		out = append([]Message{{Role: "system", Content: NoThinkSystemMessage}}, out...)
	}
	return out
}

// ─────────────────────────────────────────────────────────────────────────────
// Provider constants
// ─────────────────────────────────────────────────────────────────────────────

const (
	ProviderOllama       = "ollama"
	ProviderOpenAI       = "openai"
	ProviderOpenAICompat = "openai_compatible"
	ProviderAnthropic    = "anthropic"
	ProviderGemini       = "gemini"
	ProviderXAI          = "xai"
	ProviderGrok         = "grok"
)

// ProviderBaseURLs are the default API base URLs for managed providers.
var ProviderBaseURLs = map[string]string{
	ProviderOpenAI:    "https://api.openai.com",
	ProviderAnthropic: "https://api.anthropic.com",
	ProviderGemini:    "https://generativelanguage.googleapis.com/v1beta/openai",
	ProviderXAI:       "https://api.x.ai",
	ProviderGrok:      "https://api.x.ai",
}

// ─────────────────────────────────────────────────────────────────────────────
// Agent role constants (for multi-model routing)
// ─────────────────────────────────────────────────────────────────────────────

const (
	RolePlanner     = "planner"
	RoleImplementer = "implementer"
	RoleReviewer    = "reviewer"
	RoleTester      = "tester"
	RolePM          = "pm"
	RoleCritic      = "critic"
)

// AgentModelConfig holds the per-role model override from flume-agent-models.
type AgentModelConfig struct {
	Role         string `json:"role"`
	Model        string `json:"model"`
	Provider     string `json:"provider,omitempty"`
	CredentialID string `json:"credential_id,omitempty"`
	Think        bool   `json:"think,omitempty"`
}
