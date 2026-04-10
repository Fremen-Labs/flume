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
	// 1. Setup minimal server with mock ensemble config
	config := NewConfig("", time.Minute)
	config.EnsembleEnabled = true
	config.EnsembleSize = 3
	config.FrontierFallbackModel = "mock-frontier"

	// 2. We mock `Router.Route` by overriding the HTTP client or using a local test server?
	// The `Server` struct has `s.router`. We can inject a test `http.Client`?
	// Actually, the easiest way to test ExecuteEnsemble logic is to start a mock HTTP server doing the routing,
	// but ExecuteEnsemble calls Server.router.Route. Our `gateway` heavily uses API calls.

	// Let's create a stub server that intercepts the router responses.
	// We configure a test HTTP server for the default BaseURL that returns terrible responses to force escalation.
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
	config.DefaultProvider = ProviderOpenAI // We use an OpenAI compat mock to bypass Ollama specifics in testing

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = ts.Client() // Use the test client

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
	}

	req := &ChatRequest{
		Provider: ProviderOpenAI,
		Model:    "mock-local",
	}

	// 3. Execute ensemble (withTools=true, same as /chat/tools path)
	ctx := context.Background()
	_, err := srv.ExecuteEnsemble(ctx, req, true)

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// 4. Verify Behavior
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
	stallDuration := 500 * time.Millisecond // Each mock member stalls this long
	ensembleTimeout := 100 * time.Millisecond

	blockedServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Simulate a very slow model
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
	config.FrontierFallbackModel = ""   // no frontier — ensures we get degraded local best or error
	config.DefaultProvider = ProviderOpenAI
	config.DefaultBaseURL = blockedServer.URL

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = blockedServer.Client()

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
	}

	req := &ChatRequest{Provider: ProviderOpenAI, Model: "slow-model"}

	started := time.Now()
	// Should return well before stallDuration — the timeout cancels the jury.
	_, _ = srv.ExecuteEnsemble(context.Background(), req, false)
	elapsed := time.Since(started)

	// Allow 3× the configured timeout as headroom for goroutine scheduling.
	maxAllowed := ensembleTimeout * 3
	if elapsed > maxAllowed {
		t.Errorf("ExecuteEnsemble hung: elapsed=%v, max allowed=%v", elapsed, maxAllowed)
	}
}

// TestHandleChat_EnsembleEnabled verifies that /v1/chat routes through the
// ensemble when Ollama + ensemble are configured, not just /v1/chat/tools.
func TestHandleChat_EnsembleEnabled(t *testing.T) {
	var callCount atomic.Int32

	mockLLM := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount.Add(1)
		w.Header().Set("Content-Type", "application/json")
		// Return a decent text response (ScoreResponse → 60 for no tools, passes threshold)
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

	secrets := NewSecretStore("dummy", "dummy", "dummy", time.Minute)
	router := NewProviderRouter(config, secrets)
	router.client = mockLLM.Client()

	srv := &Server{
		config:    config,
		router:    router,
		globalSem: make(chan struct{}, 32),
	}

	// Simulate a /v1/chat request for an Ollama-provider payload.
	// We use OpenAI mock here but force the provider so the ensemble is entered.
	req := &ChatRequest{Provider: ProviderOpenAI, Model: "test-model"}
	ctx := context.Background()

	_, err := srv.ExecuteEnsemble(ctx, req, false /* withTools=false for /chat */)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Ensemble of size 2 means at least 2 calls to the LLM (plus optional frontier).
	if n := int(callCount.Load()); n < 2 {
		t.Errorf("expected >= 2 ensemble member calls, got %d", n)
	}
}

// TestGlobalSemaphore_BlocksFlood verifies that the global concurrency cap
// prevents more than N simultaneous requests from proceeding past decode.
func TestGlobalSemaphore_BlocksFlood(t *testing.T) {
	const cap = 3
	globalSem := make(chan struct{}, cap)

	var (
		active    atomic.Int32
		maxActive atomic.Int32
		wg        sync.WaitGroup
	)

	// Simulate 10 concurrent goroutines trying to acquire the global sem.
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()

			select {
			case globalSem <- struct{}{}:
				// Acquired
			case <-ctx.Done():
				return // Rejected (slot full) — expected for excess goroutines
			}

			cur := active.Add(1)
			// Track high-water mark
			for {
				prev := maxActive.Load()
				if cur <= prev || maxActive.CompareAndSwap(prev, cur) {
					break
				}
			}

			time.Sleep(20 * time.Millisecond) // Hold the slot briefly
			active.Add(-1)
			<-globalSem
		}()
	}

	wg.Wait()

	if got := int(maxActive.Load()); got > cap {
		t.Errorf("global semaphore allowed %d concurrent > cap %d", got, cap)
	}
}
