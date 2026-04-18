package gateway

import (
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Label represents a key-value pair for Prometheus metrics.
type Label struct {
	Key   string
	Value string
}

// formatLabels formats and escapes a slice of labels into a Prometheus label string.
func formatLabels(labels []Label) string {
	if len(labels) == 0 {
		return ""
	}
	var sb strings.Builder
	for i, l := range labels {
		if i > 0 {
			sb.WriteString(",")
		}
		sb.WriteString(l.Key)
		sb.WriteString(`="`)
		sb.WriteString(escapeLabelValue(l.Value))
		sb.WriteString(`"`)
	}
	return sb.String()
}

// escapeLabelValue escapes backslashes, double-quotes, and newlines in label values.
func escapeLabelValue(s string) string {
	s = strings.ReplaceAll(s, "\\", `\\`)
	s = strings.ReplaceAll(s, "\n", `\n`)
	s = strings.ReplaceAll(s, "\"", `\"`)
	return s
}

// parseModelFamily reduces a high-cardinality model string (e.g., qwen2.5-coder:7b)
// to a low-cardinality family string (e.g., qwen2.5-coder).
func parseModelFamily(model string) string {
	if idx := strings.IndexByte(model, ':'); idx != -1 {
		return model[:idx]
	}
	return model
}

var (
	buildVersion = "unknown"
	buildCommit  = "unknown"
	goVersion    = "unknown"
	buildOnce    sync.Once
)

// ensureBuildInfo reads version metadata statically, caching the result.
func ensureBuildInfo() {
	buildOnce.Do(func() {
		for i := 0; i < 3; i++ {
			prefix := strings.Repeat("../", i)
			data, err := os.ReadFile(filepath.Join(prefix, ".version"))
			if err == nil {
				buildVersion = strings.TrimSpace(string(data))
				break
			}
		}

		goVersion = runtime.Version()

		if info, ok := debug.ReadBuildInfo(); ok {
			for _, setting := range info.Settings {
				if setting.Key == "vcs.revision" {
					buildCommit = setting.Value
					if len(buildCommit) > 7 {
						buildCommit = buildCommit[:7]
					}
					break
				}
			}
		}
	})
}

// ─────────────────────────────────────────────────────────────────────────────
// Prometheus Metrics — Lightweight, zero-dependency Prometheus exporter.
//
// Implements the Prometheus text exposition format (v0.0.4) directly, avoiding
// the heavyweight prometheus/client_golang dependency.  Metrics are exposed on
// GET /metrics and cover the critical Flume Gateway observables:
//
//   flume_ensemble_requests_total       — counter (model, task_type, size)
//   flume_ensemble_score_histogram      — histogram of jury scores
//   flume_escalation_total              — counter for frontier fallback rate
//   flume_local_success_rate            — gauge: recent_success / recent_total
//   flume_vram_pressure_events_total    — counter
//   flume_request_duration_seconds      — histogram (provider, success)
//   flume_active_models                 — gauge (model)
//
// Logging follows the unified gateway pattern: slog + secret scrubbing.
// ─────────────────────────────────────────────────────────────────────────────

// ─── Counters ────────────────────────────────────────────────────────────────

// counterVec is a label-aware atomic counter.
type counterVec struct {
	mu     sync.Mutex
	counts map[string]uint64
}

func newCounterVec() *counterVec {
	return &counterVec{counts: make(map[string]uint64)}
}

func (c *counterVec) Inc(labels string) {
	c.mu.Lock()
	c.counts[labels]++
	c.mu.Unlock()
}

func (c *counterVec) snapshot() map[string]uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	snap := make(map[string]uint64, len(c.counts))
	for k, v := range c.counts {
		snap[k] = v
	}
	return snap
}

// simpleCounter is a single-dimension counter.
type simpleCounter struct {
	mu    sync.Mutex
	value uint64
}

func (c *simpleCounter) Inc() {
	c.mu.Lock()
	c.value++
	c.mu.Unlock()
}

func (c *simpleCounter) get() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.value
}

// ─── Histogram ───────────────────────────────────────────────────────────────

// histogram collects samples into pre-defined buckets.
type histogram struct {
	mu      sync.Mutex
	buckets []float64 // upper bounds (sorted)
	counts  map[string]*histogramData
}

type histogramData struct {
	bucketCounts []uint64
	sum          float64
	count        uint64
}

func newHistogram(buckets []float64) *histogram {
	return &histogram{
		buckets: buckets,
		counts:  make(map[string]*histogramData),
	}
}

func (h *histogram) Observe(labels string, value float64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	d, ok := h.counts[labels]
	if !ok {
		d = &histogramData{bucketCounts: make([]uint64, len(h.buckets))}
		h.counts[labels] = d
	}
	d.sum += value
	d.count++
	i := sort.Search(len(h.buckets), func(i int) bool { return h.buckets[i] >= value })
	if i < len(h.buckets) {
		d.bucketCounts[i]++
	}
}

func (h *histogram) snapshot() map[string]*histogramData {
	h.mu.Lock()
	defer h.mu.Unlock()
	snap := make(map[string]*histogramData, len(h.counts))
	for k, v := range h.counts {
		cp := &histogramData{
			bucketCounts: make([]uint64, len(v.bucketCounts)),
			sum:          v.sum,
			count:        v.count,
		}
		copy(cp.bucketCounts, v.bucketCounts)
		snap[k] = cp
	}
	return snap
}

// ─── Gauge ───────────────────────────────────────────────────────────────────

// gaugeVec is a label-aware gauge (last-write-wins).
type gaugeVec struct {
	mu     sync.Mutex
	values map[string]float64
}

func newGaugeVec() *gaugeVec {
	return &gaugeVec{values: make(map[string]float64)}
}

func (g *gaugeVec) Set(labels string, v float64) {
	g.mu.Lock()
	g.values[labels] = v
	g.mu.Unlock()
}

func (g *gaugeVec) snapshot() map[string]float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	snap := make(map[string]float64, len(g.values))
	for k, v := range g.values {
		snap[k] = v
	}
	return snap
}

// ─── Metrics registry (singleton) ────────────────────────────────────────────

// Metrics is the global metrics registry for the Flume Gateway.
var Metrics = &metricsRegistry{
	EnsembleRequests:     newCounterVec(),
	EnsembleScores:       newHistogram([]float64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100}),
	EnsembleDuration:     newHistogram([]float64{0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0}),
	EscalationTotal:      &simpleCounter{},
	LocalRequests:        newCounterVec(),
	VRAMPressureEvents:   &simpleCounter{},
	RequestDuration:      newHistogram([]float64{0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0}),
	ActiveModels:         newGaugeVec(),
	NodeRequests:         newCounterVec(),
	RoutingDecisions:     newCounterVec(),
	NodeLoad:             newGaugeVec(),
	NodeHealthGauge:      newGaugeVec(),
	LocalOffloadPct:      newGaugeVec(),
	WorkerTokens:         newCounterVec(),
}

type metricsRegistry struct {
	// flume_ensemble_requests_total{model_family, task_type, size}
	EnsembleRequests *counterVec

	// flume_ensemble_score_histogram{} — jury scores 0-100
	EnsembleScores *histogram

	// flume_ensemble_decision_duration_seconds{} — how long the jury took to score
	EnsembleDuration *histogram

	// flume_escalation_total — frontier fallback count
	EscalationTotal *simpleCounter

	// flume_local_requests_total{status} — raw success/failure counts
	LocalRequests *counterVec

	// flume_vram_pressure_events_total
	VRAMPressureEvents *simpleCounter

	// flume_request_duration_seconds{provider, success}
	RequestDuration *histogram

	// flume_active_models{model}
	ActiveModels *gaugeVec

	// ── Node Mesh metrics ──────────────────────────────────────────────

	// flume_node_requests_total{node_id, model}
	NodeRequests *counterVec

	// flume_routing_decision{strategy, task_type}
	RoutingDecisions *counterVec

	// flume_node_load{node_id}
	NodeLoad *gaugeVec

	// flume_node_health{node_id, model, status}
	NodeHealthGauge *gaugeVec

	// flume_local_offload_percentage
	LocalOffloadPct *gaugeVec

	// flume_worker_tokens_total{worker_name, direction}
	WorkerTokens *counterVec
}

// RecordRequest records a completed request's duration and success state.
func (m *metricsRegistry) RecordRequest(provider string, success bool, duration time.Duration) {
	successLabel := "false"
	if success {
		successLabel = "true"
	}
	labels := formatLabels([]Label{
		{"provider", provider},
		{"success", successLabel},
	})
	m.RequestDuration.Observe(labels, duration.Seconds())

	status := "failure"
	if success {
		status = "success"
	}
	m.LocalRequests.Inc(formatLabels([]Label{{"status", status}}))

	Log().Debug("metrics: request recorded",
		slog.String("component", "metrics"),
		slog.String("provider", provider),
		slog.Bool("success", success),
		slog.Float64("duration_s", duration.Seconds()),
	)
}

// RecordEnsemble records an ensemble execution.
func (m *metricsRegistry) RecordEnsemble(model, taskType string, size int, bestScore int, duration time.Duration) {
	family := parseModelFamily(model)
	labels := formatLabels([]Label{
		{"model_family", family},
		{"task_type", taskType},
		{"size", strconv.Itoa(size)},
	})
	m.EnsembleRequests.Inc(labels)
	m.EnsembleScores.Observe("", float64(bestScore))
	m.EnsembleDuration.Observe("", duration.Seconds())

	Log().Debug("metrics: ensemble recorded",
		slog.String("component", "metrics"),
		slog.String("model_family", family),
		slog.String("task_type", taskType),
		slog.Int("jury_size", size),
		slog.Int("best_score", bestScore),
		slog.Float64("duration_s", duration.Seconds()),
	)
}

// RecordEscalation tracks a frontier fallback event.
func (m *metricsRegistry) RecordEscalation() {
	m.EscalationTotal.Inc()
	Log().Debug("metrics: frontier escalation recorded",
		slog.String("component", "metrics"),
		slog.Uint64("total_escalations", m.EscalationTotal.get()),
	)
}

// RecordVRAMPressure tracks when ensemble is degraded due to VRAM pressure.
func (m *metricsRegistry) RecordVRAMPressure() {
	m.VRAMPressureEvents.Inc()
	Log().Debug("metrics: VRAM pressure event recorded",
		slog.String("component", "metrics"),
		slog.Uint64("total_vram_events", m.VRAMPressureEvents.get()),
	)
}

// SetActiveModel marks a model as active with a gauge value of 1.
func (m *metricsRegistry) SetActiveModel(model string) {
	labels := formatLabels([]Label{{"model", model}})
	m.ActiveModels.Set(labels, 1)
	Log().Debug("metrics: active model set",
		slog.String("component", "metrics"),
		slog.String("model", model),
	)
}

// RecordNodeRequest tracks a request routed to a specific node.
func (m *metricsRegistry) RecordNodeRequest(nodeID, model string) {
	labels := formatLabels([]Label{{"node_id", nodeID}, {"model", model}})
	m.NodeRequests.Inc(labels)
}

// RecordRoutingDecision tracks a routing strategy decision.
func (m *metricsRegistry) RecordRoutingDecision(strategy, taskType string) {
	labels := formatLabels([]Label{{"strategy", strategy}, {"task_type", taskType}})
	m.RoutingDecisions.Inc(labels)
}

// SetNodeLoad sets the current load gauge for a node.
func (m *metricsRegistry) SetNodeLoad(nodeID string, load float64) {
	labels := formatLabels([]Label{{"node_id", nodeID}})
	m.NodeLoad.Set(labels, load)
}

// SetNodeHealth sets the health status gauge for a node.
// RecordWorkerTokens records usage for a specific worker.
func (m *metricsRegistry) RecordWorkerTokens(workerName string, input, output int) {
	if workerName == "" {
		workerName = "unknown"
	}
	m.WorkerTokens.Inc(formatLabels([]Label{{"worker_name", workerName}, {"direction", "input"}}))
	for i := 1; i < input; i++ {
		m.WorkerTokens.Inc(formatLabels([]Label{{"worker_name", workerName}, {"direction", "input"}}))
	}
	m.WorkerTokens.Inc(formatLabels([]Label{{"worker_name", workerName}, {"direction", "output"}}))
	for i := 1; i < output; i++ {
		m.WorkerTokens.Inc(formatLabels([]Label{{"worker_name", workerName}, {"direction", "output"}}))
	}
}

// RecordWorkerTokensAdd is a much more efficient way to add N tokens using a custom method
func (c *counterVec) Add(labels string, val uint64) {
	c.mu.Lock()
	c.counts[labels] += val
	c.mu.Unlock()
}

// RecordWorkerTokensBatch directly adds instead of looping
func (m *metricsRegistry) RecordWorkerTokensBatch(workerName string, input, output int) {
	if workerName == "" {
		workerName = "unknown"
	}
	m.WorkerTokens.Add(formatLabels([]Label{{"worker_name", workerName}, {"direction", "input"}}), uint64(input))
	m.WorkerTokens.Add(formatLabels([]Label{{"worker_name", workerName}, {"direction", "output"}}), uint64(output))
}

func (m *metricsRegistry) SetNodeHealth(nodeID, model, status string) {
	// Reset all status values for this node, then set current.
	for _, s := range []string{"healthy", "degraded", "offline"} {
		labels := formatLabels([]Label{{"node_id", nodeID}, {"model", model}, {"status", s}})
		if s == status {
			m.NodeHealthGauge.Set(labels, 1)
		} else {
			m.NodeHealthGauge.Set(labels, 0)
		}
	}
}

// SetLocalOffloadPercentage sets the percentage of requests handled locally vs. frontier.
func (m *metricsRegistry) SetLocalOffloadPercentage(pct float64) {
	m.LocalOffloadPct.Set("", pct)
}

// ─── Prometheus text exposition ──────────────────────────────────────────────

// HandleMetrics returns an HTTP handler serving Prometheus text exposition format.
func HandleMetrics() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		log := Log()
		log.Debug("metrics endpoint scraped",
			slog.String("component", "metrics"),
		)

		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		w.WriteHeader(http.StatusOK)

		buf := make([]byte, 0, 4096)

		// ── System Health & Application ──────────────────────────────────
		buf = append(buf, "# HELP flume_up Whether the Flume gateway process is running (1 = up).\n"...)
		buf = append(buf, "# TYPE flume_up gauge\n"...)
		buf = append(buf, "flume_up 1\n"...)

		ensureBuildInfo()
		buf = append(buf, "# HELP flume_build_info Build version information.\n"...)
		buf = append(buf, "# TYPE flume_build_info gauge\n"...)
		buf = append(buf, "flume_build_info{version=\""...)
		buf = append(buf, escapeLabelValue(buildVersion)...)
		buf = append(buf, "\",commit=\""...)
		buf = append(buf, escapeLabelValue(buildCommit)...)
		buf = append(buf, "\",go_version=\""...)
		buf = append(buf, escapeLabelValue(goVersion)...)
		buf = append(buf, "\"} 1\n"...)

		// ── Go Runtime Metrics ─────────────────────────────────────────
		var mem runtime.MemStats
		runtime.ReadMemStats(&mem)

		buf = append(buf, "# HELP go_goroutines Number of goroutines that currently exist.\n"...)
		buf = append(buf, "# TYPE go_goroutines gauge\n"...)
		buf = append(buf, "go_goroutines "...)
		buf = strconv.AppendInt(buf, int64(runtime.NumGoroutine()), 10)
		buf = append(buf, '\n')

		buf = append(buf, "# HELP go_memstats_alloc_bytes Number of bytes allocated and still in use.\n"...)
		buf = append(buf, "# TYPE go_memstats_alloc_bytes gauge\n"...)
		buf = append(buf, "go_memstats_alloc_bytes "...)
		buf = strconv.AppendUint(buf, mem.Alloc, 10)
		buf = append(buf, '\n')
		
		buf = append(buf, "# HELP go_memstats_sys_bytes Number of bytes obtained from system.\n"...)
		buf = append(buf, "# TYPE go_memstats_sys_bytes gauge\n"...)
		buf = append(buf, "go_memstats_sys_bytes "...)
		buf = strconv.AppendUint(buf, mem.Sys, 10)
		buf = append(buf, '\n')

		buf = append(buf, "# HELP go_gc_duration_seconds_sum A summary of the pause duration of garbage collection cycles.\n"...)
		buf = append(buf, "# TYPE go_gc_duration_seconds_sum counter\n"...)
		buf = append(buf, "go_gc_duration_seconds_sum "...)
		buf = strconv.AppendFloat(buf, float64(mem.PauseTotalNs)/1e9, 'f', -1, 64)
		buf = append(buf, '\n')

		// ── flume_ensemble_requests_total ──────────────────────────────
		buf = append(buf, "# HELP flume_ensemble_requests_total Total ensemble requests by model_family, task type, and jury size.\n"...)
		buf = append(buf, "# TYPE flume_ensemble_requests_total counter\n"...)
		for labels, count := range Metrics.EnsembleRequests.snapshot() {
			buf = append(buf, "flume_ensemble_requests_total{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendUint(buf, count, 10)
			buf = append(buf, '\n')
		}

		// ── flume_ensemble_score_histogram ─────────────────────────────
		buf = appendHistogramMetric(buf, "flume_ensemble_score_histogram",
			"Distribution of ensemble jury scores (0-100).",
			Metrics.EnsembleScores)

		// ── flume_ensemble_decision_duration_seconds ────────────────────
		buf = appendHistogramMetric(buf, "flume_ensemble_decision_duration_seconds",
			"Distribution of ensemble jury consensus evaluation times from start to final score.",
			Metrics.EnsembleDuration)

		// ── flume_escalation_total ─────────────────────────────────────
		buf = append(buf, "# HELP flume_escalation_total Total frontier LLM fallback escalations.\n"...)
		buf = append(buf, "# TYPE flume_escalation_total counter\n"...)
		buf = append(buf, "flume_escalation_total "...)
		buf = strconv.AppendUint(buf, Metrics.EscalationTotal.get(), 10)
		buf = append(buf, '\n')

		// ── flume_local_requests_total ─────────────────────────────────
		buf = append(buf, "# HELP flume_local_requests_total Total number of local LLM requests by success status.\n"...)
		buf = append(buf, "# TYPE flume_local_requests_total counter\n"...)
		for reqLabels, count := range Metrics.LocalRequests.snapshot() {
			buf = append(buf, "flume_local_requests_total{"...)
			buf = append(buf, reqLabels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendUint(buf, count, 10)
			buf = append(buf, '\n')
		}

		// ── flume_vram_pressure_events_total ───────────────────────────
		buf = append(buf, "# HELP flume_vram_pressure_events_total Total VRAM pressure events causing ensemble degradation.\n"...)
		buf = append(buf, "# TYPE flume_vram_pressure_events_total counter\n"...)
		buf = append(buf, "flume_vram_pressure_events_total "...)
		buf = strconv.AppendUint(buf, Metrics.VRAMPressureEvents.get(), 10)
		buf = append(buf, '\n')

		// ── flume_request_duration_seconds ─────────────────────────────
		buf = appendHistogramMetric(buf, "flume_request_duration_seconds",
			"Request duration in seconds by provider and success.",
			Metrics.RequestDuration)

		// ── flume_active_models ────────────────────────────────────────
		buf = append(buf, "# HELP flume_active_models Currently active/loaded LLM models (1 = loaded).\n"...)
		buf = append(buf, "# TYPE flume_active_models gauge\n"...)
		for labels, val := range Metrics.ActiveModels.snapshot() {
			buf = append(buf, "flume_active_models{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendFloat(buf, val, 'f', 1, 64)
			buf = append(buf, '\n')
		}

		// ── flume_node_requests_total ──────────────────────────────────
		buf = append(buf, "# HELP flume_node_requests_total Total requests routed to each node.\n"...)
		buf = append(buf, "# TYPE flume_node_requests_total counter\n"...)
		for labels, count := range Metrics.NodeRequests.snapshot() {
			buf = append(buf, "flume_node_requests_total{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendUint(buf, count, 10)
			buf = append(buf, '\n')
		}

		// ── flume_routing_decision ────────────────────────────────────
		buf = append(buf, "# HELP flume_routing_decision Total routing decisions by strategy and task type.\n"...)
		buf = append(buf, "# TYPE flume_routing_decision counter\n"...)
		for labels, count := range Metrics.RoutingDecisions.snapshot() {
			buf = append(buf, "flume_routing_decision{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendUint(buf, count, 10)
			buf = append(buf, '\n')
		}

		// ── flume_node_load ───────────────────────────────────────────
		buf = append(buf, "# HELP flume_node_load Current load factor per node (0.0-1.0).\n"...)
		buf = append(buf, "# TYPE flume_node_load gauge\n"...)
		for labels, val := range Metrics.NodeLoad.snapshot() {
			buf = append(buf, "flume_node_load{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendFloat(buf, val, 'f', 3, 64)
			buf = append(buf, '\n')
		}

		// ── flume_node_health ─────────────────────────────────────────
		buf = append(buf, "# HELP flume_node_health Node health status (1 = this status is active).\n"...)
		buf = append(buf, "# TYPE flume_node_health gauge\n"...)
		for labels, val := range Metrics.NodeHealthGauge.snapshot() {
			buf = append(buf, "flume_node_health{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendFloat(buf, val, 'f', 1, 64)
			buf = append(buf, '\n')
		}

		// ── flume_local_offload_percentage ────────────────────────────
		buf = append(buf, "# HELP flume_local_offload_percentage Percentage of requests handled by local nodes.\n"...)
		buf = append(buf, "# TYPE flume_local_offload_percentage gauge\n"...)
		for labels, val := range Metrics.LocalOffloadPct.snapshot() {
			buf = append(buf, "flume_local_offload_percentage"...)
			if labels != "" {
				buf = append(buf, '{')  
				buf = append(buf, labels...)
				buf = append(buf, '}')  
			}
			buf = append(buf, ' ')
			buf = strconv.AppendFloat(buf, val, 'f', 1, 64)
			buf = append(buf, '\n')
		}

		// ── flume_worker_tokens_total ──────────────────────────────────
		buf = append(buf, "# HELP flume_worker_tokens_total Total tokens streamed per worker.\n"...)
		buf = append(buf, "# TYPE flume_worker_tokens_total counter\n"...)
		for labels, count := range Metrics.WorkerTokens.snapshot() {
			buf = append(buf, "flume_worker_tokens_total{"...)
			buf = append(buf, labels...)
			buf = append(buf, "} "...)
			buf = strconv.AppendUint(buf, count, 10)
			buf = append(buf, '\n')
		}

		_, err := w.Write(buf)
		if err != nil {
			log.Error("failed to write metrics",
				slog.String("component", "metrics"),
				slog.String("error", err.Error()),
			)
		}
	}
}

// appendHistogramMetric serialises a histogram to Prometheus text format.
func appendHistogramMetric(buf []byte, name, help string, h *histogram) []byte {
	buf = append(buf, "# HELP "...)
	buf = append(buf, name...)
	buf = append(buf, ' ')
	buf = append(buf, help...)
	buf = append(buf, '\n')
	buf = append(buf, "# TYPE "...)
	buf = append(buf, name...)
	buf = append(buf, " histogram\n"...)

	for labels, data := range h.snapshot() {
		var labelSuffix string
		if labels != "" {
			labelSuffix = "{" + labels + "}"
		}

		// Buckets
		var cumulative uint64
		for i, bound := range h.buckets {
			cumulative += data.bucketCounts[i]
			buf = append(buf, name...)
			buf = append(buf, "_bucket{"...)
			if labels != "" {
				buf = append(buf, labels...)
				buf = append(buf, ","...)
			}
			buf = append(buf, `le="`...)
			buf = strconv.AppendFloat(buf, bound, 'f', -1, 64)
			buf = append(buf, `"} `...)
			buf = strconv.AppendUint(buf, cumulative, 10)
			buf = append(buf, '\n')
		}
		// +Inf bucket
		buf = append(buf, name...)
		buf = append(buf, `_bucket{`...)
		if labels != "" {
			buf = append(buf, labels...)
			buf = append(buf, ","...)
		}
		buf = append(buf, `le="+Inf"} `...)
		buf = strconv.AppendUint(buf, data.count, 10)
		buf = append(buf, '\n')

		// _sum and _count
		buf = append(buf, name...)
		buf = append(buf, "_sum"...)
		buf = append(buf, labelSuffix...)
		buf = append(buf, ' ')
		buf = strconv.AppendFloat(buf, data.sum, 'f', 6, 64)
		buf = append(buf, '\n')

		buf = append(buf, name...)
		buf = append(buf, "_count"...)
		buf = append(buf, labelSuffix...)
		buf = append(buf, ' ')
		buf = strconv.AppendUint(buf, data.count, 10)
		buf = append(buf, '\n')
	}

	return buf
}
