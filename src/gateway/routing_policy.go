package gateway

import (
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
)

// ─────────────────────────────────────────────────────────────────────────────
// Routing Policy Engine — Kubernetes-grade 3-mode routing with spend controls.
//
// Manages frontier/hybrid/local routing decisions, per-model spend budgets with
// circuit-breaker enforcement, weighted random frontier selection, and per-role
// model pinning. All state persists to ES at `flume-routing-policy/_doc/singleton`.
//
// Modes:
//   - frontier_only: All requests route to cloud LLMs via weighted selection.
//   - hybrid:        Probability + complexity gate routes to frontier or local.
//   - local_only:    Existing MultiNodeRouter behavior (default).
//
// Spend enforcement:
//   - Each frontier model has a configurable USD budget.
//   - At 90% utilization: slog.Warn emitted.
//   - At 100%: model is circuit-broken and excluded from selection.
//   - Spend state is debounced-persisted to ES every 30s or on circuit-break.
// ─────────────────────────────────────────────────────────────────────────────

// RoutingMode defines the three operating modes of the routing policy.
type RoutingMode string

const (
	RoutingModeFrontierOnly RoutingMode = "frontier_only"
	RoutingModeHybrid       RoutingMode = "hybrid"
	RoutingModeLocalOnly    RoutingMode = "local_only"
)

// FrontierModelWeight represents a single frontier model in the weighted mix,
// including its spend budget and circuit-breaker state.
type FrontierModelWeight struct {
	Provider     string  `json:"provider"`       // "openai", "anthropic", "gemini", "xai"
	Model        string  `json:"model"`          // "gpt-5", "claude-opus-4.7", etc.
	CredentialID string  `json:"credential_id"`  // links to flume-llm-credentials
	Weight       float64 `json:"weight"`         // 0.0–1.0, normalized at runtime

	// Spend Controls
	BudgetUSD   float64 `json:"budget_usd"`   // User-configurable spend cap ($)
	SpentUSD    float64 `json:"spent_usd"`     // Cumulative spend tracked by gateway
	CircuitOpen bool    `json:"circuit_open"`  // True when budget exhausted
}

// RoutingPolicy defines the complete routing configuration persisted in ES.
type RoutingPolicy struct {
	mu sync.RWMutex `json:"-"`

	Mode                RoutingMode           `json:"mode"`
	FrontierMix         []FrontierModelWeight `json:"frontier_mix"`
	FrontierLocalRatio  float64               `json:"frontier_local_ratio"`  // 0.0=all local, 1.0=all frontier
	ComplexityThreshold int                   `json:"complexity_threshold"`  // 1-10 scale

	// Per-role frontier pinning: role → model name
	RolePinning map[string]string `json:"role_pinning,omitempty"`

	// Internal: tracks when spend was last persisted to ES
	lastSpendPersist time.Time `json:"-"`
}

// CostPerMillionTokens maps frontier model names to their per-million-token
// cost in USD. Updated April 2026 from official provider pricing pages.
var CostPerMillionTokens = map[string]struct{ Input, Output float64 }{
	// ── OpenAI (April 2026) ──────────────────────────────────────────
	"gpt-5":        {1.25, 10.00},  // Current flagship
	"gpt-4.1":      {2.00, 8.00},   // Production / 1M context
	"gpt-4.1-mini": {0.10, 0.40},   // Budget tier
	"gpt-4.1-nano": {0.10, 0.40},   // Ultra-budget tier
	"o3":           {2.00, 8.00},   // Reasoning model
	"o4-mini":      {1.10, 4.40},   // Lightweight reasoning
	"gpt-4o":       {2.50, 10.00},  // Previous-gen flagship
	"gpt-4o-mini":  {0.15, 0.60},   // Previous-gen budget

	// ── Anthropic (April 2026) ───────────────────────────────────────
	"claude-opus-4.7":  {5.00, 25.00},  // Latest flagship (Apr 16 2026)
	"claude-sonnet-4.6":{3.00, 15.00},  // Balanced performance
	"claude-haiku-4.5": {1.00, 5.00},   // Fast / cost-effective

	// ── Google Gemini (April 2026) ───────────────────────────────────
	"gemini-2.5-pro":       {1.25, 10.00},  // ≤200k context
	"gemini-2.5-flash":     {0.30, 2.50},   // Fast multimodal
	"gemini-2.5-flash-lite":{0.10, 0.40},   // Ultra-budget

	// ── xAI Grok (April 2026) ────────────────────────────────────────
	"grok-4.20":    {2.00, 6.00},   // Latest flagship (2M context)
	"grok-4":       {3.00, 15.00},  // Premium w/ web search
	"grok-4.1-fast":{0.20, 0.50},   // High-volume budget
}

// DefaultRoutingPolicy returns the safe default: local_only mode with no
// frontier models configured. This exactly preserves pre-existing behavior
// when the flume-routing-policy ES document does not exist.
func DefaultRoutingPolicy() *RoutingPolicy {
	return &RoutingPolicy{
		Mode:                RoutingModeLocalOnly,
		FrontierMix:         []FrontierModelWeight{},
		FrontierLocalRatio:  0.3,
		ComplexityThreshold: 7,
		RolePinning:         make(map[string]string),
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// ES Loader
// ─────────────────────────────────────────────────────────────────────────────

// LoadRoutingPolicyFromES reads the routing policy from Elasticsearch.
// Returns DefaultRoutingPolicy() on missing document or parse error.
func LoadRoutingPolicyFromES(ctx context.Context, esURL string, httpClient *http.Client) *RoutingPolicy {
	log := WithContext(ctx)

	url := strings.TrimRight(esURL, "/") + "/flume-routing-policy/_doc/singleton"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		log.Debug("routing_policy: failed to build ES request",
			slog.String("error", err.Error()),
		)
		return DefaultRoutingPolicy()
	}

	apiKey := os.Getenv("ES_API_KEY")
	if apiKey != "" {
		req.Header.Set("Authorization", "ApiKey "+apiKey)
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		log.Debug("routing_policy: ES unreachable, using default local_only",
			slog.String("error", err.Error()),
		)
		return DefaultRoutingPolicy()
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		log.Debug("routing_policy: no policy document in ES, using default local_only")
		return DefaultRoutingPolicy()
	}
	if resp.StatusCode >= 400 {
		log.Warn("routing_policy: unexpected ES status, using default local_only",
			slog.Int("status_code", resp.StatusCode),
		)
		return DefaultRoutingPolicy()
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		log.Warn("routing_policy: failed to read ES response",
			slog.String("error", err.Error()),
		)
		return DefaultRoutingPolicy()
	}

	// ES wraps the document in {"_source": {...}}
	var esDoc struct {
		Source json.RawMessage `json:"_source"`
	}
	if err := json.Unmarshal(body, &esDoc); err != nil {
		log.Warn("routing_policy: failed to parse ES envelope",
			slog.String("error", err.Error()),
		)
		return DefaultRoutingPolicy()
	}

	policy := DefaultRoutingPolicy()
	if err := json.Unmarshal(esDoc.Source, policy); err != nil {
		log.Warn("routing_policy: failed to parse policy document, using defaults",
			slog.String("error", err.Error()),
		)
		return DefaultRoutingPolicy()
	}

	// Validate + normalize
	policy.normalizeWeights()

	if policy.RolePinning == nil {
		policy.RolePinning = make(map[string]string)
	}

	log.Info("routing_policy: loaded from ES",
		slog.String("mode", string(policy.Mode)),
		slog.Int("frontier_models", len(policy.FrontierMix)),
		slog.Float64("frontier_local_ratio", policy.FrontierLocalRatio),
		slog.Int("complexity_threshold", policy.ComplexityThreshold),
		slog.Int("role_pins", len(policy.RolePinning)),
	)

	return policy
}

// PersistToES writes the current routing policy to Elasticsearch.
func (p *RoutingPolicy) PersistToES(ctx context.Context, esURL string, httpClient *http.Client) error {
	log := WithContext(ctx)

	p.mu.RLock()
	data, err := json.Marshal(p)
	p.mu.RUnlock()
	if err != nil {
		return fmt.Errorf("marshal routing policy: %w", err)
	}

	url := strings.TrimRight(esURL, "/") + "/flume-routing-policy/_doc/singleton"
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, strings.NewReader(string(data)))
	if err != nil {
		return fmt.Errorf("build ES request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	apiKey := os.Getenv("ES_API_KEY")
	if apiKey != "" {
		req.Header.Set("Authorization", "ApiKey "+apiKey)
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("ES persist failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("ES returned %d: %s", resp.StatusCode, string(body))
	}

	log.Info("routing_policy: persisted to ES",
		slog.String("mode", string(p.Mode)),
		slog.Int("frontier_models", len(p.FrontierMix)),
	)
	return nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Weighted Frontier Selection
// ─────────────────────────────────────────────────────────────────────────────

// SelectWeightedFrontier picks a frontier model using cumulative-weight random
// selection. Respects per-role pinning and circuit-broken exclusions.
// Returns nil when no models are available (all circuit-broken or empty mix).
func (p *RoutingPolicy) SelectWeightedFrontier(agentRole string) *FrontierModelWeight {
	p.mu.RLock()
	defer p.mu.RUnlock()

	// Check per-role pin first
	if agentRole != "" {
		if pinned, ok := p.RolePinning[strings.ToLower(agentRole)]; ok && pinned != "" {
			for i := range p.FrontierMix {
				if p.FrontierMix[i].Model == pinned && !p.FrontierMix[i].CircuitOpen {
					Log().Info("routing_policy: role-pinned model selected",
						slog.String("agent_role", agentRole),
						slog.String("model", pinned),
						slog.String("provider", p.FrontierMix[i].Provider),
					)
					return &p.FrontierMix[i]
				}
			}
			// Pinned model is circuit-broken — fall through to weighted selection
			Log().Warn("routing_policy: pinned model circuit-broken, falling back to weighted selection",
				slog.String("agent_role", agentRole),
				slog.String("pinned_model", pinned),
			)
		}
	}

	// Collect non-circuit-broken models and their weights
	type candidate struct {
		idx    int
		weight float64
	}
	var candidates []candidate
	var totalWeight float64

	for i, m := range p.FrontierMix {
		if !m.CircuitOpen && m.Weight > 0 {
			candidates = append(candidates, candidate{idx: i, weight: m.Weight})
			totalWeight += m.Weight
		}
	}

	if len(candidates) == 0 {
		return nil
	}

	// Single candidate — skip random selection
	if len(candidates) == 1 {
		selected := &p.FrontierMix[candidates[0].idx]
		Log().Info("routing_policy: frontier model selected (single available)",
			slog.String("model", selected.Model),
			slog.String("provider", selected.Provider),
		)
		return selected
	}

	// Cumulative weight random selection
	r := rand.Float64() * totalWeight
	var cumulative float64
	for _, c := range candidates {
		cumulative += c.weight
		if r <= cumulative {
			selected := &p.FrontierMix[c.idx]
			Log().Info("routing_policy: frontier model selected",
				slog.String("model", selected.Model),
				slog.String("provider", selected.Provider),
				slog.Float64("weight", selected.Weight),
				slog.String("agent_role", agentRole),
			)
			return selected
		}
	}

	// Fallback to last candidate (floating point edge case)
	last := &p.FrontierMix[candidates[len(candidates)-1].idx]
	return last
}

// ─────────────────────────────────────────────────────────────────────────────
// Spend Enforcement
// ─────────────────────────────────────────────────────────────────────────────

// EnforceSpendBudget computes the cost of a frontier response and updates the
// model's cumulative spend. Circuit-breaks the model at 100% budget.
func (p *RoutingPolicy) EnforceSpendBudget(model string, usage Usage) {
	cost, ok := CostPerMillionTokens[model]
	if !ok {
		// Unknown model — use a conservative fallback rate
		cost = struct{ Input, Output float64 }{5.00, 15.00}
	}

	inputCost := float64(usage.PromptTokens) / 1_000_000.0 * cost.Input
	outputCost := float64(usage.CompletionTokens) / 1_000_000.0 * cost.Output
	totalCost := inputCost + outputCost

	if totalCost <= 0 {
		return
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	for i := range p.FrontierMix {
		m := &p.FrontierMix[i]
		if m.Model != model {
			continue
		}

		m.SpentUSD += totalCost

		if m.BudgetUSD <= 0 {
			// No budget set — track spend but don't enforce
			return
		}

		utilization := m.SpentUSD / m.BudgetUSD

		if utilization >= 1.0 && !m.CircuitOpen {
			m.CircuitOpen = true
			Log().Error("spend_control: model circuit-broken — budget exhausted",
				slog.String("model", model),
				slog.String("provider", m.Provider),
				slog.Float64("spent_usd", m.SpentUSD),
				slog.Float64("budget_usd", m.BudgetUSD),
			)
			Metrics.RecordFrontierCircuitBreak(m.Provider, model)
		} else if utilization >= 0.90 {
			Log().Warn("spend_control: model approaching budget",
				slog.String("model", model),
				slog.String("provider", m.Provider),
				slog.Float64("spent_usd", m.SpentUSD),
				slog.Float64("budget_usd", m.BudgetUSD),
				slog.Float64("utilization_pct", utilization*100),
			)
		}

		Metrics.RecordFrontierSpend(m.Provider, model, totalCost)
		return
	}
}

// PersistSpendIfDue writes spend state to ES if 30s have elapsed since
// the last persistence. This is called after each frontier response
// to debounce ES writes.
func (p *RoutingPolicy) PersistSpendIfDue(ctx context.Context, esURL string, httpClient *http.Client) {
	p.mu.RLock()
	due := time.Since(p.lastSpendPersist) >= 30*time.Second
	p.mu.RUnlock()

	if !due {
		return
	}

	p.mu.Lock()
	p.lastSpendPersist = time.Now()
	p.mu.Unlock()

	if err := p.PersistToES(ctx, esURL, httpClient); err != nil {
		WithContext(ctx).Warn("routing_policy: debounced spend persist failed",
			slog.String("error", err.Error()),
		)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Hybrid Routing Decision
// ─────────────────────────────────────────────────────────────────────────────

// taskTypeComplexity maps the MultiNodeRouter's task-type taxonomy to a 1-10
// complexity score for hybrid routing decisions.
var taskTypeComplexity = map[string]int{
	"reasoning":  8,
	"planning":   8,
	"code":       6,
	"generic":    4,
	"evaluation": 3,
}

// ShouldUseFrontier determines whether a request should be routed to a frontier
// model in hybrid mode. Uses complexity gating + probability.
func (p *RoutingPolicy) ShouldUseFrontier(taskType string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()

	complexity, ok := taskTypeComplexity[taskType]
	if !ok {
		complexity = 4 // default for unknown task types
	}

	// Complexity gate: high-complexity tasks always use frontier
	if complexity >= p.ComplexityThreshold {
		Log().Info("hybrid_routing_decision: complexity gate → frontier",
			slog.String("task_type", taskType),
			slog.Int("complexity", complexity),
			slog.Int("threshold", p.ComplexityThreshold),
		)
		return true
	}

	// Probability gate: random selection weighted by FrontierLocalRatio
	useFrontier := rand.Float64() < p.FrontierLocalRatio
	Log().Info("hybrid_routing_decision: probability gate",
		slog.String("task_type", taskType),
		slog.Int("complexity", complexity),
		slog.Float64("frontier_ratio", p.FrontierLocalRatio),
		slog.Bool("use_frontier", useFrontier),
	)
	return useFrontier
}

// HasAvailableFrontierModels returns true if at least one model is not
// circuit-broken in the frontier mix.
func (p *RoutingPolicy) HasAvailableFrontierModels() bool {
	p.mu.RLock()
	defer p.mu.RUnlock()

	for _, m := range p.FrontierMix {
		if !m.CircuitOpen && m.Weight > 0 {
			return true
		}
	}
	return false
}

// ─────────────────────────────────────────────────────────────────────────────
// Weight Normalization
// ─────────────────────────────────────────────────────────────────────────────

// normalizeWeights ensures all frontier model weights sum to 1.0.
// Handles edge cases: all zeros → equal distribution, negative weights → 0.
func (p *RoutingPolicy) normalizeWeights() {
	var totalWeight float64
	for i := range p.FrontierMix {
		if p.FrontierMix[i].Weight < 0 {
			p.FrontierMix[i].Weight = 0
		}
		totalWeight += p.FrontierMix[i].Weight
	}

	if totalWeight == 0 && len(p.FrontierMix) > 0 {
		// All zero weights → equal distribution
		equal := 1.0 / float64(len(p.FrontierMix))
		for i := range p.FrontierMix {
			p.FrontierMix[i].Weight = equal
		}
		return
	}

	if totalWeight > 0 {
		for i := range p.FrontierMix {
			p.FrontierMix[i].Weight /= totalWeight
		}
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Validation
// ─────────────────────────────────────────────────────────────────────────────

// Validate checks the routing policy for structural correctness.
func (p *RoutingPolicy) Validate() error {
	switch p.Mode {
	case RoutingModeFrontierOnly, RoutingModeHybrid, RoutingModeLocalOnly:
		// valid
	default:
		return fmt.Errorf("invalid routing mode: %q (must be frontier_only, hybrid, or local_only)", p.Mode)
	}

	if p.FrontierLocalRatio < 0 || p.FrontierLocalRatio > 1 {
		return fmt.Errorf("frontier_local_ratio must be 0.0–1.0, got %f", p.FrontierLocalRatio)
	}

	if p.ComplexityThreshold < 1 || p.ComplexityThreshold > 10 {
		return fmt.Errorf("complexity_threshold must be 1–10, got %d", p.ComplexityThreshold)
	}

	for i, m := range p.FrontierMix {
		if m.Provider == "" {
			return fmt.Errorf("frontier_mix[%d]: provider is required", i)
		}
		if m.Model == "" {
			return fmt.Errorf("frontier_mix[%d]: model is required", i)
		}
		if m.BudgetUSD < 0 {
			return fmt.Errorf("frontier_mix[%d]: budget_usd must be ≥ 0", i)
		}
	}

	return nil
}
