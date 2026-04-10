package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// HTTP Server — three endpoints:
//   POST /v1/chat        — text completion (replaces llm_client.chat)
//   POST /v1/chat/tools  — tool-calling    (replaces llm_client.chat_with_tools)
//   GET  /health         — Docker healthcheck
// ─────────────────────────────────────────────────────────────────────────────

// maxBodyBytes caps the request body to prevent slow-body / large-body floods
// from holding HTTP server goroutines before the provider semaphore even runs.
const maxBodyBytes = 2 << 20 // 2 MiB

// Server is the gateway HTTP server.
type Server struct {
	router    *ProviderRouter
	config    *Config
	mux       *http.ServeMux
	ollamaSem *OllamaSemaphore
	// globalSem is a provider-agnostic gate applied before JSON decode.
	// It prevents floods of non-Ollama requests from overwhelming the gateway
	// before any per-provider semaphore has a chance to protect anything.
	globalSem  chan struct{}
	// frontierQ gates concurrent cloud LLM escalation calls from the ensemble
	// to prevent rate-limit cascades and unexpected cost spikes.
	frontierQ  *FrontierQueue
}

// globalMaxConcurrent is the total cross-provider cap. Override via
// FLUME_GATEWAY_MAX_CONCURRENT; default = 32.
func globalMaxConcurrent() int {
	if v := os.Getenv("FLUME_GATEWAY_MAX_CONCURRENT"); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil && n > 0 {
			return n
		}
	}
	return 32
}

// NewServer creates a fully wired gateway server.
func NewServer(config *Config, secrets *SecretStore) *Server {
	router := NewProviderRouter(config, secrets)
	// Detect Ollama capacity and create adaptive semaphore
	ollamaURL := config.GetOllamaBaseURL()
	maxConcurrent := DetectOllamaCapacity(ollamaURL)
	s := &Server{
		router:    router,
		config:    config,
		mux:       http.NewServeMux(),
		ollamaSem:  NewOllamaSemaphore(maxConcurrent),
		globalSem:  make(chan struct{}, globalMaxConcurrent()),
		frontierQ:  NewFrontierQueue(FrontierMaxConcurrentFromEnv()),
	}
	s.mux.HandleFunc("POST /v1/chat", s.handleChat)
	s.mux.HandleFunc("POST /v1/chat/tools", s.handleChatTools)
	s.mux.HandleFunc("GET /health", s.handleHealth)
	return s
}

// ServeHTTP implements http.Handler.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

// ListenAndServe starts the HTTP server on the given address.
func (s *Server) ListenAndServe(addr string) error {
	srv := &http.Server{
		Addr:         addr,
		Handler:      s,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 300 * time.Second, // long writes for streaming responses
		IdleTimeout:  120 * time.Second,
	}
	Log().Info("flume-gateway listening",
		slog.String("addr", addr),
	)
	return srv.ListenAndServe()
}

// ─────────────────────────────────────────────────────────────────────────────
// Handlers
// ─────────────────────────────────────────────────────────────────────────────

// acquireGlobal blocks until the global concurrency slot is available or ctx is
// cancelled. Returns false if the context expired while waiting.
func (s *Server) acquireGlobal(ctx context.Context) bool {
	select {
	case s.globalSem <- struct{}{}:
		return true
	case <-ctx.Done():
		return false
	}
}

func (s *Server) releaseGlobal() { <-s.globalSem }

func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	requestID := shortID()
	start := time.Now()

	// ── Fix 3: global gate applied before JSON decode ──────────────────────
	if !s.acquireGlobal(r.Context()) {
		s.writeError(w, http.StatusServiceUnavailable, "gateway at capacity", requestID)
		return
	}
	defer s.releaseGlobal()

	// Cap body size to prevent slow-body attacks.
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)

	var req ChatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid request body: "+err.Error(), requestID)
		return
	}

	log := RequestLogger(requestID, req.Provider, req.Model, req.AgentRole)
	ctx := ContextWithLogger(r.Context(), log)

	// ── Fix 4: refresh is now singleflight-guarded inside Refresh() ───────
	s.config.Refresh(ctx)

	log.Info("incoming chat request",
		slog.Int("messages", len(req.Messages)),
	)

	// ── Fix 1: resolve model/provider before choosing code path ───────────
	_, provider, _ := s.config.ResolveModel(&req)

	// ── Fix 1 continued: acquire Ollama slot for /chat too ────────────────
	if provider == ProviderOllama {
		log.Info("awaiting ollama slot",
			slog.Int("active", s.ollamaSem.ActiveSlots()),
			slog.Int("max", s.ollamaSem.MaxSlots()),
		)
		if !s.ollamaSem.Acquire(ctx) {
			s.writeError(w, http.StatusServiceUnavailable, "request cancelled while waiting for ollama slot", requestID)
			return
		}
		defer s.ollamaSem.Release()
	}

	var resp *ChatResponse
	var err error

	// ── Fix 1 continued: /chat also benefits from ensemble when enabled ────
	// withTools=false keeps text-only semantics in each jury member call.
	if provider == ProviderOllama && s.config.EnsembleEnabled && s.config.EnsembleSize > 1 {
		resp, err = s.ExecuteEnsemble(ctx, &req, false)
	} else {
		resp, err = s.router.Route(ctx, &req, false)
	}

	if err != nil {
		log.Error("chat failed",
			slog.String("error", err.Error()),
			slog.Float64("duration_ms", msElapsed(start)),
		)
		s.writeError(w, http.StatusBadGateway, err.Error(), requestID)
		return
	}

	log.Info("chat completed",
		slog.Int("content_len", len(resp.Message.Content)),
		slog.Float64("duration_ms", msElapsed(start)),
	)

	s.writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleChatTools(w http.ResponseWriter, r *http.Request) {
	requestID := shortID()
	start := time.Now()

	// ── Fix 3: global gate applied before JSON decode ──────────────────────
	if !s.acquireGlobal(r.Context()) {
		s.writeError(w, http.StatusServiceUnavailable, "gateway at capacity", requestID)
		return
	}
	defer s.releaseGlobal()

	// Cap body size to prevent slow-body attacks.
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)

	var req ChatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid request body: "+err.Error(), requestID)
		return
	}

	log := RequestLogger(requestID, req.Provider, req.Model, req.AgentRole)
	ctx := ContextWithLogger(r.Context(), log)

	s.config.Refresh(ctx)

	log.Info("incoming tool-call request",
		slog.Int("messages", len(req.Messages)),
		slog.Int("tools", len(req.Tools)),
	)

	// Acquire Ollama concurrency slot (blocks until available)
	// This prevents invisible queue buildup in Ollama's serial inference engine.
	model, provider, _ := s.config.ResolveModel(&req)
	if provider == ProviderOllama {
		log.Info("awaiting ollama slot",
			slog.Int("active", s.ollamaSem.ActiveSlots()),
			slog.Int("max", s.ollamaSem.MaxSlots()),
			slog.String("model", model),
		)
		if !s.ollamaSem.Acquire(ctx) {
			s.writeError(w, http.StatusServiceUnavailable, "request cancelled while waiting for ollama slot", requestID)
			return
		}
		defer s.ollamaSem.Release()
	}

	var resp *ChatResponse
	var err error

	if provider == ProviderOllama && s.config.EnsembleEnabled && s.config.EnsembleSize > 1 {
		resp, err = s.ExecuteEnsemble(ctx, &req, true)
	} else {
		resp, err = s.router.Route(ctx, &req, true)
	}

	if err != nil {
		log.Error("tool-call failed",
			slog.String("error", err.Error()),
			slog.Float64("duration_ms", msElapsed(start)),
		)
		s.writeError(w, http.StatusBadGateway, err.Error(), requestID)
		return
	}

	// Apply guardrails: deduplicate, filter invalid tool calls
	SanitizeToolResponse(resp)

	log.Info("tool-call completed",
		slog.Int("content_len", len(resp.Message.Content)),
		slog.Int("tool_calls", len(resp.Message.ToolCalls)),
		slog.Float64("duration_ms", msElapsed(start)),
	)

	s.writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	metrics := map[string]interface{}{
		"status":  "ok",
		"service": "flume-gateway",
		"ollama": map[string]int{
			"active_slots": s.ollamaSem.ActiveSlots(),
			"max_slots":    s.ollamaSem.MaxSlots(),
		},
		"frontier": s.frontierQ.HealthMetrics(),
		"global": map[string]int{
			"active": len(s.globalSem),
			"max":    cap(s.globalSem),
		},
	}
	s.writeJSON(w, http.StatusOK, metrics)
}

// ─────────────────────────────────────────────────────────────────────────────
// Response helpers
// ─────────────────────────────────────────────────────────────────────────────

func (s *Server) writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		Log().Error("failed to write response", slog.String("error", err.Error()))
	}
}

func (s *Server) writeError(w http.ResponseWriter, status int, message, requestID string) {
	s.writeJSON(w, status, map[string]string{
		"error":      message,
		"request_id": requestID,
	})
}

// ─────────────────────────────────────────────────────────────────────────────
// Startup
// ─────────────────────────────────────────────────────────────────────────────

// StartGateway is the top-level entry point called from main.go.
func StartGateway(addr string) error {
	InitLogger()
	log := Log()
	log.Info("initializing flume-gateway", slog.String("version", "1.0.0"))

	config := NewConfig("", 5*time.Second)
	secrets := NewSecretStore("", "", "", 60*time.Second)

	ctx := ContextWithLogger(context.Background(), log)

	// Pre-warm config from ES
	config.Refresh(ctx)

	// Hydrate global secrets from OpenBao on startup
	secrets.GetGlobalSecrets(ctx)

	// Ensure agent-models index exists
	if err := config.EnsureAgentModelsIndex(ctx); err != nil {
		log.Warn("failed to ensure agent-models index",
			slog.String("error", err.Error()),
		)
	}

	server := NewServer(config, secrets)
	return server.ListenAndServe(addr)
}

// DefaultAddr returns the gateway listen address from env or default.
func DefaultAddr() string {
	port := os.Getenv("GATEWAY_PORT")
	if port == "" {
		port = "8090"
	}
	return ":" + port
}

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

func msElapsed(start time.Time) float64 {
	return float64(time.Since(start).Microseconds()) / 1000.0
}

// shortID generates a short request ID without external deps.
func shortID() string {
	return fmt.Sprintf("%08x", time.Now().UnixNano()&0xFFFFFFFF)
}
