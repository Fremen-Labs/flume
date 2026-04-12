package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Provider implementations — each handles routing to a specific LLM backend.
//
// Supported providers:
//   - Ollama          (local, streaming with think-milling)
//   - OpenAI          (api.openai.com)
//   - OpenAI-compat   (Groq, Together, Mistral, Azure OpenAI, etc.)
//   - Anthropic       (api.anthropic.com)
//   - Gemini          (Google AI Studio via OpenAI-compatible endpoint)
// ─────────────────────────────────────────────────────────────────────────────

// Provider is the interface for all LLM backends.
type Provider interface {
	Chat(ctx context.Context, req *ChatRequest) (*ChatResponse, error)
	ChatTools(ctx context.Context, req *ChatRequest) (*ChatResponse, error)
	Name() string
}

// ProviderRouter selects and invokes the correct provider for a request.
type ProviderRouter struct {
	config  *Config
	secrets *SecretStore
	client  *http.Client
}

// NewProviderRouter creates a router wired to config and secrets.
func NewProviderRouter(config *Config, secrets *SecretStore) *ProviderRouter {
	return &ProviderRouter{
		config:  config,
		secrets: secrets,
		client:  &http.Client{Timeout: 180 * time.Second},
	}
}

// Route dispatches a request to the appropriate provider.
// Guardrails (SanitizeToolResponse) are applied to every tool-call response
// before returning, so they fire unconditionally regardless of whether the
// caller is a single request, an ensemble member, or a frontier fallback.
func (r *ProviderRouter) Route(ctx context.Context, req *ChatRequest, withTools bool) (*ChatResponse, error) {
	// Resolve effective model/provider/credential
	model, provider, credID := r.config.ResolveModel(req)
	req.Model = model
	req.Provider = provider
	req.CredentialID = credID

	log := WithContext(ctx)
	log.Info("routing request",
		slog.String("provider", provider),
		slog.String("model", model),
		slog.String("agent_role", req.AgentRole),
		slog.Bool("with_tools", withTools),
	)

	// Resolve API key from OpenBao if needed
	apiKey, err := r.resolveAPIKey(ctx, provider, credID)
	if err != nil {
		log.Error("api key resolution failed — aborting request",
			slog.String("provider", provider),
			slog.String("cred_id", credID),
			slog.String("error", err.Error()),
		)
		return nil, fmt.Errorf("api key unavailable for provider %q: %w", provider, err)
	}

	// Check FLUME_OLLAMA_THINK env override (allows per-deployment opt-in)
	globalThinkOverride := false
	if v := strings.TrimSpace(os.Getenv("FLUME_OLLAMA_THINK")); v == "1" || v == "true" || v == "yes" {
		globalThinkOverride = true
	}

	// Determine if we should suppress thinking
	enableThink := req.Think || globalThinkOverride || r.config.ShouldThink(req)
	suppressThink := IsThinkingModel(model) && !enableThink

	var resp *ChatResponse
	switch provider {
	case ProviderOllama:
		resp, err = r.ollama(ctx, req, suppressThink, withTools)
	case ProviderOpenAI, ProviderOpenAICompat, ProviderGemini:
		resp, err = r.openaiCompat(ctx, req, provider, apiKey, withTools)
	case ProviderAnthropic:
		resp, err = r.anthropic(ctx, req, apiKey, withTools)
	default:
		return nil, fmt.Errorf("unsupported provider: %s", provider)
	}
	if err != nil {
		return nil, err
	}

	// ── Fix 1: guardrails applied universally at the routing layer ──────────
	// This ensures sanitization fires for EVERY response path — single call,
	// ensemble member, and frontier fallback — before the result is returned
	// to any caller. The HTTP handler also calls SanitizeToolResponse as a
	// defence-in-depth layer; the function is idempotent.
	if withTools {
		SanitizeToolResponse(resp)
	}

	return resp, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// API Key Resolution
// ─────────────────────────────────────────────────────────────────────────────

// resolveAPIKey returns the API key to use for the given provider.
//
// Resolution order (highest to lowest priority):
//  1. Credential-specific key from OpenBao (via credID)
//  2. Global LLM_API_KEY from OpenBao
//  3. LLM_API_KEY environment variable
//
// Managed providers (OpenAI, Anthropic, Gemini) return an error when no key
// is found — sending an empty Authorization header would result in an opaque
// 401 at the provider and could leak request context in error messages.
//
// OpenAI-compatible local endpoints (ProviderOpenAICompat) receive a dummy
// key to satisfy header-presence requirements without a real credential.
//
// Every fallback step is logged at Warn so operators can audit the resolution
// path and detect stale/missing secrets before they cause provider errors.
func (r *ProviderRouter) resolveAPIKey(ctx context.Context, provider, credID string) (string, error) {
	if provider == ProviderOllama {
		return "", nil
	}

	log := WithContext(ctx)

	// 1. Credential-specific key from OpenBao
	if credID != "" && credID != "__settings_default__" && credID != "__ollama__" {
		key := r.secrets.GetLLMKey(ctx, credID)
		if key != "" {
			return key, nil
		}
		log.Warn("api_key: credential-specific key not found in OpenBao — trying global fallback",
			slog.String("provider", provider),
			slog.String("cred_id", credID),
		)
	}

	// 2. Global key from OpenBao
	key := r.secrets.GetGlobalValue(ctx, "LLM_API_KEY")
	if key != "" {
		return key, nil
	}
	log.Warn("api_key: global LLM_API_KEY not found in OpenBao — trying environment fallback",
		slog.String("provider", provider),
	)

	// 3. Environment variable fallback
	key = os.Getenv("LLM_API_KEY")
	if key != "" {
		log.Warn("api_key: using LLM_API_KEY from environment variable — prefer OpenBao for secrets",
			slog.String("provider", provider),
		)
		return key, nil
	}

	// Local OpenAI-compatible endpoints (e.g. vLLM, LM Studio) require an
	// Authorization header but do not validate the key. Use a static dummy
	// to satisfy the schema without leaking any real credential.
	if provider == ProviderOpenAICompat {
		return "sk-local-dummy-key", nil
	}

	// For managed providers (OpenAI, Anthropic, Gemini) an absent key means
	// the request will fail with a provider 401. Fail early and loudly here
	// rather than propagating an empty Authorization header.
	log.Error("api_key: no key found for managed provider — request will be rejected",
		slog.String("provider", provider),
		slog.String("hint", "set LLM_API_KEY in OpenBao or the container environment"),
	)
	return "", fmt.Errorf("no API key available for provider %q", provider)
}

// ─────────────────────────────────────────────────────────────────────────────
// Ollama Provider
// ─────────────────────────────────────────────────────────────────────────────

func (r *ProviderRouter) ollama(ctx context.Context, req *ChatRequest, suppressThink, withTools bool) (*ChatResponse, error) {
	baseURL := r.config.GetOllamaBaseURL()
	numCtx := 8192
	if v := os.Getenv("FLUME_OLLAMA_NUM_CTX"); v != "" {
		fmt.Sscanf(v, "%d", &numCtx)
	}

	options := map[string]interface{}{
		"temperature": req.Temperature,
		"num_predict": req.MaxTokens,
		"num_ctx":     numCtx,
	}

	messages := messagesToSlice(req.Messages)

	if suppressThink {
		options["think"] = false
		messages = InjectNoThinkSystem(messages)
	}

	if withTools && len(req.Tools) > 0 {
		// THE CORE FIX: Use streaming for tool calls to prevent timeout
		return StreamOllamaToolCall(ctx, baseURL, messages, req.Tools, req.Model, options)
	}

	if suppressThink {
		// Use streaming + think milling for thinking models
		content, thoughts, err := StreamOllamaChat(ctx, baseURL, messages, req.Model, options)
		if err != nil {
			return nil, err
		}
		return &ChatResponse{
			Message: ResponseMessage{
				Role:     "assistant",
				Content:  content,
				Thoughts: thoughts,
			},
		}, nil
	}

	// Non-thinking model, no tools: use standard non-streaming call
	return r.ollamaNonStream(ctx, baseURL, messages, req.Model, options)
}

func (r *ProviderRouter) ollamaNonStream(
	ctx context.Context,
	baseURL string,
	messages []Message,
	model string,
	options map[string]interface{},
) (*ChatResponse, error) {
	defer LogDuration(ctx, "ollama_chat_nonstream")()

	payload := map[string]interface{}{
		"model":    model,
		"messages": messages,
		"stream":   false,
		"options":  options,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}

	url := strings.TrimRight(baseURL, "/") + "/api/chat"
	data, err := r.doPost(ctx, url, body, nil, 120*time.Second)
	if err != nil {
		return nil, err
	}

	content := ""
	if msg, ok := data["message"].(map[string]interface{}); ok {
		content, _ = msg["content"].(string)
	}

	return &ChatResponse{
		Message: ResponseMessage{
			Role:    "assistant",
			Content: strings.TrimSpace(content),
		},
	}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// OpenAI / OpenAI-Compatible / Gemini Provider
// ─────────────────────────────────────────────────────────────────────────────

func (r *ProviderRouter) openaiCompat(
	ctx context.Context,
	req *ChatRequest,
	provider, apiKey string,
	withTools bool,
) (*ChatResponse, error) {
	defer LogDuration(ctx, "openai_chat")()

	baseURL := r.config.GetBaseURL(provider)
	if baseURL == "" {
		if provider == ProviderGemini {
			baseURL = ProviderBaseURLs[ProviderGemini]
		} else if provider == ProviderOpenAI {
			baseURL = ProviderBaseURLs[ProviderOpenAI]
		} else {
			baseURL = os.Getenv("LLM_BASE_URL")
		}
	}
	url := strings.TrimRight(baseURL, "/") + "/v1/chat/completions"

	// Normalise messages for OpenAI-compatible APIs: tool_calls arguments
	// must be JSON *strings*, not parsed objects.  The gateway may have
	// received them as objects from Python on a follow-up turn.
	normMsgs := normalizeMessagesForOpenAI(req.Messages)

	payload := map[string]interface{}{
		"model":       req.Model,
		"messages":    normMsgs,
		"temperature": req.Temperature,
		"max_tokens":  req.MaxTokens,
	}
	if withTools && len(req.Tools) > 0 {
		payload["tools"] = req.Tools
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}

	headers := map[string]string{
		"Authorization": "Bearer " + apiKey,
	}

	data, err := r.doPost(ctx, url, body, headers, 180*time.Second)
	if err != nil {
		return nil, err
	}

	// Parse OpenAI response
	choices, _ := data["choices"].([]interface{})
	if len(choices) == 0 {
		return nil, fmt.Errorf("openai: no choices in response")
	}
	choiceMsg, _ := choices[0].(map[string]interface{})["message"].(map[string]interface{})
	content, _ := choiceMsg["content"].(string)

	var toolCalls []ToolCall
	if tcs, ok := choiceMsg["tool_calls"].([]interface{}); ok {
		for _, tc := range tcs {
			tcMap, _ := tc.(map[string]interface{})
			tcID, _ := tcMap["id"].(string)
			tcType, _ := tcMap["type"].(string)
			fn, _ := tcMap["function"].(map[string]interface{})
			name, _ := fn["name"].(string)
			argsRaw := fn["arguments"]
			// Parse string arguments into objects for the Python consumer.
			// The normalizeMessagesForOpenAI function will re-stringify them
			// if they flow back through the gateway on subsequent turns.
			var args interface{}
			if argsStr, ok := argsRaw.(string); ok {
				if err := json.Unmarshal([]byte(argsStr), &args); err != nil {
					args = argsStr
				}
			} else {
				args = argsRaw
			}
			toolCalls = append(toolCalls, ToolCall{
				ID:       tcID,
				Type:     tcType,
				Function: ToolCallFunction{Name: name, Arguments: args},
			})
		}
	}

	// Extract usage
	var usage Usage
	if u, ok := data["usage"].(map[string]interface{}); ok {
		if v, ok := u["prompt_tokens"].(float64); ok {
			usage.PromptTokens = int(v)
		}
		if v, ok := u["completion_tokens"].(float64); ok {
			usage.CompletionTokens = int(v)
		}
		if v, ok := u["total_tokens"].(float64); ok {
			usage.TotalTokens = int(v)
		}
	}

	return &ChatResponse{
		Message: ResponseMessage{
			Role:      "assistant",
			Content:   strings.TrimSpace(content),
			ToolCalls: toolCalls,
		},
		Usage: usage,
	}, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Anthropic Provider
// ─────────────────────────────────────────────────────────────────────────────

func (r *ProviderRouter) anthropic(
	ctx context.Context,
	req *ChatRequest,
	apiKey string,
	withTools bool,
) (*ChatResponse, error) {
	defer LogDuration(ctx, "anthropic_chat")()

	// Translate OpenAI-format messages into Anthropic's Messages API format.
	// This handles role:"tool" → role:"user" with tool_result blocks, and
	// role:"assistant" with tool_calls → content blocks with tool_use entries.
	system, messages := normalizeMessagesForAnthropic(req.Messages)

	payload := map[string]interface{}{
		"model":       req.Model,
		"max_tokens":  req.MaxTokens,
		"temperature": req.Temperature,
		"messages":    messages,
	}
	if system != "" {
		payload["system"] = system
	}
	if withTools && len(req.Tools) > 0 {
		payload["tools"] = convertToolsToAnthropic(req.Tools)
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}

	headers := map[string]string{
		"x-api-key":         apiKey,
		"anthropic-version": "2023-06-01",
	}

	data, err := r.doPost(ctx, "https://api.anthropic.com/v1/messages", body, headers, 180*time.Second)
	if err != nil {
		return nil, err
	}

	// Parse Anthropic response — capture tool_use IDs so the Python side
	// can reference them on follow-up turns via tool_call_id.
	var content string
	var toolCalls []ToolCall
	contentBlocks, _ := data["content"].([]interface{})
	for _, block := range contentBlocks {
		b, _ := block.(map[string]interface{})
		bType, _ := b["type"].(string)
		switch bType {
		case "text":
			content, _ = b["text"].(string)
		case "tool_use":
			name, _ := b["name"].(string)
			id, _ := b["id"].(string)
			args := b["input"]
			toolCalls = append(toolCalls, ToolCall{
				ID:       id,
				Type:     "function",
				Function: ToolCallFunction{Name: name, Arguments: args},
			})
		}
	}

	// Extract usage
	var usage Usage
	if u, ok := data["usage"].(map[string]interface{}); ok {
		if v, ok := u["input_tokens"].(float64); ok {
			usage.PromptTokens = int(v)
		}
		if v, ok := u["output_tokens"].(float64); ok {
			usage.CompletionTokens = int(v)
		}
		usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
	}

	return &ChatResponse{
		Message: ResponseMessage{
			Role:      "assistant",
			Content:   strings.TrimSpace(content),
			ToolCalls: toolCalls,
		},
		Usage: usage,
	}, nil
}

// convertToolsToAnthropic converts OpenAI tool format to Anthropic's format.
func convertToolsToAnthropic(tools []Tool) []map[string]interface{} {
	out := make([]map[string]interface{}, len(tools))
	for i, t := range tools {
		params := t.Function.Parameters
		if params == nil {
			params = map[string]interface{}{"type": "object", "properties": map[string]interface{}{}}
		}
		out[i] = map[string]interface{}{
			"name":         t.Function.Name,
			"description":  t.Function.Description,
			"input_schema": params,
		}
	}
	return out
}

// ─────────────────────────────────────────────────────────────────────────────
// HTTP helpers
// ─────────────────────────────────────────────────────────────────────────────

func (r *ProviderRouter) doPost(
	ctx context.Context,
	url string,
	body []byte,
	extraHeaders map[string]string,
	timeout time.Duration,
) (map[string]interface{}, error) {
	log := WithContext(ctx)

	client := &http.Client{Timeout: timeout}
	var lastErr error

	for attempt := 0; attempt < 4; attempt++ {
		// Bail immediately if context is already cancelled (e.g., ensemble timeout)
		if ctx.Err() != nil {
			if lastErr == nil {
				lastErr = ctx.Err()
			}
			return nil, fmt.Errorf("max retries exceeded: %w", lastErr)
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			return nil, fmt.Errorf("build request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		for k, v := range extraHeaders {
			req.Header.Set(k, v)
		}

		resp, err := client.Do(req)
		if err != nil {
			lastErr = err
			sleepDuration := time.Duration(1<<uint(attempt)) * time.Second
			log.Warn("request failed, retrying",
				slog.String("url", url),
				slog.Int("attempt", attempt+1),
				slog.String("error", err.Error()),
				slog.Duration("backoff", sleepDuration),
			)
			// Context-aware sleep: stops immediately on cancellation.
			select {
			case <-time.After(sleepDuration):
			case <-ctx.Done():
				return nil, fmt.Errorf("max retries exceeded: %w", lastErr)
			}
			continue
		}

		respBody, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode == 429 || resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(respBody[:min(len(respBody), 200)]))
			sleepDuration := time.Duration(1<<uint(attempt)) * time.Second
			log.Warn("retryable HTTP error",
				slog.String("url", url),
				slog.Int("status", resp.StatusCode),
				slog.Int("attempt", attempt+1),
				slog.Duration("backoff", sleepDuration),
			)
			// Context-aware sleep: stops immediately on cancellation.
			select {
			case <-time.After(sleepDuration):
			case <-ctx.Done():
				return nil, fmt.Errorf("max retries exceeded: %w", lastErr)
			}
			continue
		}

		if resp.StatusCode >= 400 {
			return nil, fmt.Errorf("HTTP %d for %s: %s", resp.StatusCode, url, string(respBody[:min(len(respBody), 200)]))
		}

		var result map[string]interface{}
		if err := json.Unmarshal(respBody, &result); err != nil {
			return nil, fmt.Errorf("decode response: %w", err)
		}
		return result, nil
	}

	return nil, fmt.Errorf("max retries exceeded: %w", lastErr)
}

// normalizeMessagesForOpenAI ensures that every message in the conversation
// conforms to OpenAI / Gemini expectations:
//   - assistant messages with tool_calls must have arguments as JSON *strings*
//
// The gateway parses arguments into Go objects internally; this function
// re-stringifies them before the payload is forwarded to the provider.
func normalizeMessagesForOpenAI(msgs []Message) []interface{} {
	out := make([]interface{}, len(msgs))
	for i, m := range msgs {
		nm := map[string]interface{}{
			"role":    m.Role,
			"content": m.Content,
		}
		if m.ToolCallID != "" {
			nm["tool_call_id"] = m.ToolCallID
		}
		if m.Name != "" {
			nm["name"] = m.Name
		}
		if len(m.ToolCalls) > 0 {
			normCalls := make([]interface{}, len(m.ToolCalls))
			for j, tc := range m.ToolCalls {
				tc = deepCopyMap(tc)
				if fn, ok := tc["function"].(map[string]interface{}); ok {
					if args := fn["arguments"]; args != nil {
						switch v := args.(type) {
						case string:
							// Already a string — leave as-is.
						default:
							// Object/array/number — re-stringify for the provider.
							bs, err := json.Marshal(v)
							if err == nil {
								fn["arguments"] = string(bs)
							}
						}
					}
				}
				normCalls[j] = tc
			}
			nm["tool_calls"] = normCalls
		}
		out[i] = nm
	}
	return out
}

// deepCopyMap creates a shallow copy of a map so mutations don't affect the
// original request message.
func deepCopyMap(src map[string]interface{}) map[string]interface{} {
	dst := make(map[string]interface{}, len(src))
	for k, v := range src {
		if sub, ok := v.(map[string]interface{}); ok {
			dst[k] = deepCopyMap(sub)
		} else {
			dst[k] = v
		}
	}
	return dst
}

// messagesToSlice is a no-op type assertion helper —  Message slice is
// already the correct type for Ollama/internal use.
func messagesToSlice(msgs []Message) []Message {
	return msgs
}

// normalizeMessagesForAnthropic translates OpenAI-format messages into
// Anthropic's Messages API format:
//   - system messages are extracted to a top-level field (returned separately)
//   - role:"tool" messages become role:"user" with tool_result content blocks
//   - role:"assistant" messages with tool_calls become content blocks with tool_use entries
//   - consecutive role:"tool" messages are merged into a single role:"user" message
//     (Claude requires strict user/assistant alternation)
func normalizeMessagesForAnthropic(msgs []Message) (string, []interface{}) {
	var system string
	var out []interface{}

	for i := 0; i < len(msgs); i++ {
		m := msgs[i]

		switch m.Role {
		case "system":
			if system != "" {
				system += "\n\n"
			}
			system += m.Content

		case "tool":
			// Collect all consecutive tool messages into one role:"user" message
			// with multiple tool_result content blocks. Claude requires strict
			// user/assistant alternation, so parallel tool results must be merged.
			var results []interface{}
			for i < len(msgs) && msgs[i].Role == "tool" {
				results = append(results, map[string]interface{}{
					"type":        "tool_result",
					"tool_use_id": msgs[i].ToolCallID,
					"content":     msgs[i].Content,
				})
				i++
			}
			i-- // back up — the outer for-loop will increment
			out = append(out, map[string]interface{}{
				"role":    "user",
				"content": results,
			})

		case "assistant":
			if len(m.ToolCalls) > 0 {
				// Convert tool_calls to Claude's tool_use content blocks.
				var blocks []interface{}
				if m.Content != "" {
					blocks = append(blocks, map[string]interface{}{
						"type": "text",
						"text": m.Content,
					})
				}
				for _, tc := range m.ToolCalls {
					id, _ := tc["id"].(string)
					fn, _ := tc["function"].(map[string]interface{})
					name, _ := fn["name"].(string)
					args := fn["arguments"]
					// Claude expects input as an object, not a JSON string.
					if argsStr, ok := args.(string); ok {
						var parsed interface{}
						if err := json.Unmarshal([]byte(argsStr), &parsed); err == nil {
							args = parsed
						}
					}
					blocks = append(blocks, map[string]interface{}{
						"type":  "tool_use",
						"id":    id,
						"name":  name,
						"input": args,
					})
				}
				out = append(out, map[string]interface{}{
					"role":    "assistant",
					"content": blocks,
				})
			} else {
				// Plain assistant message.
				out = append(out, map[string]interface{}{
					"role":    "assistant",
					"content": m.Content,
				})
			}

		case "user":
			out = append(out, map[string]interface{}{
				"role":    "user",
				"content": m.Content,
			})
		}
	}

	return system, out
}
