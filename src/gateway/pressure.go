package gateway

import (
	"context"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
)

// ─────────────────────────────────────────────────────────────────────────────
// FrontierQueue — per-provider backpressure for cloud LLM escalation calls.
//
// The ensemble's frontier escalation path can fire for every parallel jury
// member whose score is < 70. Without gating, a burst of low-confidence local
// responses simultaneously floods OpenAI/Anthropic with N requests, hitting
// rate limits and multiplying cost.
//
// FrontierQueue is a lightweight channel semaphore that:
//   - Limits concurrent outbound frontier calls (default 4, FLUME_FRONTIER_MAX_CONCURRENT)
//   - Queues callers that exceed the cap (blocks until slot is available or ctx cancels)
//   - Exposes live counter for the /health endpoint
//
// Unlike OllamaSemaphore which guards against VRAM thrashing on local hardware,
// FrontierQueue guards against cloud rate-limit cascades and unexpected cost spikes.
// ─────────────────────────────────────────────────────────────────────────────

const defaultFrontierMaxConcurrent = 4

// FrontierQueue controls concurrent outbound frontier LLM escalation calls.
type FrontierQueue struct {
	sem      chan struct{}
	maxSlots int
	// active tracks how many slots are currently in use (for health metrics).
	active atomic.Int32
}

// NewFrontierQueue creates a FrontierQueue with the given capacity.
// Pass 0 to use the default (4).
func NewFrontierQueue(maxConcurrent int) *FrontierQueue {
	if maxConcurrent <= 0 {
		maxConcurrent = defaultFrontierMaxConcurrent
	}
	return &FrontierQueue{
		sem:      make(chan struct{}, maxConcurrent),
		maxSlots: maxConcurrent,
	}
}

// FrontierMaxConcurrentFromEnv reads FLUME_FRONTIER_MAX_CONCURRENT from the
// environment and returns the parsed value, or 0 to use the default (4).
// Logs a warning when the variable is set but cannot be parsed so that
// operators are immediately alerted to configuration typos (e.g. "4threads").
func FrontierMaxConcurrentFromEnv() int {
	v := strings.TrimSpace(os.Getenv("FLUME_FRONTIER_MAX_CONCURRENT"))
	if v == "" {
		return 0
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		Log().Warn("frontier_queue: FLUME_FRONTIER_MAX_CONCURRENT is invalid — using default",
			slog.String("provided_value", v),
			slog.Int("default", defaultFrontierMaxConcurrent),
			slog.String("hint", "value must be a positive integer, e.g. 4"),
		)
		return 0
	}
	return n
}

// Acquire blocks until a frontier slot is available or the context is cancelled.
// Returns true if acquired, false if the context deadline exceeded or was cancelled.
func (q *FrontierQueue) Acquire(ctx context.Context) bool {
	select {
	case q.sem <- struct{}{}:
		q.active.Add(1)
		return true
	case <-ctx.Done():
		return false
	}
}

// Release returns a frontier slot to the pool.
func (q *FrontierQueue) Release() {
	q.active.Add(-1)
	<-q.sem
}

// Active returns the number of frontier calls currently in flight.
func (q *FrontierQueue) Active() int {
	return int(q.active.Load())
}

// MaxSlots returns the configured frontier concurrency cap.
func (q *FrontierQueue) MaxSlots() int {
	return q.maxSlots
}

// HealthMetrics returns a snapshot of queue state suitable for the /health response.
func (q *FrontierQueue) HealthMetrics() map[string]int {
	return map[string]int{
		"frontier_active":    q.Active(),
		"frontier_max_slots": q.maxSlots,
	}
}

// logFrontierAcquire emits a structured log before acquiring and returns a
// cleanup function that logs completion (for defer chaining).
func logFrontierAcquire(ctx context.Context, model string, q *FrontierQueue) func() {
	log := WithContext(ctx)
	log.Info("frontier queue: awaiting slot",
		slog.String("model", model),
		slog.Int("active", q.Active()),
		slog.Int("max", q.MaxSlots()),
	)
	return func() {
		log.Info("frontier queue: slot released",
			slog.String("model", model),
			slog.Int("active", q.Active()),
		)
	}
}
