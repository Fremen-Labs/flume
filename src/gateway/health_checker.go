package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
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
//   2. GET /api/ps    → measures latency + current load + VRAM discovery
//   3. POST /api/show → model metadata (context window, parameter size, family)
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
	tagsResult, err := hc.probeTags(ctx, baseURL, node)
	latencyMs := time.Since(start).Milliseconds()

	if err != nil {
		hc.recordFailure(ctx, node.ID, breaker, err)
		return
	}

	// ── Auto-Repair Misconfigured Model Tags ────────────────────────────
	// If the user's configured model tag is missing or not loaded on the hardware,
	// self-heal the configuration to securely match reality.
	modelMatches := false
	if node.ModelTag != "" {
		tagLower := strings.ToLower(node.ModelTag)
		for _, m := range tagsResult.models {
			mLower := strings.ToLower(m)
			if mLower == tagLower || strings.HasPrefix(mLower, tagLower+":") || strings.HasPrefix(tagLower, mLower+":") {
				modelMatches = true
				break
			}
		}
	}

	if !modelMatches && len(tagsResult.models) > 0 {
		fallbackTag := tagsResult.models[0]
		log.Warn("health_checker: node model tag mismatch, auto-repairing",
			slog.String("node_id", node.ID),
			slog.String("expected_tag", node.ModelTag),
			slog.String("repaired_tag", fallbackTag),
		)
		if err := hc.registry.AutoRepairModelTag(ctx, node.ID, fallbackTag); err != nil {
			log.Error("health_checker: failed to persist repaired model tag",
				slog.String("node_id", node.ID),
				slog.String("error", err.Error()),
			)
		} else {
			// Mutate local struct so subsequent probes (ReasoningScore) use the correct tag
			node.ModelTag = fallbackTag
		}
	}

	// ── Probe 2: GET /api/ps → load measurement + VRAM discovery ────────
	load, totalVRAMBytes, totalModelSizeBytes := hc.probeLoad(ctx, baseURL, node)

	// ── Probe 3: POST /api/show → model metadata (context, family, params)
	showResult, showErr := hc.probeShow(ctx, baseURL, node, tagsResult.models)
	if showErr != nil {
		log.Debug("health_checker: show probe failed (non-fatal, using tags fallback)",
			slog.String("node_id", node.ID),
			slog.String("error", showErr.Error()),
		)
	}

	// ── Auto-discover capabilities from live Ollama responses ───────────
	discovered := NodeCapabilities{}

	// MaxContext: dynamic from /api/show model_info.<arch>.context_length.
	if showErr == nil && showResult.contextLength > 0 {
		discovered.MaxContext = showResult.contextLength
	}

	// ReasoningScore: estimated from model family + parameter count.
	if showErr == nil && showResult.parameterSize != "" {
		discovered.ReasoningScore = estimateReasoningScore(
			node.ModelTag, showResult.family, showResult.parameterSize,
		)
	}

	// Quantization: prefer /api/show (model-specific) over /api/tags (first match).
	if showErr == nil && showResult.quantization != "" {
		discovered.Quantization = showResult.quantization
	} else if tagsResult.quantization != "" {
		discovered.Quantization = tagsResult.quantization
	}

	// Memory: cross-platform estimation using both VRAM and total model size
	// to detect Apple Silicon (unified), discrete GPU, or CPU-only inference.
	if totalVRAMBytes > 0 || totalModelSizeBytes > 0 {
		discovered.MemoryGB = estimateSystemMemory(totalModelSizeBytes, totalVRAMBytes)
	} else if tagsResult.primaryModelSizeBytes > 0 {
		// Fallback: no models loaded in VRAM (/api/ps empty — Ollama unloads
		// idle models). Use the model's on-disk file size from /api/tags as
		// a proxy for how much memory it needs when loaded.
		discovered.MemoryGB = estimateSystemMemory(
			tagsResult.primaryModelSizeBytes, tagsResult.primaryModelSizeBytes,
		)
	}

	// TPS estimate: rough heuristic from probe latency.
	if latencyMs > 0 {
		switch {
		case latencyMs < 100:
			discovered.EstimatedTPS = 60
		case latencyMs < 500:
			discovered.EstimatedTPS = 30
		default:
			discovered.EstimatedTPS = 10
		}
	}

	hc.registry.UpdateCapabilities(node.ID, discovered)

	// Re-compute load if the initial probeLoad returned 0 because MemoryGB
	// was not yet set on the node snapshot. Now that UpdateCapabilities has
	// populated MemoryGB, we can derive load from the raw VRAM data.
	if load == 0 && totalVRAMBytes > 0 && discovered.MemoryGB > 0 {
		usedGB := float64(totalVRAMBytes) / (1 << 30)
		load = usedGB / discovered.MemoryGB
		if load > 1.0 {
			load = 1.0
		}
	}

	// ── Success: update health ───────────────────────────────────────────
	health := NodeHealth{
		Status:       NodeStatusHealthy,
		LastSeen:     time.Now(),
		CurrentLoad:  load,
		LoadedModels: tagsResult.models,
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
		slog.Int("models", len(tagsResult.models)),
		slog.String("quantization", tagsResult.quantization),
	)
}

// tagsProbeResult holds model names and discovered metadata from /api/tags.
type tagsProbeResult struct {
	models                []string
	quantization          string // from details.quantization_level of primary model
	primaryModelSizeBytes int64  // on-disk file size of the node's assigned model
}

// probeTags calls GET /api/tags on the node and returns model names plus
// metadata details (quantization, etc.) for the primary assigned model.
func (hc *HealthChecker) probeTags(ctx context.Context, baseURL string, node *Node) (tagsProbeResult, error) {
	url := strings.TrimRight(baseURL, "/") + "/api/tags"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return tagsProbeResult{}, fmt.Errorf("build tags request: %w", err)
	}
	if node.AuthToken != "" {
		req.Header.Set("Authorization", "Bearer "+node.AuthToken)
	}

	resp, err := hc.httpClient.Do(req)
	if err != nil {
		return tagsProbeResult{}, fmt.Errorf("tags probe failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return tagsProbeResult{}, fmt.Errorf("tags probe HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return tagsProbeResult{}, fmt.Errorf("tags probe read: %w", err)
	}

	var tagsResp struct {
		Models []struct {
			Name    string `json:"name"`
			Size    int64  `json:"size"`
			Details struct {
				QuantizationLevel string `json:"quantization_level"`
				ParameterSize     string `json:"parameter_size"`
				Family            string `json:"family"`
			} `json:"details"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &tagsResp); err != nil {
		return tagsProbeResult{}, fmt.Errorf("tags probe parse: %w", err)
	}

	result := tagsProbeResult{}
	result.models = make([]string, len(tagsResp.Models))
	for i, m := range tagsResp.Models {
		result.models[i] = m.Name

		// Extract quantization from the primary model or the first model with details.
		if result.quantization == "" && m.Details.QuantizationLevel != "" {
			// Prefer matching the node's assigned model_tag.
			if node.ModelTag == "" || strings.HasPrefix(m.Name, strings.Split(node.ModelTag, ":")[0]) {
				result.quantization = m.Details.QuantizationLevel
			}
		}

		// Capture the primary model's file size for memory estimation fallback
		// (used when /api/ps returns no loaded models).
		if result.primaryModelSizeBytes == 0 && m.Size > 0 {
			if node.ModelTag == "" || strings.HasPrefix(m.Name, strings.Split(node.ModelTag, ":")[0]) {
				result.primaryModelSizeBytes = m.Size
			}
		}
	}
	// Fallback: if no primary match, use the first model's quantization.
	if result.quantization == "" && len(tagsResp.Models) > 0 {
		result.quantization = tagsResp.Models[0].Details.QuantizationLevel
	}
	return result, nil
}

// probeLoad calls GET /api/ps on the node and returns a load factor 0.0-1.0,
// the total VRAM bytes consumed, and the total model size bytes.
// The ratio of VRAM to model size reveals the memory architecture:
//   - VRAM ≈ Size  → unified memory (Apple Silicon)
//   - VRAM < Size  → discrete GPU with CPU offload (NVIDIA/AMD)
//   - VRAM = 0     → CPU-only inference (no GPU)
func (hc *HealthChecker) probeLoad(ctx context.Context, baseURL string, node *Node) (float64, int64, int64) {
	log := WithContext(ctx)
	url := strings.TrimRight(baseURL, "/") + "/api/ps"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		log.Warn("load probe failed: build request", slog.String("node_id", node.ID), slog.String("error", err.Error()))
		return 0, 0, 0
	}
	if node.AuthToken != "" {
		req.Header.Set("Authorization", "Bearer "+node.AuthToken)
	}

	resp, err := hc.httpClient.Do(req)
	if err != nil {
		log.Warn("load probe failed: http request", slog.String("node_id", node.ID), slog.String("error", err.Error()))
		return 0, 0, 0
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Warn("load probe failed: non-200 status", slog.String("node_id", node.ID), slog.Int("status", resp.StatusCode))
		return 0, 0, 0
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 32*1024))
	if err != nil {
		log.Warn("load probe failed: read body", slog.String("node_id", node.ID), slog.String("error", err.Error()))
		return 0, 0, 0
	}

	var psResp struct {
		Models []struct {
			Size     int64 `json:"size"`
			SizeVRAM int64 `json:"size_vram"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &psResp); err != nil {
		log.Warn("load probe failed: json unmarshal", slog.String("node_id", node.ID), slog.String("error", err.Error()))
		return 0, 0, 0
	}

	var totalVRAMBytes, totalModelSizeBytes int64
	for _, m := range psResp.Models {
		totalVRAMBytes += m.SizeVRAM
		totalModelSizeBytes += m.Size
	}

	// Estimate load as fraction of node memory consumed by loaded models.
	if node.Capabilities.MemoryGB <= 0 {
		// Can't compute load without a memory baseline, but still return
		// the raw bytes for auto-discovery by callers.
		return 0, totalVRAMBytes, totalModelSizeBytes
	}
	usedGB := float64(totalVRAMBytes) / (1 << 30)
	load := usedGB / node.Capabilities.MemoryGB
	if load > 1.0 {
		load = 1.0
	}
	return load, totalVRAMBytes, totalModelSizeBytes
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

// ─────────────────────────────────────────────────────────────────────────────
// Probe 3: POST /api/show — Model Metadata Discovery
// ─────────────────────────────────────────────────────────────────────────────

// showProbeResult holds metadata discovered from POST /api/show.
type showProbeResult struct {
	contextLength int    // from model_info.<arch>.context_length
	parameterSize string // e.g. "7B", "32B"
	family        string // e.g. "llama", "qwen2"
	quantization  string // e.g. "Q4_K_M"
}

// probeShow calls POST /api/show on the node for its primary model,
// extracting the real context window, parameter size, and architecture family.
// If the configured model_tag is not found, it falls back to the first
// available model discovered from /api/tags.
func (hc *HealthChecker) probeShow(ctx context.Context, baseURL string, node *Node, availableModels []string) (showProbeResult, error) {
	// Try the configured model_tag first.
	if node.ModelTag != "" {
		result, err := hc.callShowAPI(ctx, baseURL, node.AuthToken, node.ModelTag)
		if err == nil {
			return result, nil
		}
	}

	// Fallback: try each discovered model from /api/tags.
	for _, model := range availableModels {
		if model == node.ModelTag {
			continue // already tried
		}
		result, err := hc.callShowAPI(ctx, baseURL, node.AuthToken, model)
		if err == nil {
			return result, nil
		}
	}

	return showProbeResult{}, fmt.Errorf("show probe: no model responded (tried %q + %d fallbacks)", node.ModelTag, len(availableModels))
}

// callShowAPI performs a single POST /api/show call for the given model name
// and parses the response into a showProbeResult.
func (hc *HealthChecker) callShowAPI(ctx context.Context, baseURL, authToken, modelName string) (showProbeResult, error) {
	url := strings.TrimRight(baseURL, "/") + "/api/show"
	payload := fmt.Sprintf(`{"name":%q}`, modelName)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(payload))
	if err != nil {
		return showProbeResult{}, fmt.Errorf("build show request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if authToken != "" {
		req.Header.Set("Authorization", "Bearer "+authToken)
	}

	resp, err := hc.httpClient.Do(req)
	if err != nil {
		return showProbeResult{}, fmt.Errorf("show probe failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return showProbeResult{}, fmt.Errorf("show probe HTTP %d for %q", resp.StatusCode, modelName)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 2*1024*1024)) // 2MB — vision models have large tensor metadata
	if err != nil {
		return showProbeResult{}, fmt.Errorf("show probe read: %w", err)
	}

	var showResp struct {
		Details struct {
			Family            string `json:"family"`
			ParameterSize     string `json:"parameter_size"`
			QuantizationLevel string `json:"quantization_level"`
		} `json:"details"`
		ModelInfo map[string]interface{} `json:"model_info"`
	}
	if err := json.Unmarshal(body, &showResp); err != nil {
		return showProbeResult{}, fmt.Errorf("show probe parse: %w", err)
	}

	result := showProbeResult{
		parameterSize: showResp.Details.ParameterSize,
		family:        showResp.Details.Family,
		quantization:  showResp.Details.QuantizationLevel,
	}

	// Extract context_length from model_info — key varies by architecture
	// (e.g. "llama.context_length", "qwen2.context_length").
	for key, val := range showResp.ModelInfo {
		if strings.HasSuffix(key, ".context_length") {
			if v, ok := val.(float64); ok {
				result.contextLength = int(v)
				break
			}
		}
	}

	return result, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Capability Estimation Heuristics
// ─────────────────────────────────────────────────────────────────────────────

// estimateReasoningScore assigns a 1-10 reasoning capability score based on
// model family, parameter count, and well-known model characteristics.
//
// Score bands:
//
//	1-3: Small/fast models (phi, tinyllama, gemma-2b)
//	4-6: Mid-range models (7B-14B general purpose)
//	7-8: Strong models (32B+ or reasoning-optimized)
//	9-10: Frontier-class local models (70B+, deepseek-r1)
func estimateReasoningScore(modelTag, family, parameterSize string) int {
	paramB := parseParamBillions(parameterSize)

	// Base score from parameter count.
	var base int
	switch {
	case paramB >= 70:
		base = 8
	case paramB >= 32:
		base = 7
	case paramB >= 14:
		base = 6
	case paramB >= 7:
		base = 5
	case paramB >= 3:
		base = 4
	default:
		base = 3
	}

	// Model-specific adjustments based on well-known architectures.
	name := strings.ToLower(modelTag)
	switch {
	case strings.Contains(name, "deepseek-r1") || strings.Contains(name, "deepseek-reasoner"):
		base += 2 // purpose-built reasoning model
	case strings.Contains(name, "qwen2.5-coder") || strings.Contains(name, "qwen3"):
		base += 1 // strong structured reasoning
	case strings.Contains(name, "codellama") || strings.Contains(name, "starcoder"):
		base += 1 // good code reasoning
	case strings.Contains(name, "phi"):
		base -= 1 // efficient but lighter reasoning
	case strings.Contains(name, "tinyllama"):
		base -= 1
	}

	// Clamp to valid range.
	if base < 1 {
		base = 1
	}
	if base > 10 {
		base = 10
	}
	return base
}

// parseParamBillions extracts the parameter count in billions from strings
// like "7B", "32B", "0.5B", "70.6B".
func parseParamBillions(s string) float64 {
	s = strings.TrimSpace(strings.ToUpper(s))
	s = strings.TrimSuffix(s, "B")
	if v, err := strconv.ParseFloat(s, 64); err == nil {
		return v
	}
	return 0
}

// estimateSystemMemory detects the memory architecture from the ratio of
// VRAM to total model size and returns an estimated total memory capacity.
//
// Detection logic:
//   - VRAM ≥ 90% of model size → Unified memory (Apple Silicon)
//   - VRAM > 0 but < 90%       → Discrete GPU (NVIDIA/AMD on Linux/Windows)
//   - VRAM = 0, model size > 0  → CPU-only inference (no GPU)
//   - Both zero                 → No data, return 0
func estimateSystemMemory(totalModelSizeBytes, totalVRAMBytes int64) float64 {
	if totalVRAMBytes == 0 && totalModelSizeBytes == 0 {
		return 0
	}

	isUnifiedMemory := totalVRAMBytes > 0 &&
		totalModelSizeBytes > 0 &&
		float64(totalVRAMBytes) >= float64(totalModelSizeBytes)*0.9

	isCPUOnly := totalVRAMBytes == 0 && totalModelSizeBytes > 0

	// Determine how many bytes are in use for inference.
	var usedBytes int64
	switch {
	case isCPUOnly:
		usedBytes = totalModelSizeBytes
	case totalVRAMBytes > 0:
		usedBytes = totalVRAMBytes
	default:
		usedBytes = totalModelSizeBytes
	}

	// Models typically consume ~70% of available memory.
	estimatedGB := float64(usedBytes) / (1 << 30) / 0.7

	switch {
	case isUnifiedMemory:
		// Apple Silicon: M1/M2/M3/M4 unified memory configs.
		return roundToConfig(estimatedGB, []float64{8, 16, 24, 32, 36, 48, 64, 96, 128, 192})

	case isCPUOnly:
		// CPU-only (no GPU): standard system RAM configs (Linux/Windows/macOS).
		return roundToConfig(estimatedGB, []float64{8, 16, 32, 64, 128, 256, 512})

	default:
		// Discrete GPU (Linux/Windows): common NVIDIA/AMD VRAM configs.
		return roundToConfig(estimatedGB, []float64{4, 6, 8, 10, 12, 16, 24, 48, 80})
	}
}

// roundToConfig rounds an estimated GB figure up to the nearest value in
// the given sorted list of common hardware memory configurations.
func roundToConfig(estimatedGB float64, configs []float64) float64 {
	for _, cfg := range configs {
		if estimatedGB <= cfg {
			return cfg
		}
	}
	return estimatedGB // larger than any known config
}

