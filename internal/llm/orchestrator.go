// Package llm provides LLMCommsOrchestrator for Phase 2 (backpressure, health/circuit, streaming, per-plan controls).
//
// Extracted/enhanced from prior client + runner patterns per flume-workitem-orchestrator-design.md Phase 2.
// Ties to WorkItemOrchestrator: backpressure WIP per (plan_session + role + hierarchy_level), health/circuit with explosion_evidence,
// mandatory streaming with per-chunk Log*, per-plan budget/rate, connectivity recon.
//
// Follows reliable-go: ctx first, explicit errs, bounded, obs (Log*), reconciliation for health, idempotent acquires.
package llm

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
)

// LLMCommsOrchestrator manages comms reliability for LLM calls (Phase 2).
// Backpressure at claim/pre-LLM, circuit for persistent errors (ErrPersistentConfig, timeouts),
// full streaming support, per-plan budget/rate (via PlanSessionID), health recon.
//
// Can be used by Runner (pre LLM), Claimer (WIP acquire), Manager (recon).
type LLMCommsOrchestrator struct {
	mu            sync.Mutex
	wip           map[string]int // key = planSession:role:level
	circuitOpen   map[string]time.Time // key for circuit (provider or plan)
	lastRecon     time.Time
	logger        *slog.Logger
	maxWIPPerKey  int
	circuitTimeout time.Duration
}

// NewLLMCommsOrchestrator creates the orchestrator (default caps for safety).
func NewLLMCommsOrchestrator() *LLMCommsOrchestrator {
	return &LLMCommsOrchestrator{
		wip:            make(map[string]int),
		circuitOpen:    make(map[string]time.Time),
		logger:         slog.Default(),
		maxWIPPerKey:   4, // bounded per (plan/role/level) to prevent thundering herd
		circuitTimeout: 5 * time.Minute,
	}
}

// WIPKey builds key for backpressure (plan_session + role + hierarchy_level).
func WIPKey(planSession, role string, level int) string {
	if planSession == "" {
		planSession = "global"
	}
	return fmt.Sprintf("%s:%s:%d", planSession, role, level)
}

// AcquireWIP attempts backpressure acquire. Returns ok, or blocks? Non-blocking try for now (use in claim pre).
// On fail, caller should set blocked + evidence.
func (o *LLMCommsOrchestrator) AcquireWIP(ctx context.Context, planSession, role string, level int) (bool, string) {
	key := WIPKey(planSession, role, level)
	o.mu.Lock()
	defer o.mu.Unlock()

	cur := o.wip[key]
	if cur >= o.maxWIPPerKey {
		reason := fmt.Sprintf("WIP backpressure: key=%s cur=%d >= max=%d (plan_session=%s role=%s level=%d)", key, cur, o.maxWIPPerKey, planSession, role, level)
		flumelogger.LogAgentReasoning(ctx, "", "llm-comms", reason, map[string]any{"key": key, "plan_session_id": planSession, "role": role, "level": level})
		return false, reason
	}
	o.wip[key] = cur + 1
	return true, ""
}

// ReleaseWIP releases the WIP slot.
func (o *LLMCommsOrchestrator) ReleaseWIP(planSession, role string, level int) {
	key := WIPKey(planSession, role, level)
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.wip[key] > 0 {
		o.wip[key]--
	}
	if o.wip[key] == 0 {
		delete(o.wip, key)
	}
}

// IsCircuitOpen checks health/circuit for persistent errors (ErrPersistentConfig, repeated timeout).
// On open, fast-fail to blocked + explosion_evidence.
func (o *LLMCommsOrchestrator) IsCircuitOpen(key string) bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	if t, ok := o.circuitOpen[key]; ok {
		if time.Since(t) < o.circuitTimeout {
			return true
		}
		delete(o.circuitOpen, key)
	}
	return false
}

// OpenCircuit marks circuit open for key (on persistent err).
func (o *LLMCommsOrchestrator) OpenCircuit(ctx context.Context, key, reason string, taskID string) {
	o.mu.Lock()
	o.circuitOpen[key] = time.Now()
	o.mu.Unlock()
	reasonFull := fmt.Sprintf("circuit open: %s key=%s", reason, key)
	flumelogger.LogAgentReasoning(ctx, taskID, "llm-comms", reasonFull, map[string]any{"key": key, "reason": reason})
}

// ReconcileHealth does connectivity recon (called from manager cycle before claim).
// Clears old circuits, logs.
func (o *LLMCommsOrchestrator) ReconcileHealth(ctx context.Context) {
	o.mu.Lock()
	now := time.Now()
	for k, t := range o.circuitOpen {
		if now.Sub(t) > o.circuitTimeout {
			delete(o.circuitOpen, k)
			flumelogger.LogAgentReasoning(ctx, "", "llm-comms", "circuit closed after timeout", map[string]any{"key": k})
		}
	}
	o.lastRecon = now
	o.mu.Unlock()
}

// ShouldUseGateway etc. can delegate to client, but orchestrator owns the policy.
func (o *LLMCommsOrchestrator) ShouldFastFail(ctx context.Context, err error, provider, model, planSession, taskID string) bool {
	if errors.Is(err, ErrPersistentConfig) || (err != nil && isTimeoutErr(err)) {
		key := provider + ":" + model
		if planSession != "" {
			key += ":" + planSession
		}
		if o.IsCircuitOpen(key) {
			return true
		}
		o.OpenCircuit(ctx, key, "persistent or timeout", taskID)
		return true
	}
	return false
}

func isTimeoutErr(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	return s == "context deadline exceeded" || s == "timeout" || errors.Is(err, context.DeadlineExceeded)
}

// Note: import "errors" if needed, but for sketch.
