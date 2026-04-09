package gateway

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"golang.org/x/sync/errgroup"
)

// ExecuteEnsemble coordinates parallel local generation and fallback degradation.
func (s *Server) ExecuteEnsemble(ctx context.Context, req *ChatRequest) (*ChatResponse, error) {
	log := WithContext(ctx)

	size := s.config.EnsembleSize
	if size <= 1 {
		// Degrade to straight-through if size misconfigured
		return s.router.Route(ctx, req, true)
	}

	log.Info("starting LLM jury ensemble",
		slog.Int("size", size),
		slog.Float64("base_temperature", req.Temperature),
		slog.String("model", req.Model),
	)

	// Context timeout (2 minutes)
	timeoutCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()

	g, gCtx := errgroup.WithContext(timeoutCtx)

	responses := make([]*ChatResponse, size)
	errors := make([]error, size)
	durations := make([]time.Duration, size)

	for i := 0; i < size; i++ {
		i := i // loop capture
		g.Go(func() error {
			start := time.Now()

			// Deep clone request to avoid concurrent map/slice writes
			cloneReq := cloneChatRequest(req)

			// Vary temperature (e.g., base + 0.2, +0.4, etc.)
			temp := cloneReq.Temperature + float64(i)*0.2
			if temp > 1.0 {
				temp = 1.0
			}
			cloneReq.Temperature = temp

			resp, err := s.router.Route(gCtx, cloneReq, true)

			errors[i] = err
			responses[i] = resp
			durations[i] = time.Since(start)
			return nil // Never fail the group, we collect all attempts
		})
	}

	_ = g.Wait()

	// Evaluate
	bestScore := -1
	var bestResp *ChatResponse

	totalScore := 0
	validResponses := 0
	winnerVariance := 0

	for i, resp := range responses {
		if errors[i] != nil || resp == nil {
			log.Warn("jury member failed", slog.Int("index", i), slog.Any("error", errors[i]))
			continue
		}

		score := ScoreResponse(resp)
		totalScore += score
		validResponses++

		log.Info("jury member evaluated",
			slog.Int("index", i),
			slog.Int("score", score),
			slog.Float64("duration_ms", float64(durations[i].Milliseconds())),
		)

		if score > bestScore {
			// Track variance based on how much the new best jumps
			if bestScore != -1 {
				winnerVariance = score - bestScore
			}
			bestScore = score
			bestResp = resp
		}
	}

	if validResponses > 0 {
		avgScore := totalScore / validResponses
		log.Info("ensemble completed",
			slog.Int("best_score", bestScore),
			slog.Int("average_score", avgScore),
			slog.Int("valid_responses", validResponses),
			slog.Int("winner_variance", winnerVariance),
		)
	} else {
		log.Warn("ensemble entire failure, all jury members errored")
	}

	// Thresholding & Escalation
	if bestScore < 70 {
		fallback := s.config.FrontierFallbackModel
		if fallback == "" {
			fallback = "gpt-4o"
		}

		log.Warn("local confidence critically low",
			slog.Int("score", bestScore),
			slog.String("escalating_to", fallback),
		)

		frontierReq := cloneChatRequest(req)
		frontierReq.Model = fallback
		frontierReq.Provider = "" // Will be re-resolved in Route based on rules

		frontierResp, err := s.router.Route(ctx, frontierReq, true)
		if err == nil {
			log.Info("frontier escalation successful")
			return frontierResp, nil
		}

		log.Error("frontier fallback failed, gracefully degrading to best local attempt", slog.Any("error", err))
		if bestResp != nil {
			return bestResp, nil
		}
		return nil, err
	}

	return bestResp, nil
}

// ScoreResponse applies zero-LLM heuristics to grade a response (0-100).
func ScoreResponse(resp *ChatResponse) int {
	score := 0
	toolCount := len(resp.Message.ToolCalls)

	// Has tool calls at all?
	if toolCount == 0 {
		// Normal chat fallback, moderate score
		return 60
	}

	scorePerTool := 100 / toolCount
	baseSyntax := (40 * scorePerTool) / 100
	baseSafety := (30 * scorePerTool) / 100
	baseElastro := (30 * scorePerTool) / 100

	// Syntax & parsing validity
	for _, tc := range resp.Message.ToolCalls {
		rawArgs := tc.Function.Arguments

		// Capture strings/bytes
		var argBytes []byte
		switch v := rawArgs.(type) {
		case string:
			argBytes = []byte(v)
		case []byte:
			argBytes = v
		default:
			b, _ := json.Marshal(v)
			argBytes = b
		}

		// Perfect JSON
		var placeholder map[string]interface{}
		if json.Valid(argBytes) && json.Unmarshal(argBytes, &placeholder) == nil {
			score += baseSyntax
		}

		// Blast radius constraints
		isSafe := true
		name := tc.Function.Name

		if name == "run_shell_cmd" || name == "run_command" {
			cmdStr := string(argBytes)
			if strings.Contains(cmdStr, "rm -rf") || strings.Contains(cmdStr, "sudo") || strings.Contains(cmdStr, "> /etc") {
				isSafe = false
			}
		} else if name == "" {
			isSafe = false
		}

		if isSafe {
			score += baseSafety
		}

		// Tool adherence/preference pattern
		if name == "elastro_query_ast" || name == "view_file" || name == "replace_file_content" || name == "multiple_replace_file_content" || name == "grep_search" {
			score += baseElastro
		} else if name == "run_shell_cmd" || name == "run_command" {
			// Acceptable but not preferred, partial credit
			score += baseElastro / 2
		}
	}

	if score > 100 {
		score = 100
	}
	return score
}

func cloneChatRequest(req *ChatRequest) *ChatRequest {
	c := &ChatRequest{
		Model:        req.Model,
		Provider:     req.Provider,
		Temperature:  req.Temperature,
		MaxTokens:    req.MaxTokens,
		Think:        req.Think,
		CredentialID: req.CredentialID,
		AgentRole:    req.AgentRole,
		Stream:       req.Stream,
	}
	c.Messages = append([]Message{}, req.Messages...)
	c.Tools = append([]Tool{}, req.Tools...)
	return c
}
