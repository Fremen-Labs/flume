package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/src/gateway/skills"
	"github.com/Fremen-Labs/flume/src/gateway/skillslog"
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
	// skills is the registry of Inception Skill handlers.
	skills     *skills.SkillRegistry
	// nodeRegistry manages the distributed Ollama node mesh.
	nodeRegistry   *NodeRegistry
	// healthChecker probes node health in the background.
	healthChecker  *HealthChecker
	// multiRouter coordinates smart routing across the node mesh.
	multiRouter    *MultiNodeRouter
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
	s.mux.HandleFunc("POST /internal/level", s.handleLogLevel)
	s.mux.HandleFunc("GET /metrics", s.handleMetrics)

	// Inception Skills endpoints
	s.mux.HandleFunc("POST /skills/execute/", skills.HandleSkillExecute(s.skills))
	s.mux.HandleFunc("GET /skills", skills.HandleSkillsList(s.skills))
	s.mux.HandleFunc("POST /skills/reload", skills.HandleSkillsReload(s.skills))

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

	// ── Input validation: normalise and reject structurally invalid fields
	// before any config resolution, secret lookup, or provider dispatch.
	if err := ValidateChatRequest(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "request validation failed: "+err.Error(), requestID)
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

	// Track active model for metrics
	metricModel := req.Model
	if !s.config.IsKnownModel(metricModel) {
		metricModel = "unknown"
	}
	Metrics.SetActiveModel(metricModel)

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
		Metrics.RecordRequest(string(provider), false, time.Since(start))
		s.writeError(w, http.StatusBadGateway, err.Error(), requestID)
		return
	}

	Metrics.RecordRequest(string(provider), true, time.Since(start))

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

	// ── Input validation: normalise and reject structurally invalid fields
	// before any config resolution, secret lookup, or provider dispatch.
	if err := ValidateChatRequest(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "request validation failed: "+err.Error(), requestID)
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

	// Track active model for metrics
	Metrics.SetActiveModel(req.Model)
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
		Metrics.RecordRequest(string(provider), false, time.Since(start))
		s.writeError(w, http.StatusBadGateway, err.Error(), requestID)
		return
	}

	Metrics.RecordRequest(string(provider), true, time.Since(start))

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

// handleMetrics gates the Prometheus metrics endpoint dynamically based on config.
func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if !s.config.IsPrometheusEnabled() {
		http.NotFound(w, r)
		return
	}
	HandleMetrics()(w, r)
}

func (s *Server) handleLogLevel(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Level string `json:"level"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid body", "")
		return
	}
	SetLogLevel(body.Level)
	s.writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "new_level": body.Level})
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

	// Inject the secure gateway logger into the skills bridge
	skillslog.SetLogger(log)

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

	// ── Distributed Node Mesh initialization ────────────────────────────
	esURL := os.Getenv("ES_URL")
	if esURL == "" {
		esURL = "http://elasticsearch:9200"
	}
	server.nodeRegistry = NewNodeRegistry(esURL)

	// Ensure the node registry ES index exists.
	if err := server.nodeRegistry.EnsureIndex(ctx); err != nil {
		log.Warn("failed to ensure node-registry index",
			slog.String("error", err.Error()),
		)
	}

	// Load nodes from ES.
	server.nodeRegistry.RefreshFromES(ctx)
	nodeCount := server.nodeRegistry.Count()
	log.Info("node mesh initialized",
		slog.Int("registered_nodes", nodeCount),
	)

	// Start background health checker.
	server.healthChecker = NewHealthChecker(server.nodeRegistry)
	server.healthChecker.Start(ctx)

	// Create multi-node router.
	server.multiRouter = NewMultiNodeRouter(server.router, server.nodeRegistry, config)

	// Register node mesh API endpoints.
	server.mux.HandleFunc("GET /api/nodes", server.handleGetNodes)
	server.mux.HandleFunc("POST /api/nodes", server.handleAddNode)
	server.mux.HandleFunc("DELETE /api/nodes/{id}", server.handleDeleteNode)

	// Initialize Inception Skill Registry
	server.skills = skills.NewSkillRegistry()
	if err := server.skills.LoadAll(ctx); err != nil {
		log.Warn("skill registry initialization failed (non-fatal)",
			slog.String("error", err.Error()),
		)
	} else {
		log.Info("inception skill registry initialized",
			slog.Int("skills_loaded", server.skills.Count()),
		)
	}

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

// ─────────────────────────────────────────────────────────────────────────────
// Node Mesh API Handlers
// ─────────────────────────────────────────────────────────────────────────────

// handleGetNodes returns the list of all registered nodes (AuthToken redacted).
func (s *Server) handleGetNodes(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())
	log.Debug("handling GET /api/nodes")

	nodes := s.nodeRegistry.AllNodes()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"nodes": nodes,
		"count": len(nodes),
	})
}

// handleAddNode registers a new Ollama node in the mesh.
func (s *Server) handleAddNode(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())

	var node Node
	if err := json.NewDecoder(r.Body).Decode(&node); err != nil {
		log.Warn("node_api: invalid JSON body",
			slog.String("error", err.Error()),
		)
		http.Error(w, `{"error":"invalid JSON body"}`, http.StatusBadRequest)
		return
	}

	// Input validation: node ID format
	if !isValidNodeID(node.ID) {
		log.Warn("node_api: invalid node ID",
			slog.String("node_id", node.ID),
		)
		http.Error(w, `{"error":"node ID must match ^[a-z0-9-]+$ and be 1-64 chars"}`, http.StatusBadRequest)
		return
	}

	// Input validation: host SSRF prevention
	if !isValidNodeHost(node.Host) {
		log.Warn("node_api: invalid host format or unsafe target",
			slog.String("host", node.Host),
		)
		http.Error(w, `{"error":"host must be a valid hostname or IP:port and cannot point to local/internal services"}`, http.StatusBadRequest)
		return
	}

	// Default health state for new nodes.
	node.Health = NodeHealth{
		Status:   NodeStatusOffline,
		LastSeen: time.Now(),
	}

	if err := s.nodeRegistry.UpsertNodeToES(r.Context(), &node); err != nil {
		log.Error("node_api: failed to persist node",
			slog.String("node_id", node.ID),
			slog.String("error", err.Error()),
		)
		http.Error(w, `{"error":"failed to persist node"}`, http.StatusInternalServerError)
		return
	}

	log.Info("node_api: node registered",
		slog.String("node_id", node.ID),
		slog.String("host", node.Host),
		slog.String("model", node.ModelTag),
	)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]string{"status": "created", "id": node.ID})
}

// handleDeleteNode removes a node from the mesh.
func (s *Server) handleDeleteNode(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())

	// Extract node ID from URL path natively in Go 1.22+
	nodeID := r.PathValue("id")

	if nodeID == "" || !isValidNodeID(nodeID) {
		http.Error(w, `{"error":"invalid or missing node ID"}`, http.StatusBadRequest)
		return
	}

	if err := s.nodeRegistry.DeleteNodeFromES(r.Context(), nodeID); err != nil {
		log.Error("node_api: failed to delete node",
			slog.String("node_id", nodeID),
			slog.String("error", err.Error()),
		)
		http.Error(w, `{"error":"failed to delete node"}`, http.StatusInternalServerError)
		return
	}

	log.Info("node_api: node deleted",
		slog.String("node_id", nodeID),
	)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "deleted", "id": nodeID})
}

// isValidNodeID validates node IDs against ^[a-z0-9\-]+$ (1-64 chars).
func isValidNodeID(id string) bool {
	if len(id) == 0 || len(id) > 64 {
		return false
	}
	for _, c := range id {
		if !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-') {
			return false
		}
	}
	return true
}

// isValidNodeHost ensures the host is a valid IP:port or hostname:port format,
// blocking path traversal characters and common SSRF target domains/IPs.
func isValidNodeHost(host string) bool {
	if host == "" || len(host) > 255 {
		return false
	}

	h, portStr, err := net.SplitHostPort(host)
	if err != nil {
		return false // must contain a port
	}

	var port int
	if _, err := fmt.Sscanf(portStr, "%d", &port); err != nil || port < 1 || port > 65535 {
		return false
	}

	lowerHost := strings.ToLower(h)
	
	// Pre-filter outright path traversal or spaces
	if strings.ContainsAny(lowerHost, "/\\?#& ") {
		return false
	}

	// Validate allowed characters: alphanumeric, hyphens, and dots
	for _, c := range lowerHost {
		if !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '.') {
			return false
		}
	}

	// Basic SSRF restrictions against internal loopback or cloud metadata services
	if lowerHost == "localhost" || lowerHost == "127.0.0.1" || lowerHost == "::1" ||
		strings.Contains(lowerHost, "metadata.google.internal") || strings.Contains(lowerHost, "169.254.169.254") {
		return false
	}

	return true
}
