package gateway

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Unit tests for the Prometheus metrics exporter.
// ─────────────────────────────────────────────────────────────────────────────

func TestMetrics_Counter(t *testing.T) {
	c := newCounterVec()
	c.Inc(`model="qwen",task_type="chat"`)
	c.Inc(`model="qwen",task_type="chat"`)
	c.Inc(`model="llama",task_type="tool"`)

	snap := c.snapshot()
	if snap[`model="qwen",task_type="chat"`] != 2 {
		t.Errorf("qwen chat = %d, want 2", snap[`model="qwen",task_type="chat"`])
	}
	if snap[`model="llama",task_type="tool"`] != 1 {
		t.Errorf("llama tool = %d, want 1", snap[`model="llama",task_type="tool"`])
	}
}

func TestMetrics_Histogram(t *testing.T) {
	h := newHistogram([]float64{1.0, 5.0, 10.0})
	h.Observe("", 0.5)
	h.Observe("", 3.0)
	h.Observe("", 7.0)
	h.Observe("", 15.0)

	snap := h.snapshot()
	d := snap[""]
	if d == nil {
		t.Fatal("empty-label data is nil")
	}
	if d.count != 4 {
		t.Errorf("count = %d, want 4", d.count)
	}
	// bucket[0] le=1.0 → 0.5 = 1
	if d.bucketCounts[0] != 1 {
		t.Errorf("bucket[0] (le=1.0) = %d, want 1", d.bucketCounts[0])
	}
	// bucket[1] le=5.0 → 3.0 = 1
	if d.bucketCounts[1] != 1 {
		t.Errorf("bucket[1] (le=5.0) = %d, want 1", d.bucketCounts[1])
	}
	// bucket[2] le=10.0 → 7.0 = 1
	if d.bucketCounts[2] != 1 {
		t.Errorf("bucket[2] (le=10.0) = %d, want 1", d.bucketCounts[2])
	}
}

func TestMetrics_Gauge(t *testing.T) {
	g := newGaugeVec()
	g.Set(`model="qwen"`, 1.0)
	g.Set(`model="llama"`, 1.0)
	g.Set(`model="qwen"`, 0.0) // overwrite

	snap := g.snapshot()
	if snap[`model="qwen"`] != 0.0 {
		t.Errorf("qwen = %f, want 0.0", snap[`model="qwen"`])
	}
	if snap[`model="llama"`] != 1.0 {
		t.Errorf("llama = %f, want 1.0", snap[`model="llama"`])
	}
}

func TestMetrics_RollingRate(t *testing.T) {
	r := &rollingRate{}

	// No data = 1.0 (healthy assumption)
	if r.Rate() != 1.0 {
		t.Errorf("empty rate = %f, want 1.0", r.Rate())
	}

	r.RecordSuccess()
	r.RecordSuccess()
	r.RecordFailure()

	rate := r.Rate()
	expected := 2.0 / 3.0
	if rate < expected-0.01 || rate > expected+0.01 {
		t.Errorf("rate = %f, want ~%f", rate, expected)
	}
}

func TestMetrics_RecordRequest(t *testing.T) {
	// Reset metrics for test isolation
	old := Metrics
	Metrics = &metricsRegistry{
		EnsembleRequests:   newCounterVec(),
		EnsembleScores:     newHistogram([]float64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100}),
		EscalationTotal:    &simpleCounter{},
		LocalSuccessRate:   &rollingRate{},
		VRAMPressureEvents: &simpleCounter{},
		RequestDuration:    newHistogram([]float64{0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0}),
		ActiveModels:       newGaugeVec(),
	}
	defer func() { Metrics = old }()

	Metrics.RecordRequest("ollama", true, 500*time.Millisecond)
	Metrics.RecordRequest("ollama", false, 1*time.Second)
	Metrics.RecordRequest("openai", true, 200*time.Millisecond)

	// Check duration histogram: 3 unique label combos (ollama/true, ollama/false, openai/true)
	snap := Metrics.RequestDuration.snapshot()
	if len(snap) != 3 {
		t.Errorf("duration label sets = %d, want 3", len(snap))
	}

	// Check success rate
	rate := Metrics.LocalSuccessRate.Rate()
	expected := 2.0 / 3.0
	if rate < expected-0.01 || rate > expected+0.01 {
		t.Errorf("success rate = %f, want ~%f", rate, expected)
	}
}

func TestMetrics_RecordEnsemble(t *testing.T) {
	old := Metrics
	Metrics = &metricsRegistry{
		EnsembleRequests:   newCounterVec(),
		EnsembleScores:     newHistogram([]float64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100}),
		EscalationTotal:    &simpleCounter{},
		LocalSuccessRate:   &rollingRate{},
		VRAMPressureEvents: &simpleCounter{},
		RequestDuration:    newHistogram([]float64{0.05, 0.1, 0.25, 0.5, 1.0}),
		ActiveModels:       newGaugeVec(),
	}
	defer func() { Metrics = old }()

	Metrics.RecordEnsemble("qwen2.5-coder:7b", "tool_call", 3, 85)
	Metrics.RecordEnsemble("qwen2.5-coder:7b", "tool_call", 3, 72)

	snap := Metrics.EnsembleRequests.snapshot()
	key := `model="qwen2.5-coder:7b",task_type="tool_call",size="3"`
	if snap[key] != 2 {
		t.Errorf("ensemble requests for key = %d, want 2", snap[key])
	}

	scores := Metrics.EnsembleScores.snapshot()
	d := scores[""]
	if d == nil {
		t.Fatal("score histogram data is nil")
	}
	if d.count != 2 {
		t.Errorf("score count = %d, want 2", d.count)
	}
}

func TestMetrics_HTTPEndpoint(t *testing.T) {
	// Use clean metrics
	old := Metrics
	Metrics = &metricsRegistry{
		EnsembleRequests:   newCounterVec(),
		EnsembleScores:     newHistogram([]float64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100}),
		EscalationTotal:    &simpleCounter{},
		LocalSuccessRate:   &rollingRate{},
		VRAMPressureEvents: &simpleCounter{},
		RequestDuration:    newHistogram([]float64{0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0}),
		ActiveModels:       newGaugeVec(),
	}
	defer func() { Metrics = old }()

	// Seed some data
	Metrics.RecordRequest("ollama", true, 100*time.Millisecond)
	Metrics.RecordEnsemble("qwen:7b", "chat", 2, 90)
	Metrics.RecordEscalation()
	Metrics.RecordVRAMPressure()
	Metrics.SetActiveModel("qwen:7b")

	InitLogger() // ensure logger is initialized for handler

	handler := HandleMetrics()
	req := httptest.NewRequest("GET", "/metrics", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	resp := rec.Result()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	body := rec.Body.String()

	// Check all required metric names are present
	required := []string{
		"flume_ensemble_requests_total",
		"flume_ensemble_score_histogram",
		"flume_escalation_total",
		"flume_local_success_rate",
		"flume_vram_pressure_events_total",
		"flume_request_duration_seconds",
		"flume_active_models",
	}
	for _, name := range required {
		if !strings.Contains(body, name) {
			t.Errorf("missing metric %q in output", name)
		}
	}

	// Check content type
	ct := resp.Header.Get("Content-Type")
	if !strings.Contains(ct, "text/plain") {
		t.Errorf("Content-Type = %q, want text/plain", ct)
	}

	// Check specific values
	if !strings.Contains(body, "flume_escalation_total 1") {
		t.Error("escalation_total should be 1")
	}
	if !strings.Contains(body, "flume_vram_pressure_events_total 1") {
		t.Error("vram_pressure_events should be 1")
	}
	if !strings.Contains(body, `flume_active_models{model="qwen:7b"} 1.0`) {
		t.Error("active_models should show qwen:7b = 1.0")
	}
}

func TestMetrics_EmptyOutput(t *testing.T) {
	// Verify it doesn't panic with zero data
	old := Metrics
	Metrics = &metricsRegistry{
		EnsembleRequests:   newCounterVec(),
		EnsembleScores:     newHistogram([]float64{50, 100}),
		EscalationTotal:    &simpleCounter{},
		LocalSuccessRate:   &rollingRate{},
		VRAMPressureEvents: &simpleCounter{},
		RequestDuration:    newHistogram([]float64{1.0}),
		ActiveModels:       newGaugeVec(),
	}
	defer func() { Metrics = old }()

	InitLogger()
	handler := HandleMetrics()
	req := httptest.NewRequest("GET", "/metrics", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}

	body := rec.Body.String()
	// Should still have HELP/TYPE lines even with no data
	if !strings.Contains(body, "# HELP flume_escalation_total") {
		t.Error("missing HELP for escalation_total even with no data")
	}
	if !strings.Contains(body, "flume_local_success_rate 1.000000") {
		t.Error("empty success rate should be 1.0 (healthy assumption)")
	}
}
