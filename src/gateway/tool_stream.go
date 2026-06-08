package gateway

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
)

// CleanJSONResponse heuristically strips conversational wrapper text or markdown
// fences from raw LLM strings to expose the underlying JSON structured object.
func CleanJSONResponse(raw string) string {
	s := strings.TrimSpace(raw)

	// 1. Check for exact code fences anywhere
	if strings.Contains(s, "```json") {
		first := strings.Index(s, "```json")
		after := s[first+7:]
		if end := strings.LastIndex(after, "```"); end != -1 {
			return strings.TrimSpace(after[:end])
		}
	}

	// Optional fallback code fence if they used ``` without json
	if strings.HasPrefix(s, "```") && strings.HasSuffix(s, "```") {
		s = strings.TrimPrefix(s, "```")
		s = strings.TrimSuffix(s, "```")
		return strings.TrimSpace(s)
	}

	// 2. Direct bracket wrapping (already valid or close to it)
	if (strings.HasPrefix(s, "{") && strings.HasSuffix(s, "}")) ||
		(strings.HasPrefix(s, "[") && strings.HasSuffix(s, "]")) {
		return s
	}

	// 3. Fallback: Find the largest bounding box of { ... } or [ ... ]
	firstBrace := strings.Index(s, "{")
	lastBrace := strings.LastIndex(s, "}")

	firstBracket := strings.Index(s, "[")
	lastBracket := strings.LastIndex(s, "]")

	hasObj := firstBrace != -1 && lastBrace != -1 && lastBrace > firstBrace
	hasArr := firstBracket != -1 && lastBracket != -1 && lastBracket > firstBracket

	if hasObj && hasArr {
		if firstBrace < firstBracket && lastBrace > lastBracket {
			return strings.TrimSpace(s[firstBrace : lastBrace+1])
		}
		if firstBracket < firstBrace && lastBracket > lastBrace {
			return strings.TrimSpace(s[firstBracket : lastBracket+1])
		}
	} else if hasObj {
		return strings.TrimSpace(s[firstBrace : lastBrace+1])
	} else if hasArr {
		return strings.TrimSpace(s[firstBracket : lastBracket+1])
	}

	// If all else fails, return the original string
	return s
}

// ─────────────────────────────────────────────────────────────────────────────
// Tool Stream — the core fix for agent blockages.
//
// When Ollama receives a request with tools and stream:false, thinking models
// (gemma4, qwq, deepseek-r1) can block for 2-5 minutes before responding,
// causing urllib timeouts in the Python worker and cascading into 10
// consecutive failures → task blocked.
//
// This module sends stream:true to Ollama and aggregates the NDJSON chunks
// in real-time.  The HTTP connection stays alive (no timeout), think blocks
// are stripped mid-stream, and the final tool_calls array is extracted from
// the last chunk.
// ─────────────────────────────────────────────────────────────────────────────

// ollamaStreamChunk represents a single NDJSON line from Ollama's streaming API.
type ollamaStreamChunk struct {
	Message struct {
		Role      string     `json:"role"`
		Content   string     `json:"content"`
		ToolCalls []ToolCall `json:"tool_calls,omitempty"`
	} `json:"message"`
	Done               bool   `json:"done"`
	Error              string `json:"error,omitempty"`
	PromptEvalCount    int    `json:"prompt_eval_count,omitempty"`
	EvalCount          int    `json:"eval_count,omitempty"`
	TotalDuration      int64  `json:"total_duration,omitempty"`
	LoadDuration       int64  `json:"load_duration,omitempty"`
	PromptEvalDuration int64  `json:"prompt_eval_duration,omitempty"`
	EvalDuration       int64  `json:"eval_duration,omitempty"`
}

// StreamOllamaToolCall sends a tool-call request to Ollama using stream:true
// and aggregates the response, returning a unified ChatResponse.
func StreamOllamaToolCall(
	ctx context.Context,
	baseURL string,
	messages []Message,
	tools []Tool,
	model string,
	options map[string]interface{},
	authToken string, // Phase 2: for local node Bearer auth (from node.AuthToken); opt-in only
) (*ChatResponse, error) {
	log := WithContext(ctx)
	defer LogDuration(ctx, "ollama_tool_stream")()

	// Build the Ollama payload with stream:true
	payload := map[string]interface{}{
		"model":    model,
		"messages": messages,
		"tools":    tools,
		"stream":   true,
		"options":  options,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal payload: %w", err)
	}

	url := strings.TrimRight(baseURL, "/") + "/api/chat"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	// Phase 2: Bearer injection for local nodes (from authToken passed by caller).
	// Strictly conditional — if "", no header (default for vast majority of local Ollama users).
	if authToken != "" {
		req.Header.Set("Authorization", "Bearer "+authToken)
	}

	// Phase 1: Use NodeConnManager for shared per-node Transport (keepalives, ForceAttemptHTTP2).
	// (Previously a bare client + placeholder call; now actually uses the managed client.)
	client := getOllamaStreamClientForBase(baseURL)
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ollama request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return nil, fmt.Errorf("ollama HTTP %d: %s", resp.StatusCode, string(errBody))
	}

	// Process the NDJSON stream
	mill := NewThinkMill()
	var toolCalls []ToolCall
	var usage Usage
	chunkCount := 0

	scanner := bufio.NewScanner(resp.Body)
	// Increase buffer for large tool responses
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}

		var chunk ollamaStreamChunk
		if err := json.Unmarshal(line, &chunk); err != nil {
			log.Debug("skipping unparseable NDJSON line",
				slog.String("error", err.Error()),
				slog.Int("line_len", len(line)),
			)
			continue
		}

		chunkCount++

		if chunk.Error != "" {
			return nil, fmt.Errorf("ollama error: %s", chunk.Error)
		}

		// Feed content through the think mill
		if chunk.Message.Content != "" {
			content := chunk.Message.Content
			stopIdx := -1
			var foundStop string
			for _, stop := range defaultOllamaStopTokensList {
				if idx := strings.Index(content, stop); idx != -1 {
					if stopIdx == -1 || idx < stopIdx {
						stopIdx = idx
						foundStop = stop
					}
				}
			}

			if stopIdx != -1 {
				log.Info("stream scanner encountered stop token",
					slog.String("stop_token", foundStop),
					slog.Int("index", stopIdx),
				)
				if stopIdx > 0 {
					mill.Process([]byte(content[:stopIdx]))
				}
				break
			}

			mill.Process([]byte(content))
		}

		// Capture tool calls from the final chunk
		if len(chunk.Message.ToolCalls) > 0 {
			toolCalls = chunk.Message.ToolCalls
		}

		if chunk.PromptEvalCount > 0 {
			usage.PromptTokens = chunk.PromptEvalCount
		}
		if chunk.EvalCount > 0 {
			usage.CompletionTokens = chunk.EvalCount
		}
		if chunk.TotalDuration > 0 {
			usage.TotalDurationNs = chunk.TotalDuration
		}
		if chunk.LoadDuration > 0 {
			usage.LoadDurationNs = chunk.LoadDuration
		}
		if chunk.PromptEvalDuration > 0 {
			usage.PromptEvalDurationNs = chunk.PromptEvalDuration
		}
		if chunk.EvalDuration > 0 {
			usage.EvalDurationNs = chunk.EvalDuration
		}

		if chunk.Done {
			break
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("stream read: %w", err)
	}

	usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens

	log.Info("ollama tool stream completed",
		slog.Int("chunks", chunkCount),
		slog.Int("tool_calls", len(toolCalls)),
		slog.Int("visible_chars", len(mill.Visible())),
		slog.Int("prompt_tokens", usage.PromptTokens),
		slog.Int("completion_tokens", usage.CompletionTokens),
	)

	visible := mill.Visible()
	visible = truncateAtStopTokens(visible)

	return &ChatResponse{
		Message: ResponseMessage{
			Role:      "assistant",
			Content:   visible,
			ToolCalls: toolCalls,
			Thoughts:  mill.Thoughts(),
		},
		Usage: usage,
	}, nil
}

// StreamOllamaChat sends a plain chat request to Ollama using stream:true
// and strips think blocks, returning the visible content and thoughts.
func StreamOllamaChat(
	ctx context.Context,
	baseURL string,
	messages []Message,
	model string,
	options map[string]interface{},
	authToken string, // Phase 2: for local node Bearer auth (from node.AuthToken)
) (string, string, Usage, error) {
	log := WithContext(ctx)
	defer LogDuration(ctx, "ollama_chat_stream")()

	payload := map[string]interface{}{
		"model":    model,
		"messages": messages,
		"stream":   true,
		"options":  options,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return "", "", Usage{}, fmt.Errorf("marshal payload: %w", err)
	}

	url := strings.TrimRight(baseURL, "/") + "/api/chat"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", "", Usage{}, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	// Phase 2: Bearer injection for local nodes (from authToken passed by caller).
	if authToken != "" {
		req.Header.Set("Authorization", "Bearer "+authToken)
	}

	// Phase 1: Use NodeConnManager for shared per-node Transport (keepalives, ForceAttemptHTTP2).
	// This eliminates repeated handshakes for the distributed mesh (core of the optimization report).
	// TODO(Phase 1 wiring): wire a real manager instance from server/router instead of package singleton.
	// For skeleton we use a package-level one (explicit construction in real server init).
	client := getOllamaStreamClientForBase(baseURL) // uses conn manager under the hood
	resp, err := client.Do(req)
	if err != nil {
		return "", "", Usage{}, fmt.Errorf("ollama request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return "", "", Usage{}, fmt.Errorf("ollama HTTP %d: %s", resp.StatusCode, string(errBody))
	}

	mill := NewThinkMill()
	var usage Usage
	chunkCount := 0

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}

		var chunk ollamaStreamChunk
		if err := json.Unmarshal(line, &chunk); err != nil {
			continue
		}
		chunkCount++

		if chunk.Error != "" {
			return "", "", Usage{}, fmt.Errorf("ollama error: %s", chunk.Error)
		}

		if chunk.Message.Content != "" {
			content := chunk.Message.Content
			stopIdx := -1
			var foundStop string
			for _, stop := range defaultOllamaStopTokensList {
				if idx := strings.Index(content, stop); idx != -1 {
					if stopIdx == -1 || idx < stopIdx {
						stopIdx = idx
						foundStop = stop
					}
				}
			}

			if stopIdx != -1 {
				log.Info("stream scanner encountered stop token",
					slog.String("stop_token", foundStop),
					slog.Int("index", stopIdx),
				)
				if stopIdx > 0 {
					mill.Process([]byte(content[:stopIdx]))
				}
				break
			}

			mill.Process([]byte(content))
		}

		if chunk.PromptEvalCount > 0 {
			usage.PromptTokens = chunk.PromptEvalCount
		}
		if chunk.EvalCount > 0 {
			usage.CompletionTokens = chunk.EvalCount
		}
		if chunk.TotalDuration > 0 {
			usage.TotalDurationNs = chunk.TotalDuration
		}
		if chunk.LoadDuration > 0 {
			usage.LoadDurationNs = chunk.LoadDuration
		}
		if chunk.PromptEvalDuration > 0 {
			usage.PromptEvalDurationNs = chunk.PromptEvalDuration
		}
		if chunk.EvalDuration > 0 {
			usage.EvalDurationNs = chunk.EvalDuration
		}

		if chunk.Done {
			break
		}
	}

	if err := scanner.Err(); err != nil {
		return "", "", Usage{}, fmt.Errorf("stream read: %w", err)
	}

	usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens

	log.Debug("ollama chat stream completed",
		slog.Int("chunks", chunkCount),
		slog.Int("visible_chars", len(mill.Visible())),
		slog.Int("thought_chars", len(mill.Thoughts())),
		slog.Int("prompt_tokens", usage.PromptTokens),
		slog.Int("completion_tokens", usage.CompletionTokens),
	)

	visible := mill.Visible()
	visible = truncateAtStopTokens(visible)

	return CleanJSONResponse(visible), mill.Thoughts(), usage, nil
}

// === Phase 1: NodeConnManager integration helpers (skeleton) ===
//
// These bridge the existing stream paths to the new per-node conn manager
// (see node_conn_manager.go and phases doc).
//
// In full Phase 1 wiring (next edits): pass a *NodeConnManager from the
// gateway Server / ProviderRouter (explicit, per SKILLs) instead of package var.
// For now, a singleton lets us start refactoring without big wiring changes.

var (
	ollamaConnMgr     = NewNodeConnManager()
	ollamaConnMgrOnce sync.Once
)

// getOllamaStreamClientForBase returns a client that reuses a managed Transport
// for the given base (in real use, we'll key by node.ID after SelectNode).
// This is the entry point exercised by StreamOllama* after Phase 1 edits.
func getOllamaStreamClientForBase(baseURL string) *http.Client {
	ollamaConnMgrOnce.Do(func() {
		// Ensure initialized (safe).
		_ = ollamaConnMgr
	})

	// For skeleton: use a synthetic node keyed by host part of URL.
	// Real impl (Phase 1 continuation): pass *Node from routeToNode / ollamaWithNode.
	host := "default"
	if baseURL != "" {
		// crude parse for demo; real code uses node.Host
		if u, err := url.Parse(baseURL); err == nil && u.Host != "" {
			host = u.Host
		}
	}
	synthNode := &Node{ID: "ollama-" + host, Host: host}

	// Use 0 timeout - rely on ctx (Phase 1 unification goal).
	return ollamaConnMgr.GetClientForNode(synthNode, 0)
}

// Note: import "net/url" and "sync" may be needed at top if not present.
// (We will clean in compile check.)

// End of Phase 1 skeleton in tool_stream.go. Continue integration in providers.go
// and server init. See todo for remaining Phase 1 steps (always-stream force,
// ctx timeout unification, stats exposure, tests).

// OllamaConnStats exposes the (package) NodeConnManager stats for Phase 4 observability
// (used by BuildLiveGatewayMetrics + /api/gateway-metrics to surface conn reuse in dashboard).
// In full explicit wiring the Server's connMgr.Stats() would be preferred, but this keeps
// the skeleton working without changing all call sites yet. Stats include config + (future) counters.
func OllamaConnStats() map[string]map[string]int {
	if ollamaConnMgr == nil {
		return nil
	}
	return ollamaConnMgr.Stats()
}

var defaultOllamaStopTokensList = []string{
	"<|endoftext|>",
	"<|im_start|>",
	"<|im_end|>",
	"<im_start>",
	"<im_end>",
	"<|eot_id|>",
	"<|start_header_id|>",
	"assistant\n\n<tool_call",
	"assistant\n<tool_call",
}

func truncateAtStopTokens(content string) string {
	stopIdx := -1
	for _, stop := range defaultOllamaStopTokensList {
		if idx := strings.Index(content, stop); idx != -1 {
			if stopIdx == -1 || idx < stopIdx {
				stopIdx = idx
			}
		}
	}
	if stopIdx != -1 {
		return content[:stopIdx]
	}
	return content
}
