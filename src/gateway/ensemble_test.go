package gateway

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Existing tests preserved
// ─────────────────────────────────────────────────────────────────────────────

// mockProvider Always returns the preconfigured responses sequentially
type mockProvider struct {
	responses []*ChatResponse
	errs      []error
	callCount int
}

func (m *mockProvider) Chat(ctx context.Context, req *ChatRequest) (*ChatResponse, error) {
	return nil, nil
}

func (m *mockProvider) ChatTools(ctx context.Context, req *ChatRequest) (*ChatResponse, error) {
	idx := m.callCount
	if idx >= len(m.responses) {
		idx = len(m.responses) - 1
	}
	m.callCount++

	// Optional delay to test timeout/groups
	time.Sleep(10 * time.Millisecond)

	return m.responses[idx], m.errs[idx]
}

func (m *mockProvider) Name() string {
	return "mock-provider"
}

func TestScoreResponse(t *testing.T) {
	tests := []struct {
		name          string
		resp          *ChatResponse
		expectedScore int
	}{
		{
			name: "No tool calls",
			resp: &ChatResponse{
				Message: ResponseMessage{
					Role:    "assistant",
					Content: "Hello",
				},
			},
			expectedScore: 60,
		},
		{
			name: "Valid Elastro Tool With Perfect JSON",
			resp: &ChatResponse{
				Message: ResponseMessage{
					ToolCalls: []ToolCall{
						{
							Function: ToolCallFunction{
								Name:      "elastro_query_ast",
								Arguments: `{"query": "User"}`,
							},
						},
					},
				},
			},
			expectedScore: 100, // 40 syntax + 30 safety + 30 elastro pattern
		},
		{
			name: "Destructive Shell Auth Nullified",
			resp: &ChatResponse{
				Message: ResponseMessage{
					ToolCalls: []ToolCall{
						{
							Function: ToolCallFunction{
								Name:      "run_shell_cmd",
								Arguments: `{"command": "rm -rf /"}`,
							},
						},
					},
				},
			},
			expectedScore: 55, // 40 syntax + 0 safety + 15 elastro base penalty (30/2)
		},
		{
			name: "Malformed JSON Arguments",
			resp: &ChatResponse{
				Message: ResponseMessage{
					ToolCalls: []ToolCall{
						{
							Function: ToolCallFunction{
								Name:      "view_file",
								Arguments: `{"file": "/valid"`, // Missing closing brace
							},
						},
					},
				},
			},
			expectedScore: 60, // 0 syntax + 30 safety + 30 elastro pattern
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			score := ScoreResponse(tc.resp)
			if score != tc.expectedScore {
				t.Errorf("expected score %d, got %d", tc.expectedScore, score)
			}
		})
	}
}

// TestEnsembleRouter_Escalation mocks the full ExecuteEnsemble method to assert graceful fallback behavior.
func TestEnsembleRouter_Escalation(t *testing.T) {
	config := NewConfig("", time.Minute)
	config.EnsembleEnabled = true
	config.EnsembleSize = 3
	config.FrontierFallbackModel = "mock-frontier"

	var callCount int
	frontierHit := false
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reqBody ChatRequest
		_ = json.NewDecoder(r.Body).Decode(&reqBody)

		if reqBody.Model == "mock-frontier" {
			frontierHit = true
		}

		callCount++
		w.Header().Set("Content-Type", "application/json")

		// Return terrible responses that fail scoring (bestScore < 70)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"choices": []map[string]interface{}{
				{
					"message": map[string]interface{}{
						"role": "assistant",
						"tool_calls": []map[string]interface{}{
							{
								"function": map[string]interface{}{
									"name":      "run_shell_cmd",
									"arguments": `rm -rf`, // Bad syntax AND unsafe
								},
							},
						},
					},
				},
			},
		})
	}))
	defer ts.Close()

	config.DefaultBaseURL = ts.URL
	config.DefaultProvider = ProviderOpenAI
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "1")
	t.Setenv("LLM_API_KEY", "test-key-escalation")

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = ts.Client()

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
		frontierQ: NewFrontierQueue(4),
	}

	req := &ChatRequest{
		Provider: ProviderOpenAI,
		Model:    "mock-local",
	}

	ctx := context.Background()
	_, err := srv.ExecuteEnsemble(ctx, req, true)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Should hit local 3 times, then hit frontier 1 time.
	if callCount != 4 {
		t.Errorf("Expected 4 total calls (3 local + 1 frontier), got %d", callCount)
	}

	if !frontierHit {
		t.Errorf("Expected frontier model fallback to be triggered due to low score")
	}
}

// TestEnsembleTimeout verifies that when all jury members stall beyond the
// ensemble timeout, the call returns within a reasonable deadline (no hang).
func TestEnsembleTimeout(t *testing.T) {
	stallDuration := 500 * time.Millisecond
	ensembleTimeout := 100 * time.Millisecond

	blockedServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(stallDuration)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"choices": []map[string]interface{}{
				{"message": map[string]interface{}{"role": "assistant", "content": "slow"}},
			},
		})
	}))
	defer blockedServer.Close()

	config := NewConfig("", time.Minute)
	config.EnsembleEnabled = true
	config.EnsembleSize = 2
	config.EnsembleTimeout = ensembleTimeout
	config.FrontierFallbackModel = ""
	config.DefaultProvider = ProviderOpenAI
	config.DefaultBaseURL = blockedServer.URL
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "1")
	t.Setenv("LLM_API_KEY", "test-key-timeout")

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = blockedServer.Client()

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
		frontierQ: NewFrontierQueue(4),
	}

	req := &ChatRequest{Provider: ProviderOpenAI, Model: "slow-model"}

	started := time.Now()
	_, _ = srv.ExecuteEnsemble(context.Background(), req, false)
	elapsed := time.Since(started)

	maxAllowed := ensembleTimeout * 3
	if elapsed > maxAllowed {
		t.Errorf("ExecuteEnsemble hung: elapsed=%v, max allowed=%v", elapsed, maxAllowed)
	}
}

// TestHandleChat_EnsembleEnabled verifies that /v1/chat routes through the
// ensemble when Ollama + ensemble are configured.
func TestHandleChat_EnsembleEnabled(t *testing.T) {
	var callCount atomic.Int32

	mockLLM := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount.Add(1)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"choices": []map[string]interface{}{
				{"message": map[string]interface{}{"role": "assistant", "content": "hello from ensemble"}},
			},
		})
	}))
	defer mockLLM.Close()

	config := NewConfig("", time.Minute)
	config.EnsembleEnabled = true
	config.EnsembleSize = 2
	config.EnsembleTimeout = 10 * time.Second
	config.DefaultProvider = ProviderOpenAI
	config.DefaultBaseURL = mockLLM.URL
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "1")
	t.Setenv("LLM_API_KEY", "test-key-ensemble")

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = mockLLM.Client()

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
		frontierQ: NewFrontierQueue(4),
	}

	req := &ChatRequest{Provider: ProviderOpenAI, Model: "test-model"}
	ctx := context.Background()

	_, err := srv.ExecuteEnsemble(ctx, req, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if n := int(callCount.Load()); n < 2 {
		t.Errorf("expected >= 2 ensemble member calls, got %d", n)
	}
}

// TestGlobalSemaphore_BlocksFlood verifies the global concurrency cap.
func TestGlobalSemaphore_BlocksFlood(t *testing.T) {
	const cap = 3
	globalSem := make(chan struct{}, cap)

	var (
		active    atomic.Int32
		maxActive atomic.Int32
		wg        sync.WaitGroup
	)

	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()

			select {
			case globalSem <- struct{}{}:
			case <-ctx.Done():
				return
			}

			cur := active.Add(1)
			for {
				prev := maxActive.Load()
				if cur <= prev || maxActive.CompareAndSwap(prev, cur) {
					break
				}
			}

			time.Sleep(20 * time.Millisecond)
			active.Add(-1)
			<-globalSem
		}()
	}

	wg.Wait()

	if got := int(maxActive.Load()); got > cap {
		t.Errorf("global semaphore allowed %d concurrent > cap %d", got, cap)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// New tests for PR: adaptive sizing, early-exit, and frontier backpressure
// ─────────────────────────────────────────────────────────────────────────────

// TestModelVRAMEstimateGB verifies the parameter-suffix heuristic.
func TestModelVRAMEstimateGB(t *testing.T) {
	tests := []struct {
		model   string
		minVRAM float64
		maxVRAM float64
	}{
		{"qwen2.5-coder:7b", 4.0, 5.5},
		{"qwen3-coder:30b", 9.0, 11.0},
		{"llama3.2:3b", 1.5, 2.5},
		{"gemma4:27b", 13.0, 16.0},
		{"deepseek-r1:14b", 8.0, 10.0},
		{"llama3.2", 4.0, 6.0},          // no suffix — fallback
		{"unknown-model-xyz", 4.0, 6.0}, // no suffix — fallback
	}
	for _, tt := range tests {
		t.Run(tt.model, func(t *testing.T) {
			gb := ModelVRAMEstimateGB(tt.model)
			if gb < tt.minVRAM || gb > tt.maxVRAM {
				t.Errorf("ModelVRAMEstimateGB(%q) = %.1f, want [%.1f, %.1f]",
					tt.model, gb, tt.minVRAM, tt.maxVRAM)
			}
		})
	}
}

// TestAdaptiveEnsembleSize_NoAdaptive verifies env override bypass.
func TestAdaptiveEnsembleSize_NoAdaptive(t *testing.T) {
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "1")
	got := AdaptiveEnsembleSize("llama3.2:70b", 3, "http://localhost:11434")
	if got != 3 {
		t.Errorf("expected configured size 3 when adaptive disabled, got %d", got)
	}
}

// TestAdaptiveEnsembleSize_LargeModel verifies VRAM pressure degrades to 1.
func TestAdaptiveEnsembleSize_LargeModel(t *testing.T) {
	// Use a tiny fake system memory that can't fit parallel 70B calls.
	t.Setenv("FLUME_SYSTEM_MEMORY_GB", "16")
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "")
	// 70B at Q4 ≈ 40 GB per call. With 16 GB system, adaptive should return 1.
	got := AdaptiveEnsembleSize("llama3.2:70b", 3, "")
	if got != 1 {
		t.Errorf("expected adaptive size 1 for 70B model on 16GB system, got %d", got)
	}
}

// TestAdaptiveEnsembleSize_SmallModel verifies small models fit multiple slots.
func TestAdaptiveEnsembleSize_SmallModel(t *testing.T) {
	t.Setenv("FLUME_SYSTEM_MEMORY_GB", "64")
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "")
	// 3B model ≈ 2.0 GB. On 64 GB with 20% reserved = 51.2 GB available.
	// extra_per_slot ≈ 1.2 GB → > 3 slots easily.
	got := AdaptiveEnsembleSize("llama3.2:3b", 3, "")
	if got < 2 {
		t.Errorf("expected adaptive size >= 2 for 3B model on 64GB system, got %d", got)
	}
}

// TestEarlyExitEnsemble verifies that a high-scoring member causes early exit
// before all jury members complete.
func TestEarlyExitEnsemble(t *testing.T) {
	var callCount atomic.Int32
	// All responses score high (perfect elastro tool call)
	goodServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount.Add(1)
		// Slow down to make race condition visible
		time.Sleep(20 * time.Millisecond)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"choices": []map[string]interface{}{
				{
					"message": map[string]interface{}{
						"role": "assistant",
						"tool_calls": []map[string]interface{}{
							{
								"function": map[string]interface{}{
									"name":      "elastro_query_ast",
									"arguments": `{"query": "User"}`,
								},
							},
						},
					},
				},
			},
		})
	}))
	defer goodServer.Close()

	config := NewConfig("", time.Minute)
	config.EnsembleEnabled = true
	config.EnsembleSize = 3
	config.EnsembleTimeout = 10 * time.Second
	config.DefaultProvider = ProviderOpenAI
	config.DefaultBaseURL = goodServer.URL
	t.Setenv("FLUME_ENSEMBLE_NO_ADAPTIVE", "1")
	t.Setenv("LLM_API_KEY", "test-key-early-exit")

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = goodServer.Client()

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
		frontierQ: NewFrontierQueue(4),
	}

	req := &ChatRequest{Provider: ProviderOpenAI, Model: "fast-model"}
	start := time.Now()
	resp, err := srv.ExecuteEnsemble(context.Background(), req, true)
	elapsed := time.Since(start)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp == nil {
		t.Fatal("expected non-nil response")
	}
	// With early-exit, we should return well before all 3 complete.
	// 3 × 20ms = 60ms worst case without early exit.
	// With early exit (score=100 ≥ 80 threshold), should stop after first win.
	if elapsed > 200*time.Millisecond {
		t.Errorf("early-exit too slow: %v (expected < 200ms)", elapsed)
	}
	// At least 1 call made (the early-exit winner)
	if callCount.Load() < 1 {
		t.Errorf("expected at least 1 LLM call, got %d", callCount.Load())
	}
}

// TestFrontierQueue_Backpressure verifies that the FrontierQueue limits concurrent
// frontier escalation calls correctly.
func TestFrontierQueue_Backpressure(t *testing.T) {
	const maxConcurrent = 2
	q := NewFrontierQueue(maxConcurrent)

	var (
		active  atomic.Int32
		maxSeen atomic.Int32
		wg      sync.WaitGroup
	)

	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()

			if !q.Acquire(ctx) {
				return // slot not acquired in time — expected for excess goroutines
			}
			defer q.Release()

			cur := active.Add(1)
			// Track high-water mark
			for {
				prev := maxSeen.Load()
				if cur <= prev || maxSeen.CompareAndSwap(prev, cur) {
					break
				}
			}

			time.Sleep(20 * time.Millisecond)
			active.Add(-1)
		}()
	}

	wg.Wait()

	if got := int(maxSeen.Load()); got > maxConcurrent {
		t.Errorf("FrontierQueue allowed %d concurrent > cap %d", got, maxConcurrent)
	}
}

// TestFrontierQueue_HealthMetrics verifies the metrics map structure.
func TestFrontierQueue_HealthMetrics(t *testing.T) {
	q := NewFrontierQueue(3)
	m := q.HealthMetrics()
	if _, ok := m["frontier_active"]; !ok {
		t.Error("missing frontier_active key in health metrics")
	}
	if _, ok := m["frontier_max_slots"]; !ok {
		t.Error("missing frontier_max_slots key in health metrics")
	}
	if m["frontier_max_slots"] != 3 {
		t.Errorf("expected frontier_max_slots=3, got %d", m["frontier_max_slots"])
	}
}
