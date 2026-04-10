package gateway

import (
	"log/slog"
	"net/http"
	"strconv"
	"sync"
	"time"
)

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
	for i, bound := range h.buckets {
		if value <= bound {
			d.bucketCounts[i]++
		}
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

// ─── Rolling success rate tracker ────────────────────────────────────────────

// rollingRate tracks success/total over a sliding window for gauge output.
type rollingRate struct {
	mu       sync.Mutex
	success  uint64
	total    uint64
	// We use simple monotonic counters; the gauge value is success/total.
}

func (r *rollingRate) RecordSuccess() {
	r.mu.Lock()
	r.success++
	r.total++
	r.mu.Unlock()
}

func (r *rollingRate) RecordFailure() {
	r.mu.Lock()
	r.total++
	r.mu.Unlock()
}

func (r *rollingRate) Rate() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.total == 0 {
		return 1.0 // no data yet = assume healthy
	}
	return float64(r.success) / float64(r.total)
}

// ─── Metrics registry (singleton) ────────────────────────────────────────────

// Metrics is the global metrics registry for the Flume Gateway.
var Metrics = &metricsRegistry{
	EnsembleRequests:     newCounterVec(),
	EnsembleScores:       newHistogram([]float64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100}),
	EscalationTotal:      &simpleCounter{},
	LocalSuccessRate:     &rollingRate{},
	VRAMPressureEvents:   &simpleCounter{},
	RequestDuration:      newHistogram([]float64{0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0}),
	ActiveModels:         newGaugeVec(),
}

type metricsRegistry struct {
	// flume_ensemble_requests_total{model, task_type, size}
	EnsembleRequests *counterVec

	// flume_ensemble_score_histogram{} — jury scores 0-100
	EnsembleScores *histogram

	// flume_escalation_total — frontier fallback count
	EscalationTotal *simpleCounter

	// flume_local_success_rate — derived gauge
	LocalSuccessRate *rollingRate

	// flume_vram_pressure_events_total
	VRAMPressureEvents *simpleCounter

	// flume_request_duration_seconds{provider, success}
	RequestDuration *histogram

	// flume_active_models{model}
	ActiveModels *gaugeVec
}

// RecordRequest records a completed request's duration and success state.
func (m *metricsRegistry) RecordRequest(provider string, success bool, duration time.Duration) {
	successLabel := "false"
	if success {
		successLabel = "true"
	}
	labels := `provider="` + provider + `",success="` + successLabel + `"`
	m.RequestDuration.Observe(labels, duration.Seconds())

	if success {
		m.LocalSuccessRate.RecordSuccess()
	} else {
		m.LocalSuccessRate.RecordFailure()
	}

	Log().Debug("metrics: request recorded",
		slog.String("component", "metrics"),
		slog.String("provider", provider),
		slog.Bool("success", success),
		slog.Float64("duration_s", duration.Seconds()),
		slog.Float64("success_rate", m.LocalSuccessRate.Rate()),
	)
}

// RecordEnsemble records an ensemble execution.
func (m *metricsRegistry) RecordEnsemble(model, taskType string, size int, bestScore int) {
	labels := `model="` + model + `",task_type="` + taskType + `",size="` + strconv.Itoa(size) + `"`
	m.EnsembleRequests.Inc(labels)
	m.EnsembleScores.Observe("", float64(bestScore))

	Log().Debug("metrics: ensemble recorded",
		slog.String("component", "metrics"),
		slog.String("model", model),
		slog.String("task_type", taskType),
		slog.Int("jury_size", size),
		slog.Int("best_score", bestScore),
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
	m.ActiveModels.Set(`model="` + model + `"`, 1)
	Log().Debug("metrics: active model set",
		slog.String("component", "metrics"),
		slog.String("model", model),
	)
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

		// ── flume_ensemble_requests_total ──────────────────────────────
		buf = append(buf, "# HELP flume_ensemble_requests_total Total ensemble requests by model, task type, and jury size.\n"...)
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

		// ── flume_escalation_total ─────────────────────────────────────
		buf = append(buf, "# HELP flume_escalation_total Total frontier LLM fallback escalations.\n"...)
		buf = append(buf, "# TYPE flume_escalation_total counter\n"...)
		buf = append(buf, "flume_escalation_total "...)
		buf = strconv.AppendUint(buf, Metrics.EscalationTotal.get(), 10)
		buf = append(buf, '\n')

		// ── flume_local_success_rate ───────────────────────────────────
		buf = append(buf, "# HELP flume_local_success_rate Ratio of successful local LLM requests (0.0 to 1.0).\n"...)
		buf = append(buf, "# TYPE flume_local_success_rate gauge\n"...)
		buf = append(buf, "flume_local_success_rate "...)
		buf = strconv.AppendFloat(buf, Metrics.LocalSuccessRate.Rate(), 'f', 6, 64)
		buf = append(buf, '\n')

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
		prefix := name
		if labels != "" {
			prefix = name + "{" + labels + "}"
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
		_ = prefix // used for consistent naming
		buf = append(buf, name...)
		buf = append(buf, "_sum"...)
		if labels != "" {
			buf = append(buf, "{"...)
			buf = append(buf, labels...)
			buf = append(buf, "}"...)
		}
		buf = append(buf, ' ')
		buf = strconv.AppendFloat(buf, data.sum, 'f', 6, 64)
		buf = append(buf, '\n')

		buf = append(buf, name...)
		buf = append(buf, "_count"...)
		if labels != "" {
			buf = append(buf, "{"...)
			buf = append(buf, labels...)
			buf = append(buf, "}"...)
		}
		buf = append(buf, ' ')
		buf = strconv.AppendUint(buf, data.count, 10)
		buf = append(buf, '\n')
	}

	return buf
}
