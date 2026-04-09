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

// Server is the gateway HTTP server.
type Server struct {
	router    *ProviderRouter
	config    *Config
	mux       *http.ServeMux
	ollamaSem *OllamaSemaphore
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
		ollamaSem: NewOllamaSemaphore(maxConcurrent),
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

func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	requestID := shortID()
	start := time.Now()

	var req ChatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid request body: "+err.Error(), requestID)
		return
	}

	log := RequestLogger(requestID, req.Provider, req.Model, req.AgentRole)
	ctx := ContextWithLogger(r.Context(), log)

	// Refresh config from ES (cached, sub-ms if fresh)
	s.config.Refresh(ctx)

	log.Info("incoming chat request",
		slog.Int("messages", len(req.Messages)),
	)

	resp, err := s.router.Route(ctx, &req, false)
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
		resp, err = s.ExecuteEnsemble(ctx, &req)
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
	s.writeJSON(w, http.StatusOK, map[string]string{
		"status":  "ok",
		"service": "flume-gateway",
	})
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
