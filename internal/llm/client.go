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
	TaskID           string    `json:"task_id,omitempty"`
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
		gatewayURL = "http://gateway:8090"
	}
	gatewayURL = strings.TrimRight(gatewayURL, "/")

	workerName := os.Getenv("FLUME_WORKER_NAME")
	if workerName == "" {
		workerName = "go-worker"
	}

	return &Client{
		gatewayURL: gatewayURL,
		httpClient: &http.Client{
			Timeout: 180 * time.Second,
		},
		logger:     logger,
		workerName: workerName,
	}
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
		timeout = 120
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

		resp, err := c.postGateway(ctx, "/v1/chat", payload, timeout)
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
				resp, err = c.postGateway(ctx, "/v1/chat", payload, timeout)
				if err == nil {
					return c.parseChatResponse(ctx, resp)
				}
				flumelogger.WithContext(ctx).Warn("fallback model also failed",
					slog.String("error", err.Error()),
				)
			}

			// Fall through to legacy
		} else {
			return c.parseChatResponse(ctx, resp)
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

		resp, err := c.postGateway(ctx, "/v1/chat/tools", payload, 180)
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
				resp, err = c.postGateway(ctx, "/v1/chat/tools", payload, 180)
				if err == nil {
					return c.parseToolsResponse(resp)
				}
			}
		} else {
			return c.parseToolsResponse(resp)
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
func (c *Client) postGateway(ctx context.Context, path string, payload interface{}, timeoutSec int) (map[string]interface{}, error) {
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
		req.Header.Set("X-Worker-Name", c.workerName)

		resp, err := c.httpClient.Do(req)
		cancel()

		if err != nil {
			// Timeout errors on long requests (>= 60s) are not retried
			if timeoutSec >= 60 {
				return nil, fmt.Errorf("llm: gateway timeout after %ds: %w", timeoutSec, err)
			}
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

// legacyChat falls back to direct LLM calls when the gateway is unavailable.
// In the Go binary, this posts to the gateway anyway since the gateway IS the
// provider router. If the gateway is truly down, we return an error.
func (c *Client) legacyChat(ctx context.Context, req ChatRequest) (*ChatResponse, error) {
	// In Go, the gateway IS the provider router. There's no separate "legacy"
	// module to import. If the gateway is down, we return an error.
	return nil, fmt.Errorf("llm: gateway is unreachable and no legacy fallback is available in Go binary; " +
		"ensure the gateway is running: docker compose up gateway")
}

func (c *Client) legacyChatWithTools(ctx context.Context, req ChatToolsRequest) (*ChatToolsResponse, error) {
	return nil, fmt.Errorf("llm: gateway is unreachable and no legacy fallback is available in Go binary; " +
		"ensure the gateway is running: docker compose up gateway")
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
