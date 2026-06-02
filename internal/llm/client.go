// Package llm provides the Flume LLM client with gateway-backed routing,
// intelligent fallback, and multi-provider support.
//
// Direct port of Python:
//   - utils/llm_client.py (28 AST nodes) — gateway-backed thin HTTP shim
//   - utils/llm_client_gateway.py (3 nodes) — gateway health check + POST
//   - utils/llm_client_legacy.py (3 nodes) — direct provider calls
//   - utils/llm_client_fallback.py (1 node) — intelligent model fallback
//
// Architecture:
//   In the Go binary, the gateway runs in-process — but this client still
//   uses HTTP to communicate with it for two reasons:
//     1. The gateway may run standalone (docker compose).
//     2. The HTTP boundary provides clean observability (request IDs, telemetry).
//
//   When Phase 5 merges everything into one binary, this client can optionally
//   call the gateway handler directly (zero-copy).
package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
)

// ErrPersistentConfig is a sentinel for unrecoverable LLM/gateway configuration errors
// (e.g. 404 on legacy path, unreachable gateway with no Ollama fallback).
// Callers use errors.Is to trigger fast-fail to blocked instead of retry storms.
// Per reliable-go-systems SKILL (explicit typed errors, no brittle strings).
var ErrPersistentConfig = fmt.Errorf("llm: persistent config error (will not self-heal)")

// Client is the Flume LLM client.
// Routes traffic through the gateway; falls back to direct provider calls.
type Client struct {
	gatewayURL    string
	httpClient    *http.Client
	logger        *slog.Logger
	workerName    string

	// Gateway health cache (30s TTL)
	mu            sync.RWMutex
	gatewayOK     bool
	gatewayCheckedAt time.Time
}

// ChatRequest holds the parameters for a Chat call.
type ChatRequest struct {
	Messages         []Message `json:"messages"`
	Model            string    `json:"model,omitempty"`
	Provider         string    `json:"provider,omitempty"`
	Temperature      float64   `json:"temperature"`
	MaxTokens        int       `json:"max_tokens"`
	Think            bool      `json:"think,omitempty"`
	AgentRole        string    `json:"agent_role,omitempty"`
	TaskType         string    `json:"task_type,omitempty"` // "planning", "code", "reasoning", etc. — overrides AgentRole-derived type for routing
	TaskID           string    `json:"task_id,omitempty"`
	PlanSessionID    string    `json:"plan_session_id,omitempty"` // Phase 2: for per-plan budget + gateway PM rate limiter (keyed by (plan,role=pm))
	WorkerName       string    `json:"worker_name,omitempty"`
	TimeoutSeconds   int       `json:"-"` // client-side timeout, not sent to gateway
	ReturnUsage      bool      `json:"-"`
	ReturnTelemetry  bool      `json:"-"`
}

// ChatToolsRequest holds the parameters for a ChatWithTools call.
type ChatToolsRequest struct {
	Messages         []Message      `json:"messages"`
	Tools            []Tool         `json:"tools"`
	Model            string         `json:"model,omitempty"`
	Provider         string         `json:"provider,omitempty"`
	Temperature      float64        `json:"temperature"`
	MaxTokens        int            `json:"max_tokens"`
	Think            bool           `json:"think,omitempty"`
	AgentRole        string         `json:"agent_role,omitempty"`
	TaskID           string         `json:"task_id,omitempty"`
	PlanSessionID    string         `json:"plan_session_id,omitempty"` // Phase 2: for per-plan budget + gateway PM rate limiter
	WorkerName       string         `json:"worker_name,omitempty"`
	ReturnTelemetry  bool           `json:"-"`
}

// Message is an OpenAI-compatible chat message.
type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
}

// Tool is an OpenAI-compatible tool definition.
type Tool struct {
	Type     string       `json:"type"`
	Function ToolFunction `json:"function"`
}

// ToolFunction describes a callable function.
type ToolFunction struct {
	Name        string      `json:"name"`
	Description string      `json:"description,omitempty"`
	Parameters  interface{} `json:"parameters,omitempty"`
}

// ToolCall represents a tool call from the assistant.
type ToolCall struct {
	ID       string           `json:"id,omitempty"`
	Type     string           `json:"type,omitempty"`
	Function ToolCallFunction `json:"function"`
}

// ToolCallFunction is the function details of a tool call.
type ToolCallFunction struct {
	Name      string      `json:"name"`
	Arguments interface{} `json:"arguments"`
}

// ChatStreamChunk is the client-side view of an incremental streaming response
// from the gateway (or legacy direct path).
//
// Matches the gateway's ChatStreamChunk shape (see src/gateway/models.go).
// Used by workers to observe partial reasoning / thoughts in real time during
// long local Ollama generations (the key to diagnosing PM and other agent
// behavior that was previously invisible behind aggregated responses).
type ChatStreamChunk struct {
	RequestID    string                 `json:"request_id,omitempty"`
	DeltaContent string                 `json:"delta_content,omitempty"`
	Thoughts     string                 `json:"thoughts,omitempty"`
	ToolCalls    []ToolCall             `json:"tool_calls,omitempty"`
	Usage        map[string]interface{} `json:"usage,omitempty"`
	Telemetry    map[string]interface{} `json:"telemetry,omitempty"`
	Done         bool                   `json:"done"`
	Error        string                 `json:"error,omitempty"`
}

// ChatResponse is the unified response from a Chat call.
type ChatResponse struct {
	Content   string                 `json:"content"`
	Usage     map[string]interface{} `json:"usage,omitempty"`
	Telemetry map[string]interface{} `json:"telemetry,omitempty"`
}

// ChatToolsResponse is the unified response from a ChatWithTools call.
type ChatToolsResponse struct {
	Message   ToolMessage            `json:"message"`
	Usage     map[string]interface{} `json:"usage,omitempty"`
	Telemetry map[string]interface{} `json:"telemetry,omitempty"`
}

// ToolMessage is the assistant's response containing optional tool calls.
type ToolMessage struct {
	Role      string     `json:"role"`
	Content   string     `json:"content"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`
}

// New creates a new LLM client.
func New(logger *slog.Logger) *Client {
	if logger == nil {
		logger = flumelogger.Log()
	}
	gatewayURL := os.Getenv("FLUME_GATEWAY_URL")
	if gatewayURL == "" {
		// Native mode: gateway runs in-process on localhost.
		// Docker mode: docker compose DNS resolves "gateway".
		if os.Getenv("FLUME_NATIVE_MODE") == "1" {
			gatewayURL = "http://localhost:8090"
		} else {
			gatewayURL = "http://gateway:8090"
		}
	}
	gatewayURL = strings.TrimRight(gatewayURL, "/")

	workerName := os.Getenv("FLUME_WORKER_NAME")
	if workerName == "" {
		workerName = "go-worker"
	}

	c := &Client{
		gatewayURL: gatewayURL,
		httpClient: &http.Client{
			// No Client.Timeout — we use per-request context timeouts (300s) instead.
			// Client.Timeout applies to the ENTIRE request lifecycle including reading
			// the response body. With streaming (stream:true), the response body is read
			// incrementally over the full generation time, which can exceed 180s for
			// thinking models. The old 180s Client.Timeout caused:
			//   "Client.Timeout exceeded while awaiting headers"
			// Per-request context.WithTimeout is the correct approach for streaming.
		},
		logger:     logger,
		workerName: workerName,
	}

	// Startup health check — make misconfiguration loud and immediately visible.
	go func() {
		time.Sleep(3 * time.Second)
		if !c.gatewayAvailable(context.Background()) {
			c.logger.Error("STARTUP CRITICAL: gateway unreachable at configured URL — all LLM calls will use legacy fallback",
				slog.String("gateway_url", c.gatewayURL),
				slog.String("hint", "Set FLUME_GATEWAY_URL=http://localhost:8090 for native mode"),
			)
		} else {
			c.logger.Info("gateway health check passed",
				slog.String("gateway_url", c.gatewayURL),
			)
		}
	}()

	return c
}

// Chat calls the LLM and returns the assistant's text response.
// Derived from Python: llm_client.py chat() — gateway with legacy fallback.
func (c *Client) Chat(ctx context.Context, req ChatRequest) (*ChatResponse, error) {
	if req.Temperature == 0 {
		req.Temperature = 0.3
	}
	if req.MaxTokens == 0 {
		req.MaxTokens = 8192
	}
	timeout := req.TimeoutSeconds
	if timeout == 0 {
		timeout = 300 // Must match gateway WriteTimeout (300s). The old 120s caused
		// "context deadline exceeded" because the gateway aggregates the full Ollama
		// stream before responding — the client sees zero bytes until completion.
		// Thinking models (qwen3.5:35b-a3b) routinely need 2-5 minutes.
	}

	if c.gatewayAvailable(ctx) {
		payload := map[string]interface{}{
			"messages":    req.Messages,
			"model":       req.Model,
			"provider":    req.Provider,
			"temperature": req.Temperature,
			"max_tokens":  req.MaxTokens,
			"think":       req.Think,
			"agent_role":  req.AgentRole,
		}
		if req.TaskID != "" {
			payload["task_id"] = req.TaskID
		}
		if req.PlanSessionID != "" {
			payload["plan_session_id"] = req.PlanSessionID
		}

		resp, err := c.postGateway(ctx, "/v1/chat", payload, timeout, req.WorkerName)
		if err != nil {
			flumelogger.WithContext(ctx).Warn("gateway chat request failed, attempting fallback",
				slog.String("error", err.Error()),
				slog.String("model", req.Model),
			)

			// Attempt intelligent model fallback
			fallback := ResolveFallback(ctx, req.Provider, req.Model, c.gatewayURL)
			if fallback != "" {
				flumelogger.WithContext(ctx).Warn("intelligently downgrading model",
					slog.String("from", req.Model),
					slog.String("to", fallback),
				)
				payload["model"] = fallback
				resp, err = c.postGateway(ctx, "/v1/chat", payload, timeout, req.WorkerName)
				if err == nil {
					return c.parseChatResponse(ctx, resp)
				}
				flumelogger.WithContext(ctx).Warn("fallback model also failed",
					slog.String("error", err.Error()),
				)
			}

			// Fall through to legacy
		} else {
			respParsed, perr := c.parseChatResponse(ctx, resp)
			if perr == nil && req.TaskID != "" {
				usage := respParsed.Usage
				pt := 0
				ct := 0
				if u, ok := usage["prompt_tokens"].(float64); ok {
					pt = int(u)
				}
				if u, ok := usage["completion_tokens"].(float64); ok {
					ct = int(u)
				}
				flumelogger.LogLLMCall(ctx, req.TaskID, req.Provider, req.Model, pt, ct, 0, nil)
			}
			return respParsed, perr
		}
	}

	// Legacy fallback: call the gateway directly but via legacy endpoints
	flumelogger.WithContext(ctx).Debug("using legacy direct provider call")
	return c.legacyChat(ctx, req)
}

// ChatWithTools calls the LLM with tool definitions.
// Derived from Python: llm_client.py chat_with_tools() — gateway with legacy fallback.
func (c *Client) ChatWithTools(ctx context.Context, req ChatToolsRequest) (*ChatToolsResponse, error) {
	if req.Temperature == 0 {
		req.Temperature = 0.2
	}
	if req.MaxTokens == 0 {
		req.MaxTokens = 4096
	}

	if c.gatewayAvailable(ctx) {
		payload := map[string]interface{}{
			"messages":    req.Messages,
			"tools":       req.Tools,
			"model":       req.Model,
			"provider":    req.Provider,
			"temperature": req.Temperature,
			"max_tokens":  req.MaxTokens,
			"think":       req.Think,
			"agent_role":  req.AgentRole,
		}
		if req.TaskID != "" {
			payload["task_id"] = req.TaskID
		}
		if req.PlanSessionID != "" {
			payload["plan_session_id"] = req.PlanSessionID
		}

		resp, err := c.postGateway(ctx, "/v1/chat/tools", payload, 180, req.WorkerName)
		if err != nil {
			flumelogger.WithContext(ctx).Warn("gateway chat_with_tools request failed, attempting fallback",
				slog.String("error", err.Error()),
				slog.String("model", req.Model),
			)

			fallback := ResolveFallback(ctx, req.Provider, req.Model, c.gatewayURL)
			if fallback != "" {
				flumelogger.WithContext(ctx).Warn("intelligently downgrading model for tool call",
					slog.String("from", req.Model),
					slog.String("to", fallback),
				)
				payload["model"] = fallback
				resp, err = c.postGateway(ctx, "/v1/chat/tools", payload, 180, req.WorkerName)
				if err == nil {
					return c.parseToolsResponse(resp)
				}
			}
		} else {
			respParsed, perr := c.parseToolsResponse(resp)
			if perr == nil && req.TaskID != "" {
				usage := respParsed.Usage
				pt := 0
				ct := 0
				if u, ok := usage["prompt_tokens"].(float64); ok {
					pt = int(u)
				}
				if u, ok := usage["completion_tokens"].(float64); ok {
					ct = int(u)
				}
				flumelogger.LogLLMCall(ctx, req.TaskID, req.Provider, req.Model, pt, ct, 0, nil,
					"tool_calls", len(respParsed.Message.ToolCalls))
			}
			return respParsed, perr
		}
	}

	// Legacy fallback: call the gateway directly but via legacy endpoints
	flumelogger.WithContext(ctx).Debug("using legacy direct provider call for tools")
	return c.legacyChatWithTools(ctx, req)
}

// ─── Gateway Communication ──────────────────────────────────────────────────

// gatewayAvailable checks if the gateway is healthy (cached for 30s).
// Derived from Python: llm_client.py _gateway_available().
func (c *Client) gatewayAvailable(ctx context.Context) bool {
	c.mu.RLock()
	ok := c.gatewayOK
	checkedAt := c.gatewayCheckedAt
	c.mu.RUnlock()

	if ok && time.Since(checkedAt) < 30*time.Second {
		return true
	}

	checkCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(checkCtx, http.MethodGet,
		c.gatewayURL+"/health", nil)
	if err != nil {
		c.cacheGatewayStatus(false)
		return false
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		c.cacheGatewayStatus(false)
		return false
	}
	defer resp.Body.Close()

	healthy := resp.StatusCode == 200
	c.cacheGatewayStatus(healthy)
	return healthy
}

func (c *Client) cacheGatewayStatus(ok bool) {
	c.mu.Lock()
	c.gatewayOK = ok
	c.gatewayCheckedAt = time.Now()
	c.mu.Unlock()
}

// postGateway POSTs JSON to the gateway with exponential backoff.
// Derived from Python: llm_client.py _post_gateway().
func (c *Client) postGateway(ctx context.Context, path string, payload interface{}, timeoutSec int, workerName string) (map[string]interface{}, error) {
	const maxRetries = 3
	backoffs := []time.Duration{30 * time.Second, 60 * time.Second, 120 * time.Second}

	data, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("llm: marshal failed: %w", err)
	}

	url := c.gatewayURL + path

	for attempt := 0; attempt <= maxRetries; attempt++ {
		reqCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSec)*time.Second)
		req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, url, bytes.NewReader(data))
		if err != nil {
			cancel()
			return nil, fmt.Errorf("llm: request build failed: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		
		wName := workerName
		if wName == "" {
			wName = c.workerName
		}
		req.Header.Set("X-Worker-Name", wName)

		resp, err := c.httpClient.Do(req)
		cancel()

		if err != nil {
			// Fast-fail on timeout errors: these indicate Ollama/gateway is overwhelmed
			// or unreachable. Retrying with 30/60/120s backoffs would burn 690s before
			// falling through to the legacy path. The Python v0.1.126 equivalent had NO
			// retry loop — it failed fast and fell back immediately.
			if errors.Is(err, context.DeadlineExceeded) {
				flumelogger.WithContext(ctx).Warn("gateway timed out — fast-failing to legacy path",
					slog.String("error", err.Error()),
					slog.Int("timeout_s", timeoutSec),
				)
				return nil, fmt.Errorf("llm: gateway timed out (fast-fail, no retry): %w", err)
			}

			// Retry on transient network errors (connection refused, DNS, etc.).
			if attempt < maxRetries {
				sleep := c.jitteredBackoff(backoffs, attempt)
				flumelogger.WithContext(ctx).Warn("gateway connection error, retrying",
					slog.Int("attempt", attempt+1),
					slog.Int("max_retries", maxRetries+1),
					slog.Float64("backoff_s", sleep.Seconds()),
					slog.String("error", err.Error()),
				)
				time.Sleep(sleep)
				continue
			}
			return nil, fmt.Errorf("llm: gateway permanently unreachable after %d retries: %w", maxRetries, err)
		}

		defer resp.Body.Close()

		if resp.StatusCode >= 400 && resp.StatusCode < 500 {
			body, _ := io.ReadAll(resp.Body)
			return nil, fmt.Errorf("llm: gateway rejected request (HTTP %d): %s",
				resp.StatusCode, truncateStr(string(body), 500))
		}

		if resp.StatusCode >= 500 {
			if attempt < maxRetries {
				sleep := c.jitteredBackoff(backoffs, attempt)
				flumelogger.WithContext(ctx).Warn("gateway server error, retrying",
					slog.Int("status", resp.StatusCode),
					slog.Int("attempt", attempt+1),
					slog.Float64("backoff_s", sleep.Seconds()),
				)
				time.Sleep(sleep)
				continue
			}
			body, _ := io.ReadAll(resp.Body)
			return nil, fmt.Errorf("llm: gateway HTTP %d after %d retries: %s",
				resp.StatusCode, maxRetries, truncateStr(string(body), 500))
		}

		var result map[string]interface{}
		if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
			return nil, fmt.Errorf("llm: gateway response decode failed: %w", err)
		}
		return result, nil
	}

	return nil, fmt.Errorf("llm: gateway exhausted all retries")
}

// postGatewayStream performs the POST and returns the raw *http.Response so the
// caller can incrementally decode NDJSON chunks. The caller is responsible for
// closing the response body.
//
// This is the foundation for restoring true streaming of agent reasoning from
// the gateway (and eventually from direct Ollama in the legacy path).
func (c *Client) postGatewayStream(ctx context.Context, path string, payload interface{}, timeoutSec int, workerName string) (*http.Response, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("llm: marshal failed: %w", err)
	}

	url := c.gatewayURL + path

	reqCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSec)*time.Second)
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		cancel()
		return nil, fmt.Errorf("llm: request build failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	wName := workerName
	if wName == "" {
		wName = c.workerName
	}
	req.Header.Set("X-Worker-Name", wName)
	// Signal streaming preference (in addition to body flag)
	req.Header.Set("Accept", "application/x-ndjson")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("llm: gateway stream request failed: %w", err)
	}

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		cancel()
		return nil, fmt.Errorf("llm: gateway stream HTTP %d: %s", resp.StatusCode, truncateStr(string(body), 500))
	}

	// IMPORTANT: Do NOT cancel here. The reqCtx (with its timeout) must remain
	// active while the caller reads the (potentially very long) streaming body.
	// Canceling immediately (as was done in non-stream postGateway) would cause
	// "context canceled" on the NDJSON decoder for slow/large generations.
	// The WithTimeout will fire after timeoutSec if the stream is still open.
	// Caller should consume until Done, at which point the timer can harmlessly fire later.
	return resp, nil
}

// ChatStream initiates a streaming chat request.
// Returns a channel that will yield ChatStreamChunk values (including a final
// Done chunk or an Error chunk). The channel is closed when the stream ends.
//
// For Phase 0 this exercises the full NDJSON writer path even though the
// gateway is still emitting a single terminal chunk. True incremental deltas
// arrive in Phase 1+.
func (c *Client) ChatStream(ctx context.Context, req ChatRequest) (<-chan ChatStreamChunk, error) {
	if req.Temperature == 0 {
		req.Temperature = 0.3
	}
	if req.MaxTokens == 0 {
		req.MaxTokens = 8192
	}
	timeout := req.TimeoutSeconds
	if timeout == 0 {
		timeout = 600 // Generous for streaming; the open connection + chunks keep it alive
	}

	ch := make(chan ChatStreamChunk, 16)

	if c.gatewayAvailable(ctx) {
		payload := map[string]interface{}{
			"messages":    req.Messages,
			"model":       req.Model,
			"provider":    req.Provider,
			"temperature": req.Temperature,
			"max_tokens":  req.MaxTokens,
			"think":       req.Think,
			"agent_role":  req.AgentRole,
			"stream":      true, // explicit opt-in
		}
		if req.TaskID != "" {
			payload["task_id"] = req.TaskID
		}
		if req.PlanSessionID != "" {
			payload["plan_session_id"] = req.PlanSessionID
		}

		resp, err := c.postGatewayStream(ctx, "/v1/chat", payload, timeout, req.WorkerName)
		if err != nil {
			close(ch)
			return nil, err
		}

		go func() {
			defer close(ch)
			defer resp.Body.Close()

			decoder := json.NewDecoder(resp.Body)
			for decoder.More() {
				var chunk ChatStreamChunk
				if err := decoder.Decode(&chunk); err != nil {
					ch <- ChatStreamChunk{Error: err.Error(), Done: true}
					return
				}
				ch <- chunk
				if chunk.Telemetry != nil {
					flumelogger.WithContext(ctx).Info("gateway telemetry retrieved (stream)",
						slog.String("node_id", strVal(chunk.Telemetry["node_id"])),
						slog.String("node_host", strVal(chunk.Telemetry["node_host"])),
					)
				}
				if chunk.Done || chunk.Error != "" {
					return
				}
			}
		}()

		return ch, nil
	}

	// Legacy direct path — for Phase 0 we simply don't support streaming yet
	// (or we could wire legacyChat into a channel, but keep it simple).
	close(ch)
	return nil, fmt.Errorf("llm: streaming not yet available on legacy direct path (use gateway)")
}

// jitteredBackoff returns the backoff duration with ±10% jitter.
// Derived from Python: llm_client.py backoff jitter logic.
func (c *Client) jitteredBackoff(backoffs []time.Duration, attempt int) time.Duration {
	idx := attempt
	if idx >= len(backoffs) {
		idx = len(backoffs) - 1
	}
	base := backoffs[idx]
	jitter := float64(base) * 0.1 * (rand.Float64()*2 - 1)
	result := time.Duration(float64(base) + jitter)
	if result < time.Second {
		result = time.Second
	}
	return result
}

// ─── Response Parsing ───────────────────────────────────────────────────────

func (c *Client) parseChatResponse(ctx context.Context, raw map[string]interface{}) (*ChatResponse, error) {
	msg, _ := raw["message"].(map[string]interface{})
	content, _ := msg["content"].(string)

	telemetry, _ := raw["telemetry"].(map[string]interface{})
	if telemetry != nil {
		flumelogger.WithContext(ctx).Info("gateway telemetry retrieved",
			slog.String("node_id", strVal(telemetry["node_id"])),
			slog.String("node_host", strVal(telemetry["node_host"])),
		)
	}

	usage, _ := raw["usage"].(map[string]interface{})
	return &ChatResponse{
		Content:   content,
		Usage:     usage,
		Telemetry: telemetry,
	}, nil
}

func (c *Client) parseToolsResponse(raw map[string]interface{}) (*ChatToolsResponse, error) {
	msg, _ := raw["message"].(map[string]interface{})
	content, _ := msg["content"].(string)

	var toolCalls []ToolCall
	if tcRaw, ok := msg["tool_calls"].([]interface{}); ok {
		for _, tc := range tcRaw {
			tcMap, _ := tc.(map[string]interface{})
			fn, _ := tcMap["function"].(map[string]interface{})
			toolCalls = append(toolCalls, ToolCall{
				ID:   strVal(tcMap["id"]),
				Type: strVal(tcMap["type"]),
				Function: ToolCallFunction{
					Name:      strVal(fn["name"]),
					Arguments: fn["arguments"],
				},
			})
		}
	}

	telemetry, _ := raw["telemetry"].(map[string]interface{})
	usage, _ := raw["usage"].(map[string]interface{})

	return &ChatToolsResponse{
		Message: ToolMessage{
			Role:      "assistant",
			Content:   content,
			ToolCalls: toolCalls,
		},
		Usage:     usage,
		Telemetry: telemetry,
	}, nil
}

// ─── Legacy Direct Provider Fallback ────────────────────────────────────────

// legacyChat falls back to direct Ollama calls when the gateway is unavailable.
// Mirrors the Python worker's direct-provider fallback path.
//
// CRITICAL: Uses stream:true + NDJSON aggregation, matching both the Python
// implementation (_ollama_stream_strip_think) and the Go gateway (StreamOllamaChat).
// The original non-streaming version caused "context deadline exceeded" errors
// because thinking models (qwen3.5:35b-a3b etc.) can take >120s to produce
// a complete non-streamed response. Streaming keeps the HTTP connection alive
// with periodic chunks, eliminating the timeout.
func (c *Client) legacyChat(ctx context.Context, req ChatRequest) (*ChatResponse, error) {
	baseURL := c.resolveOllamaBaseURL()
	if baseURL == "" {
		return nil, fmt.Errorf("%w: gateway is unreachable and no Ollama base URL is configured; "+
			"set LLM_BASE_URL or LOCAL_OLLAMA_BASE_URL, or ensure the gateway is running: docker compose up gateway", ErrPersistentConfig)
	}

	model := req.Model
	if model == "" {
		model = os.Getenv("LLM_MODEL")
	}
	if model == "" {
		model = "qwen3.5:35b-a3b" // deployment default from flume start banner
	}

	c.logger.Warn("legacy fallback: direct Ollama call (gateway unavailable)",
		slog.String("base_url", baseURL),
		slog.String("model", model),
	)

	// Build Ollama-native payload with stream:true (critical for thinking models).
	// Python equivalent: _ollama_stream_strip_think() in llm_client_legacy.py.
	payload := map[string]interface{}{
		"model":      model,
		"messages":   req.Messages,
		"stream":     true, // Keeps connection alive — prevents timeout on large responses
		"keep_alive": "24h", // Prevent model cold starts between requests (Ollama default is 5m)
		"options": map[string]interface{}{
			"temperature": req.Temperature,
			"num_predict": req.MaxTokens,
			"num_ctx":     8192,
		},
	}

	data, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("llm: legacy marshal failed: %w", err)
	}

	// Strip common "/v1" or "/v1/" suffixes that come from the OpenAI-compatible
	// wrapper URL (LOCAL_OLLAMA_BASE_URL). Ollama native API uses /api/chat directly.
	cleanBase := strings.TrimRight(baseURL, "/")
	cleanBase = strings.TrimSuffix(cleanBase, "/v1")
	url := cleanBase + "/api/chat"

	// 600s (10-min) safety net. Go's context.WithTimeout is an ABSOLUTE wall-clock
	// deadline (unlike Python's socket idle timeout which resets on each byte).
	// The old 300s was too tight: model cold starts can take 60-120s of silence
	// before the first streaming chunk, leaving only 180-240s for actual generation.
	// 600s provides headroom for cold start + full generation of thinking models.
	reqCtx, cancel := context.WithTimeout(ctx, 600*time.Second)
	defer cancel()

	httpReq, err := http.NewRequestWithContext(reqCtx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("llm: legacy request build failed: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("llm: legacy direct Ollama call failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("llm: legacy Ollama HTTP %d: %s", resp.StatusCode, truncateStr(string(body), 500))
	}

	// Read NDJSON stream and aggregate content chunks.
	// Each line is a JSON object with {"message":{"content":"..."},"done":false/true}.
	// Think blocks (<think>...</think>) are stripped post-aggregation.
	var contentBuilder strings.Builder
	decoder := json.NewDecoder(resp.Body)
	for decoder.More() {
		var chunk map[string]interface{}
		if err := decoder.Decode(&chunk); err != nil {
			// Partial read is OK — use what we have
			c.logger.Warn("legacy stream: decode error (using accumulated content)",
				slog.String("error", err.Error()))
			break
		}

		// Extract content from this chunk
		if msg, ok := chunk["message"].(map[string]interface{}); ok {
			if content, ok := msg["content"].(string); ok {
				contentBuilder.WriteString(content)
			}
		}

		// Check if this is the final chunk
		if done, ok := chunk["done"].(bool); ok && done {
			break
		}
	}

	content := contentBuilder.String()

	// Strip <think> blocks if present (matches Python _strip_think_blocks)
	content = stripThinkBlocks(content)

	return &ChatResponse{
		Content: strings.TrimSpace(content),
	}, nil
}

func (c *Client) legacyChatWithTools(ctx context.Context, req ChatToolsRequest) (*ChatToolsResponse, error) {
	baseURL := c.resolveOllamaBaseURL()
	if baseURL == "" {
		return nil, fmt.Errorf("%w: gateway is unreachable and no Ollama base URL is configured; "+
			"set LLM_BASE_URL or LOCAL_OLLAMA_BASE_URL, or ensure the gateway is running: docker compose up gateway", ErrPersistentConfig)
	}

	model := req.Model
	if model == "" {
		model = os.Getenv("LLM_MODEL")
	}
	if model == "" {
		model = "qwen3.5:35b-a3b"
	}

	c.logger.Warn("legacy fallback: direct Ollama tool call (gateway unavailable)",
		slog.String("base_url", baseURL),
		slog.String("model", model),
	)

	// Build Ollama-native payload with tools + stream:true
	// (same streaming fix as legacyChat — prevents timeout on thinking models)
	payload := map[string]interface{}{
		"model":      model,
		"messages":   req.Messages,
		"tools":      req.Tools,
		"stream":     true,
		"keep_alive": "24h", // Prevent model cold starts between requests (Ollama default is 5m)
		"options": map[string]interface{}{
			"temperature": req.Temperature,
			"num_predict": req.MaxTokens,
			"num_ctx":     8192,
		},
	}

	data, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("llm: legacy marshal failed: %w", err)
	}

	// Strip common "/v1" suffix — same fix as legacyChat (Ollama native API is /api/chat, not /v1/api/chat)
	cleanBase := strings.TrimRight(baseURL, "/")
	cleanBase = strings.TrimSuffix(cleanBase, "/v1")
	url := cleanBase + "/api/chat"
	// 600s safety net — matches legacyChat (see comment there for rationale).
	reqCtx, cancel := context.WithTimeout(ctx, 600*time.Second)
	defer cancel()

	httpReq, err := http.NewRequestWithContext(reqCtx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("llm: legacy request build failed: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("llm: legacy direct Ollama tool call failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("llm: legacy Ollama HTTP %d: %s", resp.StatusCode, truncateStr(string(body), 500))
	}

	// Read NDJSON stream — aggregate content and capture tool_calls from final chunk.
	// Ollama emits tool_calls in the done:true chunk's message object.
	var contentBuilder strings.Builder
	var toolCalls []ToolCall
	decoder := json.NewDecoder(resp.Body)
	for decoder.More() {
		var chunk map[string]interface{}
		if err := decoder.Decode(&chunk); err != nil {
			c.logger.Warn("legacy tool stream: decode error (using accumulated content)",
				slog.String("error", err.Error()))
			break
		}

		if msg, ok := chunk["message"].(map[string]interface{}); ok {
			// Accumulate content
			if content, ok := msg["content"].(string); ok {
				contentBuilder.WriteString(content)
			}
			// Extract tool_calls (present in the final chunk)
			if tcs, ok := msg["tool_calls"].([]interface{}); ok {
				for _, tc := range tcs {
					tcMap, _ := tc.(map[string]interface{})
					fn, _ := tcMap["function"].(map[string]interface{})
					toolCalls = append(toolCalls, ToolCall{
						Function: ToolCallFunction{
							Name:      strVal(fn["name"]),
							Arguments: fn["arguments"],
						},
					})
				}
			}
		}

		if done, ok := chunk["done"].(bool); ok && done {
			break
		}
	}

	content := stripThinkBlocks(contentBuilder.String())

	return &ChatToolsResponse{
		Message: ToolMessage{
			Role:      "assistant",
			Content:   strings.TrimSpace(content),
			ToolCalls: toolCalls,
		},
	}, nil
}

// resolveOllamaBaseURL returns the direct Ollama URL for the legacy fallback path.
// Checks LOCAL_OLLAMA_BASE_URL first (set by flume start for Docker envs),
// then LLM_BASE_URL, then LLM_HOST with default port.
func (c *Client) resolveOllamaBaseURL() string {
	if v := os.Getenv("LOCAL_OLLAMA_BASE_URL"); v != "" {
		return v
	}
	if v := os.Getenv("LLM_BASE_URL"); v != "" {
		return v
	}
	if v := os.Getenv("LLM_HOST"); v != "" {
		if !strings.HasPrefix(v, "http") {
			v = "http://" + v
		}
		if !strings.Contains(v[8:], ":") { // no port after http://
			v += ":11434"
		}
		return v
	}
	return ""
}

// stripThinkBlocks removes <think>...</think> blocks from Ollama responses.
// This is the simplified version of the gateway's ThinkMill for the fallback path.
func stripThinkBlocks(s string) string {
	for {
		start := strings.Index(s, "<think>")
		if start == -1 {
			return s
		}
		end := strings.Index(s[start:], "</think>")
		if end == -1 {
			// Unterminated <think> block — strip from <think> to end
			return s[:start]
		}
		s = s[:start] + s[start+end+len("</think>"):]
	}
}

// ─── Helpers ────────────────────────────────────────────────────────────────

func strVal(v interface{}) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

func truncateStr(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen]
}

// Embed returns the vector embedding for the input text using the gateway.
func (c *Client) Embed(ctx context.Context, text string, model string, provider string) ([]float64, error) {
	if !c.gatewayAvailable(ctx) {
		return nil, fmt.Errorf("llm: gateway unavailable for embeddings")
	}

	payload := map[string]interface{}{
		"input":    text,
		"model":    model,
		"provider": provider,
	}

	res, err := c.postGateway(ctx, "/v1/embeddings", payload, 30, "")
	if err != nil {
		return nil, err
	}

	rawEmbed, ok := res["embedding"].([]interface{})
	if !ok {
		return nil, fmt.Errorf("llm: invalid embedding response format")
	}

	embedding := make([]float64, len(rawEmbed))
	for i, v := range rawEmbed {
		if f, ok := v.(float64); ok {
			embedding[i] = f
		} else {
			return nil, fmt.Errorf("llm: non-float64 value in embedding array")
		}
	}

	return embedding, nil
}
