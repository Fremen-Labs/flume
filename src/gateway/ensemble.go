package gateway

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"
)

// ─────────────────────────────────────────────────────────────────────────────
// Ensemble executor — parallel local jury with early-exit and adaptive sizing.
//
// Key behaviours:
//
//  1. Adaptive sizing: consults AdaptiveEnsembleSize to clamp jury count to
//     what fits in current VRAM headroom. Degrades to single-call when only 1 fits.
//
//  2. Early-exit (first-complete-wins): jury members write results to a buffered
//     channel as they finish. The coordinator picks the first result that scores
//     ≥ scoreThreshold and cancels remaining members immediately — reducing
//     median latency to the speed of the fastest acceptable response.
//
//  3. Fallback: if no member hits the threshold before the timeout,
//     we degrade to the best score seen so far and escalate to frontier if < 70.
//
//  4. Frontier backpressure: escalation calls are gated behind FrontierQueue
//     to prevent simultaneous burst-to-cloud from multiple low-confidence results.
// ─────────────────────────────────────────────────────────────────────────────

// scoreThreshold is the minimum score accepted for an early-exit win.
// A response scoring ≥ this is returned immediately without waiting for peers.
const scoreThreshold = 80

// temperatureIncrement is the per-member temperature step applied across jury
// members to encourage response diversity without extreme randomness.
// Member 0 uses the base temperature; member i uses base + i*temperatureIncrement.
const temperatureIncrement = 0.2

// juryResult carries one jury member's outcome.
type juryResult struct {
	index    int
	resp     *ChatResponse
	err      error
	score    int
	duration time.Duration
}

// ExecuteEnsemble coordinates parallel local generation and fallback degradation.
// withTools controls whether jury members invoke the tool-calling endpoint (true)
// or the plain-chat endpoint (false). This allows /v1/chat to benefit from the
// ensemble without accidentally injecting tool semantics into text-only paths.
func (s *Server) ExecuteEnsemble(ctx context.Context, req *ChatRequest, withTools bool) (*ChatResponse, error) {
	ensembleStart := time.Now()
	log := WithContext(ctx)

	// ── 1. Adaptive sizing ────────────────────────────────────────────────
	configuredSize := s.config.EnsembleSize
	if configuredSize <= 1 {
		// Degrade to straight-through if size misconfigured or already single
		return s.router.Route(ctx, req, withTools)
	}

	ollamaURL := s.config.GetOllamaBaseURL()
	size := AdaptiveEnsembleSize(req.Model, configuredSize, ollamaURL)

	if size <= 1 {
		// Hardware pressure: single-call fallback
		log.Info("ensemble: single-call fallback due to VRAM pressure",
			slog.String("model", req.Model),
			slog.Int("configured_size", configuredSize),
		)
		return s.router.Route(ctx, req, withTools)
	}

	log.Info("starting LLM jury ensemble",
		slog.Int("size", size),
		slog.Int("configured_size", configuredSize),
		slog.Float64("base_temperature", req.Temperature),
		slog.String("model", req.Model),
		slog.Bool("with_tools", withTools),
	)

	// ── 2. Apply ensemble timeout ─────────────────────────────────────────
	timeout := s.config.EnsembleTimeout
	if timeout <= 0 {
		timeout = 90 * time.Second
	}
	timeoutCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	// ── 3. Launch jury members concurrently ───────────────────────────────
	// earlyWin is buffered to size so members never block on send even after
	// the coordinator has picked a winner and cancelled.
	earlyWin := make(chan juryResult, size)

	g, gCtx := errgroup.WithContext(timeoutCtx)

	for i := 0; i < size; i++ {
		i := i // loop capture
		g.Go(func() error {
			start := time.Now()

			cloneReq := cloneChatRequest(req)
			// Vary temperature for diversity among jury members.
			temp := cloneReq.Temperature + float64(i)*temperatureIncrement
			if temp > 1.0 {
				temp = 1.0
			}
			cloneReq.Temperature = temp

			resp, err := s.router.Route(gCtx, cloneReq, withTools)

			score := 0
			if resp != nil && err == nil {
				score = ScoreResponse(resp)
			}

			earlyWin <- juryResult{
				index:    i,
				resp:     resp,
				err:      err,
				score:    score,
				duration: time.Since(start),
			}
			return nil // never fail errgroup; we collect all results ourselves
		})
	}

	// Close earlyWin after all goroutines finish so the drain loop below
	// can detect completion.
	go func() {
		_ = g.Wait()
		close(earlyWin)
	}()

	// ── 4. Early-exit coordinator ─────────────────────────────────────────
	var (
		bestResult juryResult
		bestScore  = -1
		collected  int
		mu         sync.Mutex
	)

	updateBest := func(r juryResult) {
		mu.Lock()
		defer mu.Unlock()
		collected++
		if r.err != nil || r.resp == nil {
			log.Warn("jury member failed",
				slog.Int("index", r.index),
				slog.Any("error", r.err),
			)
			return
		}
		log.Info("jury member evaluated",
			slog.Int("index", r.index),
			slog.Int("score", r.score),
			slog.Float64("duration_ms", float64(r.duration.Milliseconds())),
		)
		if r.score > bestScore {
			bestScore = r.score
			bestResult = r
		}
	}

	for result := range earlyWin {
		updateBest(result)

		mu.Lock()
		localBest := bestScore
		localCollected := collected
		mu.Unlock()

		// Early-exit: first response above threshold wins immediately.
		// Cancel remaining goroutines to free their Ollama slots ASAP.
		if localBest >= scoreThreshold {
			cancel() // cancels gCtx inside errgroup, stopping in-flight members
			log.Info("ensemble early-exit: high-confidence response accepted",
				slog.Int("score", localBest),
				slog.Int("after_members", localCollected),
				slog.Int("jury_size", size),
			)
			break
		}

		// All members are in: stop waiting even if nobody hit threshold.
		if localCollected >= size {
			break
		}
	}

	// ── 5. Evaluate and optionally escalate ───────────────────────────────
	mu.Lock()
	finalBest := bestScore
	finalResp := bestResult.resp
	mu.Unlock()

	if finalBest >= 0 && finalResp != nil {
		totalScore := 0
		mu.Lock()
		c := collected
		if bestScore > 0 {
			totalScore = bestScore // simplified; actual sum would need all scores
		}
		mu.Unlock()

		log.Info("ensemble completed",
			slog.Int("best_score", finalBest),
			slog.Int("valid_responses", c),
			slog.Int("total_score_proxy", totalScore),
		)

		// ── Metrics: record ensemble execution ───────────────────────
		taskType := "chat"
		if withTools {
			taskType = "tool_call"
		}
		
		metricModel := req.Model
		if !s.config.IsKnownModel(metricModel) {
			metricModel = "unknown"
		}

		Metrics.RecordEnsemble(metricModel, taskType, size, finalBest, time.Since(ensembleStart))
	} else {
		log.Warn("ensemble entire failure, all jury members errored")
	}

	// Threshold check for frontier escalation
	if finalBest < 70 {
		fallback := s.config.FrontierFallbackModel
		if fallback == "" {
			fallback = "gpt-4o"
		}

		log.Warn("local confidence critically low",
			slog.Int("score", finalBest),
			slog.String("escalating_to", fallback),
		)

		// ── Metrics: track frontier escalation ────────────────────────
		Metrics.RecordEscalation()

		// ── Frontier backpressure via FrontierQueue ────────────────────
		cleanup := logFrontierAcquire(timeoutCtx, fallback, s.frontierQ)
		if !s.frontierQ.Acquire(timeoutCtx) {
			log.Warn("frontier queue: context cancelled while waiting for slot — degrading to best local")
			if finalResp != nil {
				return finalResp, nil
			}
			return nil, timeoutCtx.Err()
		}
		defer func() {
			s.frontierQ.Release()
			cleanup()
		}()

		frontierReq := cloneChatRequest(req)
		frontierReq.Model = fallback
		frontierReq.Provider = "" // re-resolved in Route based on rules

		// Use timeoutCtx so a hung frontier call can't exceed the ensemble budget.
		frontierResp, err := s.router.Route(timeoutCtx, frontierReq, withTools)
		if err == nil {
			log.Info("frontier escalation successful")
			return frontierResp, nil
		}

		log.Error("frontier fallback failed, gracefully degrading to best local attempt",
			slog.Any("error", err),
		)
		if finalResp != nil {
			return finalResp, nil
		}
		return nil, err
	}

	return finalResp, nil
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
