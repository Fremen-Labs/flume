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
	// frontierProber periodically polls active cloud models for token limits.
	frontierProber *FrontierProber
	// multiRouter coordinates smart routing across the node mesh.
	multiRouter    *MultiNodeRouter
	// nodeSems provides per-node concurrency semaphores for the distributed ensemble.
	nodeSems       *NodeSemaphoreMap
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

	taskType := agentRoleToTaskType(req.AgentRole)
	isComplexTask := taskType == "planning" || taskType == "pm" || taskType == "reasoning"

	if provider == ProviderOllama && s.config.EnsembleEnabled && s.config.EnsembleSize > 1 && isComplexTask {
		// Complex tasks get distributed Ensembles across the Node Mesh!
		resp, err = s.ExecuteEnsemble(ctx, &req, false)
	} else if s.multiRouter != nil && s.nodeRegistry != nil {
		// Standard tasks get Smart Routing across the mesh and frontier models
		resp, err = s.multiRouter.ExecuteSmartRoute(ctx, &req, taskType, false)
	} else if provider == ProviderOllama && s.config.EnsembleEnabled && s.config.EnsembleSize > 1 {
		// Legacy single-node Ensemble fallback
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
		
		errStr := err.Error()
		statusCode := http.StatusBadGateway
		if strings.Contains(errStr, "HTTP 402") || strings.Contains(errStr, "credit_exhausted") || strings.Contains(errStr, "insufficient_quota") {
			statusCode = http.StatusPaymentRequired
		} else if strings.Contains(errStr, "HTTP 429") {
			statusCode = http.StatusTooManyRequests
		} else if strings.Contains(errStr, "HTTP 400") {
			statusCode = http.StatusBadRequest
		}

		s.writeError(w, statusCode, errStr, requestID)
		return
	}

	Metrics.RecordRequest(string(provider), true, time.Since(start))

	log.Info("chat completed",
		slog.Int("content_len", len(resp.Message.Content)),
		slog.Float64("duration_ms", msElapsed(start)),
	)

	workerName := r.Header.Get("X-Worker-Name")
	if workerName != "" {
		Metrics.RecordWorkerTokensBatch(workerName, resp.Usage.PromptTokens, resp.Usage.CompletionTokens)
	}

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

	taskType := agentRoleToTaskType(req.AgentRole)
	isComplexTask := taskType == "planning" || taskType == "pm" || taskType == "reasoning"

	if provider == ProviderOllama && s.config.EnsembleEnabled && s.config.EnsembleSize > 1 && isComplexTask {
		resp, err = s.ExecuteEnsemble(ctx, &req, true)
	} else if s.multiRouter != nil && s.nodeRegistry != nil {
		resp, err = s.multiRouter.ExecuteSmartRoute(ctx, &req, taskType, true)
	} else if provider == ProviderOllama && s.config.EnsembleEnabled && s.config.EnsembleSize > 1 {
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
		
		errStr := err.Error()
		statusCode := http.StatusBadGateway
		if strings.Contains(errStr, "HTTP 402") || strings.Contains(errStr, "credit_exhausted") || strings.Contains(errStr, "insufficient_quota") {
			statusCode = http.StatusPaymentRequired
		} else if strings.Contains(errStr, "HTTP 429") {
			statusCode = http.StatusTooManyRequests
		} else if strings.Contains(errStr, "HTTP 400") {
			statusCode = http.StatusBadRequest
		}

		s.writeError(w, statusCode, errStr, requestID)
		return
	}

	Metrics.RecordRequest(string(provider), true, time.Since(start))

	// Apply guardrails: deduplicate, filter invalid tool calls
	SanitizeToolResponse(resp)

	log.Info("tool-call completed",
		slog.Int("content_len", len(resp.Message.Content)),
		slog.Int("tools_used", len(resp.Message.ToolCalls)),
		slog.Float64("duration_ms", msElapsed(start)),
	)

	workerName := r.Header.Get("X-Worker-Name")
	if workerName != "" {
		Metrics.RecordWorkerTokensBatch(workerName, resp.Usage.PromptTokens, resp.Usage.CompletionTokens)
	}

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
		log.Warn("flume-agent-models index verification failed — index should be pre-created by `flume start`",
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

	// Start active frontier telemetry prober.
	server.frontierProber = NewFrontierProber(config, secrets, server.router)
	server.frontierProber.Start(ctx)

	// Initialize per-node semaphore map.
	server.nodeSems = NewNodeSemaphoreMap()

	// Create multi-node router.
	server.multiRouter = NewMultiNodeRouter(server.router, server.nodeRegistry, config)

	// Register node mesh API endpoints.
	server.mux.HandleFunc("GET /api/nodes", server.handleGetNodes)
	server.mux.HandleFunc("POST /api/nodes", server.handleAddNode)
	server.mux.HandleFunc("POST /api/nodes/{id}/test", server.handleTestNode)
	server.mux.HandleFunc("DELETE /api/nodes/{id}", server.handleDeleteNode)

	// Routing Policy API endpoints.
	server.mux.HandleFunc("GET /api/routing-policy", server.handleGetRoutingPolicy)
	server.mux.HandleFunc("PUT /api/routing-policy", server.handlePutRoutingPolicy)
	server.mux.HandleFunc("GET /api/frontier-models", server.handleGetFrontierModels)

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

// agentRoleToTaskType maps ChatRequest.AgentRole to the MultiNodeRouter's
// task-type taxonomy for capability-based node selection.
//
//	pm, planner → "reasoning"  (high reasoning score → primary node)
//	implementer → "code"       (moderate reasoning → primary or secondary)
//	tester, reviewer → "evaluation"  (lightweight → can use secondary nodes)
//	other/empty → "generic"
func agentRoleToTaskType(role string) string {
	switch strings.ToLower(strings.TrimSpace(role)) {
	case "pm", "planner", "intake":
		return "reasoning"
	case "implementer":
		return "code"
	case "tester", "reviewer", "critic":
		return "evaluation"
	default:
		return "generic"
	}
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

// handleTestNode probes an Ollama node endpoint and returns connectivity + discovered models.
func (s *Server) handleTestNode(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())

	nodeID := r.PathValue("id")
	if nodeID == "" || !isValidNodeID(nodeID) {
		http.Error(w, `{"error":"invalid or missing node ID"}`, http.StatusBadRequest)
		return
	}

	node := s.nodeRegistry.GetNode(nodeID)
	if node == nil {
		http.Error(w, `{"error":"node not found"}`, http.StatusNotFound)
		return
	}

	log.Info("node_api: testing connection",
		slog.String("node_id", nodeID),
		slog.String("host", node.Host),
	)

	hc := NewHealthChecker(s.nodeRegistry)
	baseURL := fmt.Sprintf("http://%s", node.Host)

	// Probe /api/tags for model discovery.
	start := time.Now()
	tagsResult, err := hc.probeTags(r.Context(), baseURL, node)
	latencyMs := time.Since(start).Milliseconds()

	result := map[string]interface{}{
		"node_id":    nodeID,
		"host":       node.Host,
		"latency_ms": latencyMs,
	}

	if err != nil {
		log.Warn("node_api: connection test failed",
			slog.String("node_id", nodeID),
			slog.String("error", err.Error()),
		)
		result["reachable"] = false
		result["models"] = []string{}
		result["current_load"] = 0.0
		result["error"] = err.Error()
	} else {
		load, _, _ := hc.probeLoad(r.Context(), baseURL, node)
		log.Info("node_api: connection test succeeded",
			slog.String("node_id", nodeID),
			slog.Int64("latency_ms", latencyMs),
			slog.Int("models_found", len(tagsResult.models)),
			slog.Float64("load", load),
		)
		result["reachable"] = true
		result["models"] = tagsResult.models
		result["current_load"] = load
		result["error"] = nil
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
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

	// SSRF restrictions: block cloud metadata endpoints but ALLOW localhost/loopback
	// so users running Flume + Ollama on the same machine can register local nodes.
	if strings.Contains(lowerHost, "metadata.google.internal") || strings.Contains(lowerHost, "169.254.169.254") {
		return false
	}

	return true
}

// ─────────────────────────────────────────────────────────────────────────────
// Routing Policy API Handlers
// ─────────────────────────────────────────────────────────────────────────────

// handleGetRoutingPolicy returns the current routing policy as JSON.
// GET /api/routing-policy
func (s *Server) handleGetRoutingPolicy(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())
	log.Info("api: GET /api/routing-policy")

	policy := s.config.GetRoutingPolicy()

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(policy); err != nil {
		log.Error("api: failed to encode routing policy",
			slog.String("error", err.Error()),
		)
		http.Error(w, `{"error":"encoding failed"}`, http.StatusInternalServerError)
	}
}

// handlePutRoutingPolicy validates and persists a new routing policy to ES.
// PUT /api/routing-policy
func (s *Server) handlePutRoutingPolicy(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())
	log.Info("api: PUT /api/routing-policy")

	var incoming RoutingPolicy
	if err := json.NewDecoder(r.Body).Decode(&incoming); err != nil {
		log.Warn("api: invalid routing policy payload",
			slog.String("error", err.Error()),
		)
		http.Error(w, fmt.Sprintf(`{"error":"invalid JSON: %s"}`, err.Error()), http.StatusBadRequest)
		return
	}

	// Validate
	if err := incoming.Validate(); err != nil {
		log.Warn("api: routing policy validation failed",
			slog.String("error", err.Error()),
		)
		http.Error(w, fmt.Sprintf(`{"error":"validation: %s"}`, err.Error()), http.StatusBadRequest)
		return
	}

	// Normalize weights
	incoming.normalizeWeights()

	// Persist to ES
	esURL := s.config.esURL
	if err := incoming.PersistToES(r.Context(), esURL, s.config.httpClient); err != nil {
		log.Error("api: failed to persist routing policy to ES",
			slog.String("error", err.Error()),
		)
		http.Error(w, fmt.Sprintf(`{"error":"persist failed: %s"}`, err.Error()), http.StatusInternalServerError)
		return
	}

	// Force-refresh config so the new policy takes effect immediately
	s.config.mu.Lock()
	s.config.RoutingPolicy = &incoming
	s.config.mu.Unlock()

	log.Info("api: routing policy updated",
		slog.String("mode", string(incoming.Mode)),
		slog.Int("frontier_models", len(incoming.FrontierMix)),
	)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// FrontierProviderCatalogResponse is the JSON shape returned by GET /api/frontier-models.
type FrontierProviderCatalogResponse struct {
	Providers []FrontierProviderCatalogEntry `json:"providers"`
}

// FrontierProviderCatalogEntry represents a single provider in the catalog.
type FrontierProviderCatalogEntry struct {
	ID          string                 `json:"id"`
	Label       string                 `json:"label"`
	Models      []string               `json:"models"`
	Credentials []CredentialPublicInfo  `json:"credentials"`
}

// CredentialPublicInfo is the public (non-secret) info about a credential.
type CredentialPublicInfo struct {
	ID     string `json:"id"`
	Label  string `json:"label"`
	HasKey bool   `json:"has_key"`
}

// handleGetFrontierModels returns the frontier model catalog merged with
// configured credentials to show which providers have active API keys.
// GET /api/frontier-models
func (s *Server) handleGetFrontierModels(w http.ResponseWriter, r *http.Request) {
	log := WithContext(r.Context())
	log.Info("api: GET /api/frontier-models")

	s.config.mu.RLock()
	credentials := s.config.Credentials
	s.config.mu.RUnlock()

	var providers []FrontierProviderCatalogEntry
	for providerID, models := range FrontierModelCatalog {
		label := FrontierProviderLabels[providerID]
		if label == "" {
			label = providerID
		}

		// Find credentials for this provider
		var creds []CredentialPublicInfo
		for _, cred := range credentials {
			if strings.EqualFold(cred.Provider, providerID) {
				creds = append(creds, CredentialPublicInfo{
					ID:     cred.ID,
					Label:  cred.Label,
					HasKey: cred.HasKey,
				})
			}
		}

		providers = append(providers, FrontierProviderCatalogEntry{
			ID:          providerID,
			Label:       label,
			Models:      models,
			Credentials: creds,
		})
	}

	resp := FrontierProviderCatalogResponse{Providers: providers}
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		log.Error("api: failed to encode frontier models",
			slog.String("error", err.Error()),
		)
	}
}
