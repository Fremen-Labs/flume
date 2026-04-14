package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Health Checker — Background goroutine that probes Ollama nodes periodically.
//
// Probe sequence per node:
//   1. GET /api/tags  → validates Ollama is alive + discovers loaded models
//   2. Lightweight GET /api/ps → measures latency + current load
//
// Circuit breaker integration:
//   - 3 consecutive probe failures → marks node "offline", stops routing
//   - After 60s, enters "half-open": allows 1 probe
//   - 2 consecutive successes → returns to "healthy"
//
// All logging uses the gateway's slog-based structured logger.
// ─────────────────────────────────────────────────────────────────────────────

const (
	defaultHealthInterval     = 15 * time.Second
	offlineProbeInterval      = 60 * time.Second
	circuitOpenThreshold      = 3
	circuitHalfOpenSuccesses  = 2
)

// HealthChecker runs background health probes against all registered nodes.
type HealthChecker struct {
	registry   *NodeRegistry
	interval   time.Duration
	httpClient *http.Client
	breakers   sync.Map // map[string]*nodeBreaker
	stopCh     chan struct{}
}

// nodeBreaker tracks per-node circuit breaker state.
type nodeBreaker struct {
	consecutiveFailures atomic.Int32
	consecutiveSuccess  atomic.Int32
	state               atomic.Value // "closed", "open", "half-open"
	lastFailure         atomic.Value // time.Time
}

func newNodeBreaker() *nodeBreaker {
	b := &nodeBreaker{}
	b.state.Store("closed")
	b.lastFailure.Store(time.Time{})
	return b
}

// NewHealthChecker creates a checker wired to the node registry.
func NewHealthChecker(registry *NodeRegistry) *HealthChecker {
	return &HealthChecker{
		registry:   registry,
		interval:   defaultHealthInterval,
		httpClient: &http.Client{Timeout: 10 * time.Second},
		stopCh:     make(chan struct{}),
	}
}

// Start launches the background health check goroutine.
func (hc *HealthChecker) Start(ctx context.Context) {
	log := WithContext(ctx)
	log.Info("health_checker: started",
		slog.Duration("interval", hc.interval),
	)

	go func() {
		ticker := time.NewTicker(hc.interval)
		defer ticker.Stop()

		for {
			select {
			case <-ticker.C:
				hc.probeAll(ctx)
			case <-hc.stopCh:
				log.Info("health_checker: stopped")
				return
			case <-ctx.Done():
				log.Info("health_checker: context cancelled, stopping")
				return
			}
		}
	}()
}

// Stop signals the health checker to shut down.
func (hc *HealthChecker) Stop() {
	close(hc.stopCh)
}

// probeAll iterates all registered nodes and probes each one.
func (hc *HealthChecker) probeAll(ctx context.Context) {
	log := WithContext(ctx)
	nodes := hc.registry.AllNodes()
	if len(nodes) == 0 {
		return
	}

	log.Debug("health_checker: probing nodes",
		slog.Int("count", len(nodes)),
	)

	var wg sync.WaitGroup
	for _, n := range nodes {
		n := n
		wg.Add(1)
		go func() {
			defer wg.Done()
			hc.probeNode(ctx, &n)
		}()
	}
	wg.Wait()
}

// probeNode runs the health probe sequence for a single node.
func (hc *HealthChecker) probeNode(ctx context.Context, node *Node) {
	log := WithContext(ctx)

	// Get or create circuit breaker for this node.
	bRaw, _ := hc.breakers.LoadOrStore(node.ID, newNodeBreaker())
	breaker := bRaw.(*nodeBreaker)

	// Check circuit breaker state.
	state := breaker.state.Load().(string)
	if state == "open" {
		lastFail := breaker.lastFailure.Load().(time.Time)
		if time.Since(lastFail) < offlineProbeInterval {
			return // Still in cooldown, skip probe.
		}
		// Transition to half-open: allow one probe.
		breaker.state.Store("half-open")
		log.Info("health_checker: circuit half-open, attempting recovery probe",
			slog.String("node_id", node.ID),
		)
	}

	baseURL := fmt.Sprintf("http://%s", node.Host)

	// ── Probe 1: GET /api/tags → alive check + model discovery ──────────
	start := time.Now()
	models, err := hc.probeTags(ctx, baseURL, node)
	latencyMs := time.Since(start).Milliseconds()

	if err != nil {
		hc.recordFailure(ctx, node.ID, breaker, err)
		return
	}

	// ── Probe 2: GET /api/ps → load measurement ─────────────────────────
	load := hc.probeLoad(ctx, baseURL, node)

	// ── Success: update health ───────────────────────────────────────────
	health := NodeHealth{
		Status:       NodeStatusHealthy,
		LastSeen:     time.Now(),
		CurrentLoad:  load,
		LoadedModels: models,
		LatencyMs:    latencyMs,
	}

	hc.registry.UpdateHealth(node.ID, health)
	hc.recordSuccess(node.ID, breaker)

	// Emit Prometheus metric.
	Metrics.SetNodeHealth(node.ID, node.ModelTag, NodeStatusHealthy)

	log.Debug("health_checker: node healthy",
		slog.String("node_id", node.ID),
		slog.Int64("latency_ms", latencyMs),
		slog.Float64("load", load),
		slog.Int("models", len(models)),
	)
}

// probeTags calls GET /api/tags on the node and returns model names.
func (hc *HealthChecker) probeTags(ctx context.Context, baseURL string, node *Node) ([]string, error) {
	url := strings.TrimRight(baseURL, "/") + "/api/tags"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build tags request: %w", err)
	}
	if node.AuthToken != "" {
		req.Header.Set("Authorization", "Bearer "+node.AuthToken)
	}

	resp, err := hc.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("tags probe failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("tags probe HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return nil, fmt.Errorf("tags probe read: %w", err)
	}

	var tagsResp struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &tagsResp); err != nil {
		return nil, fmt.Errorf("tags probe parse: %w", err)
	}

	names := make([]string, len(tagsResp.Models))
	for i, m := range tagsResp.Models {
		names[i] = m.Name
	}
	return names, nil
}

// probeLoad calls GET /api/ps on the node and returns a load factor 0.0-1.0.
func (hc *HealthChecker) probeLoad(ctx context.Context, baseURL string, node *Node) float64 {
	url := strings.TrimRight(baseURL, "/") + "/api/ps"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0
	}
	if node.AuthToken != "" {
		req.Header.Set("Authorization", "Bearer "+node.AuthToken)
	}

	resp, err := hc.httpClient.Do(req)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 32*1024))
	if err != nil {
		return 0
	}

	var psResp struct {
		Models []struct {
			SizeVRAM int64 `json:"size_vram"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &psResp); err != nil {
		return 0
	}

	// Estimate load as fraction of node memory consumed by loaded models.
	if node.Capabilities.MemoryGB <= 0 {
		return 0
	}
	var totalVRAMBytes int64
	for _, m := range psResp.Models {
		totalVRAMBytes += m.SizeVRAM
	}
	usedGB := float64(totalVRAMBytes) / (1 << 30)
	load := usedGB / node.Capabilities.MemoryGB
	if load > 1.0 {
		load = 1.0
	}
	return load
}

// recordFailure handles a probe failure, advancing the circuit breaker.
func (hc *HealthChecker) recordFailure(ctx context.Context, nodeID string, breaker *nodeBreaker, err error) {
	log := WithContext(ctx)
	breaker.consecutiveSuccess.Store(0)
	failures := breaker.consecutiveFailures.Add(1)
	breaker.lastFailure.Store(time.Now())

	if failures >= int32(circuitOpenThreshold) {
		breaker.state.Store("open")
		hc.registry.UpdateHealth(nodeID, NodeHealth{
			Status:   NodeStatusOffline,
			LastSeen: time.Now(),
		})
		Metrics.SetNodeHealth(nodeID, "", NodeStatusOffline)
		log.Warn("health_checker: circuit OPEN — node marked offline",
			slog.String("node_id", nodeID),
			slog.Int("consecutive_failures", int(failures)),
			slog.String("error", err.Error()),
		)
	} else {
		hc.registry.UpdateHealth(nodeID, NodeHealth{
			Status:   NodeStatusDegraded,
			LastSeen: time.Now(),
		})
		Metrics.SetNodeHealth(nodeID, "", NodeStatusDegraded)
		log.Warn("health_checker: probe failed",
			slog.String("node_id", nodeID),
			slog.Int("consecutive_failures", int(failures)),
			slog.String("error", err.Error()),
		)
	}
}

// recordSuccess handles a probe success, potentially closing the circuit.
func (hc *HealthChecker) recordSuccess(nodeID string, breaker *nodeBreaker) {
	breaker.consecutiveFailures.Store(0)
	successes := breaker.consecutiveSuccess.Add(1)

	state := breaker.state.Load().(string)
	if state == "half-open" && successes >= int32(circuitHalfOpenSuccesses) {
		breaker.state.Store("closed")
		Log().Info("health_checker: circuit CLOSED — node recovered",
			slog.String("node_id", nodeID),
		)
	} else if state == "open" {
		// Shouldn't happen but defensively handle.
		breaker.state.Store("half-open")
	}
}
