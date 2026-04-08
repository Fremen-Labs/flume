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
	"strings"
)

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
	Done  bool `json:"done"`
	Error string `json:"error,omitempty"`
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

	client := &http.Client{
		// No timeout here — streaming keeps the connection alive.
		// Context cancellation handles the abort case.
	}
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
			mill.Process([]byte(chunk.Message.Content))
		}

		// Capture tool calls from the final chunk
		if len(chunk.Message.ToolCalls) > 0 {
			toolCalls = chunk.Message.ToolCalls
		}

		if chunk.Done {
			break
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("stream read: %w", err)
	}

	log.Info("ollama tool stream completed",
		slog.Int("chunks", chunkCount),
		slog.Int("tool_calls", len(toolCalls)),
		slog.Int("visible_chars", len(mill.Visible())),
	)

	return &ChatResponse{
		Message: ResponseMessage{
			Role:      "assistant",
			Content:   mill.Visible(),
			ToolCalls: toolCalls,
			Thoughts:  mill.Thoughts(),
		},
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
) (string, string, error) {
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
		return "", "", fmt.Errorf("marshal payload: %w", err)
	}

	url := strings.TrimRight(baseURL, "/") + "/api/chat"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", "", fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("ollama request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return "", "", fmt.Errorf("ollama HTTP %d: %s", resp.StatusCode, string(errBody))
	}

	mill := NewThinkMill()
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
			return "", "", fmt.Errorf("ollama error: %s", chunk.Error)
		}

		if chunk.Message.Content != "" {
			mill.Process([]byte(chunk.Message.Content))
		}

		if chunk.Done {
			break
		}
	}

	if err := scanner.Err(); err != nil {
		return "", "", fmt.Errorf("stream read: %w", err)
	}

	log.Debug("ollama chat stream completed",
		slog.Int("chunks", chunkCount),
		slog.Int("visible_chars", len(mill.Visible())),
		slog.Int("thought_chars", len(mill.Thoughts())),
	)

	return mill.Visible(), mill.Thoughts(), nil
}
