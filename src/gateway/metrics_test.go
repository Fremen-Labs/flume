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

// setupTestMetrics replaces the global Metrics singleton with a clean instance
// for test isolation, and returns a cleanup function to be deferred.
func setupTestMetrics() (*metricsRegistry, func()) {
	old := Metrics
	clean := &metricsRegistry{
		EnsembleRequests:   newCounterVec(),
		EnsembleScores:     newHistogram([]float64{10, 20, 30, 40, 50, 60, 70, 80, 90, 100}),
		EnsembleDuration:   newHistogram([]float64{0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0}),
		EscalationTotal:    &simpleCounter{},
		LocalRequests:      newCounterVec(),
		VRAMPressureEvents: &simpleCounter{},
		RequestDuration:    newHistogram([]float64{0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0}),
		ActiveModels:       newGaugeVec(),
		NodeRequests:       newCounterVec(),
		RoutingDecisions:   newCounterVec(),
		NodeLoad:           newGaugeVec(),
		NodeHealthGauge:    newGaugeVec(),
		LocalOffloadPct:    newGaugeVec(),
		WorkerTokens:       newCounterVec(),
		FrontierSpend:        newCounterVec(),
		FrontierCircuitBreaks: newCounterVec(),
		ConcurrencyThrottledTotal: &simpleCounter{},
		BackoffEventsTotal:        &simpleCounter{},
		TasksBlockedTotal:         &simpleCounter{},
	}
	Metrics = clean
	return clean, func() { Metrics = old }
}

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

func TestMetrics_RecordRequest(t *testing.T) {
	_, cleanup := setupTestMetrics()
	defer cleanup()

	Metrics.RecordRequest("ollama", true, 500*time.Millisecond)
	Metrics.RecordRequest("ollama", false, 1*time.Second)
	Metrics.RecordRequest("openai", true, 200*time.Millisecond)

	// Check duration histogram: 3 unique label combos (ollama/true, ollama/false, openai/true)
	snap := Metrics.RequestDuration.snapshot()
	if len(snap) != 3 {
		t.Errorf("duration label sets = %d, want 3", len(snap))
	}

	// Check success/failure counters
	reqSnap := Metrics.LocalRequests.snapshot()
	if reqSnap[`status="success"`] != 2 {
		t.Errorf("success requests = %d, want 2", reqSnap[`status="success"`])
	}
	if reqSnap[`status="failure"`] != 1 {
		t.Errorf("failure requests = %d, want 1", reqSnap[`status="failure"`])
	}
}

func TestMetrics_RecordEnsemble(t *testing.T) {
	_, cleanup := setupTestMetrics()
	defer cleanup()

	Metrics.RecordEnsemble("qwen2.5-coder:7b", "tool_call", 3, 85, 1200*time.Millisecond)
	Metrics.RecordEnsemble("qwen2.5-coder:7b", "tool_call", 3, 72, 800*time.Millisecond)

	snap := Metrics.EnsembleRequests.snapshot()
	key := `model_family="qwen2.5-coder",task_type="tool_call",size="3"`
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
	_, cleanup := setupTestMetrics()
	defer cleanup()

	// Seed some data
	Metrics.RecordRequest("ollama", true, 100*time.Millisecond)
	Metrics.RecordEnsemble("qwen:7b", "chat", 2, 90, 400*time.Millisecond)
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
		"flume_ensemble_decision_duration_seconds",
		"flume_escalation_total",
		"flume_local_requests_total",
		"flume_vram_pressure_events_total",
		"flume_request_duration_seconds",
		"flume_active_models",
		"flume_up",
		"flume_build_info",
		"go_goroutines",
		"go_memstats_alloc_bytes",
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
	_, cleanup := setupTestMetrics()
	defer cleanup()

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
	if !strings.Contains(body, "flume_escalation_total") {
		t.Error("missing HELP for escalation_total even with no data")
	}
	if !strings.Contains(body, "# HELP flume_local_requests_total") {
		t.Error("missing metric flume_local_requests_total in output")
	}
}
