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
	apiKey := r.resolveAPIKey(ctx, provider, credID)

	// Check FLUME_OLLAMA_THINK env override (allows per-deployment opt-in)
	globalThinkOverride := false
	if v := strings.TrimSpace(os.Getenv("FLUME_OLLAMA_THINK")); v == "1" || v == "true" || v == "yes" {
		globalThinkOverride = true
	}

	// Determine if we should suppress thinking
	enableThink := req.Think || globalThinkOverride || r.config.ShouldThink(req)
	suppressThink := IsThinkingModel(model) && !enableThink

	switch provider {
	case ProviderOllama:
		return r.ollama(ctx, req, suppressThink, withTools)
	case ProviderOpenAI, ProviderOpenAICompat, ProviderGemini:
		return r.openaiCompat(ctx, req, provider, apiKey, withTools)
	case ProviderAnthropic:
		return r.anthropic(ctx, req, apiKey, withTools)
	default:
		return nil, fmt.Errorf("unsupported provider: %s", provider)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// API Key Resolution
// ─────────────────────────────────────────────────────────────────────────────

func (r *ProviderRouter) resolveAPIKey(ctx context.Context, provider, credID string) string {
	if provider == ProviderOllama {
		return ""
	}

	// Try credential-specific key from OpenBao
	if credID != "" && credID != "__settings_default__" && credID != "__ollama__" {
		key := r.secrets.GetLLMKey(ctx, credID)
		if key != "" {
			return key
		}
	}

	// Try global secret
	key := r.secrets.GetGlobalValue(ctx, "LLM_API_KEY")
	if key != "" {
		return key
	}

	// Fallback to env
	key = os.Getenv("LLM_API_KEY")
	if key != "" {
		return key
	}

	// For openai_compatible / local providers that don't need a real key,
	// provide a dummy key to satisfy header requirements.
	if provider == ProviderOpenAICompat {
		return "sk-local-dummy-key"
	}

	return ""
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

	payload := map[string]interface{}{
		"model":       req.Model,
		"messages":    req.Messages,
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
			fn, _ := tcMap["function"].(map[string]interface{})
			name, _ := fn["name"].(string)
			argsRaw := fn["arguments"]
			// Arguments may be a JSON string or already parsed
			var args interface{}
			if argsStr, ok := argsRaw.(string); ok {
				if err := json.Unmarshal([]byte(argsStr), &args); err != nil {
					args = argsStr
				}
			} else {
				args = argsRaw
			}
			toolCalls = append(toolCalls, ToolCall{
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

	// Separate system from messages (Anthropic uses a top-level system field)
	var system string
	var messages []map[string]string
	for _, m := range req.Messages {
		if m.Role == "system" {
			system = m.Content
		} else {
			messages = append(messages, map[string]string{"role": m.Role, "content": m.Content})
		}
	}

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

	// Parse Anthropic response
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
			args := b["input"]
			toolCalls = append(toolCalls, ToolCall{
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

// messagesToSlice is a no-op type assertion helper —  Message slice is
// already the correct type for Ollama/internal use.
func messagesToSlice(msgs []Message) []Message {
	return msgs
}
