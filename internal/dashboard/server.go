// Package dashboard implements the Flume Dashboard REST API server.
//
// This is the Go port of Python's src/dashboard/server.py (16 nodes) and
// all 9 API sub-modules (81 nodes). Uses net/http + Go 1.22 ServeMux for
// routing with zero external dependencies.
//
// Phase 2 of the Python → Go migration plan.
// Python source: src/dashboard/server.py, src/dashboard/api/*.py
package dashboard

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/Fremen-Labs/flume/internal/es"
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	"github.com/Fremen-Labs/flume/internal/llm"
	"github.com/Fremen-Labs/flume/pkg/types"
)

// Server is the Dashboard API HTTP server.
// Derived from Python: server.py (FastAPI app instance + lifespan).
type Server struct {
	mux              *http.ServeMux
	httpServer       *http.Server
	es               *es.Client
	llmClient        *llm.Client
	logger           *slog.Logger
	cfg              *Config
	logLevel         *slog.LevelVar
	startTime        time.Time
	autonomyStatus   map[string]interface{}
	workerStatus     map[string]interface{}
	onSweepTrigger   func(sweepName string) error
	onSettingsReload func()
	mu               sync.RWMutex

	rateLimiter *rateLimiter
}

// RegisterSweepTrigger registers a callback for manual sweep triggering.
func (s *Server) RegisterSweepTrigger(cb func(sweepName string) error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.onSweepTrigger = cb
}

// RegisterSettingsReload registers a callback for reloading settings and environment variables.
func (s *Server) RegisterSettingsReload(cb func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.onSettingsReload = cb
}

// Config holds dashboard server configuration.
// Derived from Python: config.py get_settings().
type Config struct {
	Host           string
	Port           int
	ESUrl          string
	ESApiKey       string
	CORSOrigins    []string
	StaticRoot     string
	NativeMode     bool
	RateLimitPerMin int
}

// DefaultConfig returns production-safe defaults, overridden by env vars.
// Derived from Python: config.py get_settings() Pydantic model.
func DefaultConfig() *Config {
	host := envOr("DASHBOARD_HOST", "0.0.0.0")
	port := envInt("DASHBOARD_PORT", 8765)
	esURL := envOr("ES_URL", "http://localhost:9200")
	if envOr("FLUME_NATIVE_MODE", "0") != "1" && os.Getenv("ES_URL") == "" {
		esURL = "http://elasticsearch:9200"
	}
	corsRaw := envOr("FLUME_CORS_ORIGINS", "")
	var cors []string
	if corsRaw != "" {
		for _, o := range strings.Split(corsRaw, ",") {
			o = strings.TrimSpace(o)
			if o != "" {
				cors = append(cors, o)
			}
		}
	} else {
		cors = []string{"http://localhost:8080", "http://localhost:8765", "http://127.0.0.1:8080"}
	}

	return &Config{
		Host:           host,
		Port:           port,
		ESUrl:          esURL,
		ESApiKey:       envOr("ES_API_KEY", ""),
		CORSOrigins:    cors,
		StaticRoot:     envOr("FLUME_STATIC_ROOT", ""),
		NativeMode:     envOr("FLUME_NATIVE_MODE", "0") == "1",
		RateLimitPerMin: envInt("FLUME_RATE_LIMIT", 2000),
	}
}

// New creates a new Dashboard server.
func New(cfg *Config, logger *slog.Logger) *Server {
	if logger == nil {
		logger = slog.Default()
	}
	esClient := es.New(cfg.ESUrl, cfg.ESApiKey, logger)
	s := &Server{
		mux:         http.NewServeMux(),
		es:          esClient,
		llmClient:   llm.New(logger),
		logger:      logger,
		cfg:         cfg,
		startTime:   time.Now(),
		rateLimiter: newRateLimiter(cfg.RateLimitPerMin),
	}
	// Phase 0: Wire reasoning bridge for any Go-side Log* calls that reach the dashboard
	// (primarily benefits future admin/recovery paths and consistency with worker).
	flumelogger.SetESBridge(esClient)

	s.registerRoutes()
	return s
}

// ListenAndServe starts the HTTP server.
func (s *Server) ListenAndServe() error {
	addr := fmt.Sprintf("%s:%d", s.cfg.Host, s.cfg.Port)
	s.httpServer = &http.Server{
		Addr:         addr,
		Handler:      s.withMiddleware(s.mux),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}
	s.logger.Info("Dashboard API starting",
		slog.String("addr", addr),
		slog.Bool("native_mode", s.cfg.NativeMode),
	)
	return s.httpServer.ListenAndServe()
}

// Shutdown gracefully stops the server.
func (s *Server) Shutdown(ctx context.Context) error {
	s.logger.Info("Dashboard API shutting down")
	return s.httpServer.Shutdown(ctx)
}

// ─── Route Registration ─────────────────────────────────────────────────────

// registerRoutes wires all API endpoints to their handlers.
// Each route maps directly to a Python @router.get/post/put/delete decorator.
func (s *Server) registerRoutes() {
	// Health + System (api/system.py — 21 nodes)
	s.mux.HandleFunc("GET /api/health", s.handleHealth)
	s.mux.HandleFunc("GET /api/snapshot", s.handleSnapshot)
	s.mux.HandleFunc("GET /api/system-state", s.handleSystemState)
	s.mux.HandleFunc("GET /api/telemetry", s.handleTelemetry)
	s.mux.HandleFunc("GET /api/logs", s.handleLogs)
	s.mux.HandleFunc("POST /api/logs/structured", s.handleStructuredLog) // wired for new frontend @/lib/logger transport + Logloom enrichment
	s.mux.HandleFunc("GET /ws/telemetry", s.handleWebSocketTelemetry)
	s.mux.HandleFunc("GET /api/exo-status", s.handleExoStatus)
	s.mux.HandleFunc("GET /api/autonomy/status", s.handleAutonomyStatus)
	s.mux.HandleFunc("POST /api/autonomy/sweep/{sweep_name}", s.handleAutonomySweep)
	s.mux.HandleFunc("GET /api/auto-unblock/status", s.handleAutoUnblockStatus)
	s.mux.HandleFunc("POST /api/auto-unblock/sweep", s.handleAutoUnblockSweep)

	// Tasks (api/tasks.py — 14 nodes)
	s.mux.HandleFunc("GET /api/tasks/{task_id}/history", s.handleTaskHistory)
	s.mux.HandleFunc("GET /api/tasks/{task_id}/diff", s.handleTaskDiff)
	s.mux.HandleFunc("GET /api/tasks/{task_id}/thoughts", s.handleTaskThoughts)
	s.mux.HandleFunc("GET /api/tasks/{task_id}/commits", s.handleTaskCommits)
	s.mux.HandleFunc("POST /api/tasks/{task_id}/transition", s.handleTaskTransition)
	s.mux.HandleFunc("POST /api/tasks/bulk-requeue", s.handleTasksBulkRequeue)
	s.mux.HandleFunc("POST /api/tasks/bulk-update", s.handleTasksBulkUpdate)
	s.mux.HandleFunc("POST /api/tasks/claim", s.handleTaskClaim)
	s.mux.HandleFunc("POST /api/tasks/complete", s.handleTaskComplete)

	// Security (api/security.py — 9 nodes)
	s.mux.HandleFunc("GET /api/security", s.handleSecurity)
	s.mux.HandleFunc("GET /api/vault/status", s.handleVaultStatus)
	s.mux.HandleFunc("POST /api/tasks/stop-all", s.handleTasksStopAll)
	s.mux.HandleFunc("POST /api/tasks/resume-all", s.handleTasksResumeAll)

	// Projects (api/projects.py — 6 nodes)
	s.mux.HandleFunc("POST /api/projects", s.handleProjectCreate)
	s.mux.HandleFunc("GET /api/projects/{project_id}/clone-status", s.handleProjectCloneStatus)
	s.mux.HandleFunc("GET /api/projects/{project_id}/tasks", s.handleProjectTasks)
	s.mux.HandleFunc("POST /api/projects/{project_id}/delete", s.handleProjectDelete)

	// Repos (api/repos.py — 3 nodes)
	s.mux.HandleFunc("GET /api/repos/{project_id}/branches", s.handleRepoBranches)
	s.mux.HandleFunc("GET /api/repos/{project_id}/tree", s.handleRepoTree)
	s.mux.HandleFunc("GET /api/repos/{project_id}/file", s.handleRepoFile)
	s.mux.HandleFunc("GET /api/repos/{project_id}/diff", s.handleRepoDiff)

	// Nodes (api/nodes.py — 18 nodes)
	s.mux.HandleFunc("GET /api/nodes", s.handleNodesList)
	s.mux.HandleFunc("POST /api/nodes", s.handleNodesAdd)
	s.mux.HandleFunc("DELETE /api/nodes/{node_id}", s.handleNodesDelete)
	s.mux.HandleFunc("POST /api/nodes/{node_id}/test", s.handleNodesTest)
	s.mux.HandleFunc("GET /api/routing-policy", s.handleRoutingPolicyGet)
	s.mux.HandleFunc("PUT /api/routing-policy", s.handleRoutingPolicyPut)
	s.mux.HandleFunc("GET /api/frontier-models", s.handleFrontierModels)
	s.mux.HandleFunc("GET /api/nodes/utilization", s.handleNodesUtilization)

	// Settings (api/settings.py — 3 nodes)
	s.mux.HandleFunc("POST /api/settings/log-level", s.handleSettingsLogLevel)
	s.mux.HandleFunc("POST /api/logs/client", s.handleClientLogs)
	s.mux.HandleFunc("GET /api/settings/llm", s.handleSettingsLLMGet)
	s.mux.HandleFunc("POST /api/settings/llm", s.handleSettingsLLMUpdate)
	s.mux.HandleFunc("PUT /api/settings/llm/credentials", s.handleSettingsLLMCredentialsPut)
	s.mux.HandleFunc("POST /api/settings/llm/credentials", s.handleSettingsLLMCredentialsPost)
	s.mux.HandleFunc("POST /api/settings/llm/oauth/refresh", s.handleSettingsLLMOAuthRefresh)
	s.mux.HandleFunc("GET /api/settings/repos", s.handleSettingsReposGet)
	s.mux.HandleFunc("PUT /api/settings/repos", s.handleSettingsReposUpdate)
	s.mux.HandleFunc("GET /api/settings/system", s.handleSettingsSystemGet)
	s.mux.HandleFunc("PUT /api/settings/system", s.handleSettingsSystemUpdate)
	s.mux.HandleFunc("GET /api/settings/agent-models", s.handleSettingsAgentModelsGet)
	s.mux.HandleFunc("PUT /api/settings/agent-models", s.handleSettingsAgentModelsUpdate)
	s.mux.HandleFunc("POST /api/settings/agent-models", s.handleSettingsAgentModelsUpdate)
	s.mux.HandleFunc("POST /api/settings/restart-services", s.handleSettingsRestartServices)

	// Intake (api/intake.py — 4 nodes)
	s.mux.HandleFunc("POST /api/intake/session", s.handleIntakeStartSession)
	s.mux.HandleFunc("GET /api/intake/session/{session_id}", s.handleIntakeGetSession)
	s.mux.HandleFunc("POST /api/intake/session/{session_id}/message", s.handleIntakeMessage)
	s.mux.HandleFunc("POST /api/intake/session/{session_id}/commit", s.handleIntakeCommit)

	// Workflow (api/workflow.py — 3 nodes)
	s.mux.HandleFunc("GET /api/workflow/workers", s.handleWorkflowWorkers)
	s.mux.HandleFunc("GET /api/workflow/agents/status", s.handleWorkflowAgentsStatus)
	s.mux.HandleFunc("POST /api/workflow/agents/start", s.handleWorkflowAgentsStart)
	s.mux.HandleFunc("POST /api/workflow/agents/stop", s.handleWorkflowAgentsStop)

	// ─── SPA Static File Serving ─────────────────────────────────────────
	// In Docker mode, FLUME_STATIC_ROOT points to the pre-built Vue SPA.
	// Serves static assets and falls back to index.html for client-side routing.
	if s.cfg.StaticRoot != "" {
		s.logger.Info("SPA static serving enabled", slog.String("root", s.cfg.StaticRoot))
		s.mux.Handle("/", s.spaHandler(s.cfg.StaticRoot))
	}
}

// ─── Middleware ──────────────────────────────────────────────────────────────

// withMiddleware wraps the mux with CORS, logging, and request ID middleware.
// Derived from Python: CORSMiddleware + LoggingMiddleware in server.py.
func (s *Server) withMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()

		// CORS headers
		origin := r.Header.Get("Origin")
		if s.isAllowedOrigin(origin) {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Request-ID, X-Flume-System-Token")
			w.Header().Set("Access-Control-Allow-Credentials", "true")
		}

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}

		// Request ID
		reqID := r.Header.Get("X-Request-ID")
		if reqID == "" {
			reqID = fmt.Sprintf("%d", time.Now().UnixNano())
		}
		w.Header().Set("X-Request-ID", reqID)

		// Rate limiting (Medium priority implementation)
		// Internal/private IPs (Docker, Kubernetes pods, service mesh, localhost)
		// are exempted. This protects external clients while ensuring internal
		// worker <-> dashboard communication is never throttled.
		if s.rateLimiter != nil {
			ip := r.Header.Get("X-Forwarded-For")
			if ip == "" {
				ip = r.RemoteAddr
			}
			// Take first IP if comma-separated (original client)
			if idx := strings.Index(ip, ","); idx > 0 {
				ip = strings.TrimSpace(ip[:idx])
			}

			if !isInternalIP(ip) {
				if !s.rateLimiter.allow(ip) {
					s.logger.Warn("rate limit exceeded", slog.String("ip", ip), slog.String("path", r.URL.Path))
					writeError(w, http.StatusTooManyRequests, "rate limit exceeded")
					return
				}
			} else {
				// Optional: debug log for visibility into internal traffic patterns
				s.logger.Debug("bypassing rate limit for internal IP",
					slog.String("ip", ip),
					slog.String("path", r.URL.Path))
			}
		}

		// Bypass statusWriter wrapping for WebSockets to allow http.Hijacker
		if r.Header.Get("Upgrade") == "websocket" || strings.HasPrefix(r.URL.Path, "/ws") {
			next.ServeHTTP(w, r)
			return
		}

		// Wrap response writer to capture status
		rw := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(rw, r)

		// Structured request logging
		duration := time.Since(start)
		isNoisy := strings.Contains(r.URL.Path, "/health") || strings.Contains(r.URL.Path, "/tasks")
		logFn := s.logger.Info
		if isNoisy {
			logFn = s.logger.Debug
		}
		logFn(fmt.Sprintf("%s %s - %d", r.Method, r.URL.Path, rw.status),
			slog.String("method", r.Method),
			slog.String("path", r.URL.Path),
			slog.Int("status", rw.status),
			slog.Float64("duration_ms", float64(duration.Microseconds())/1000),
			slog.String("request_id", reqID),
		)
	})
}

func (s *Server) isAllowedOrigin(origin string) bool {
	if origin == "" {
		return false
	}
	for _, o := range s.cfg.CORSOrigins {
		if o == origin || o == "*" {
			return true
		}
	}
	return false
}

// statusWriter wraps http.ResponseWriter to capture the response status.
type statusWriter struct {
	http.ResponseWriter
	status int
	written bool
}

func (w *statusWriter) WriteHeader(status int) {
	if !w.written {
		w.status = status
		w.written = true
	}
	w.ResponseWriter.WriteHeader(status)
}

// ─── Response Helpers ───────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(data); err != nil {
		// Log at package level if possible; this is a last-resort after headers sent
		slog.Default().Error("failed to encode JSON response", slog.String("error", err.Error()))
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// writeErrorWithLog logs the error at the appropriate level before responding.
func writeErrorWithLog(w http.ResponseWriter, status int, msg string, logger *slog.Logger) {
	if logger != nil {
		if status >= 500 {
			logger.Error("request error", slog.Int("status", status), slog.String("error", msg))
		} else {
			logger.Warn("request error", slog.Int("status", status), slog.String("error", msg))
		}
	}
	writeJSON(w, status, map[string]string{"error": msg})
}

func decodeBody(r *http.Request, dst interface{}) error {
	defer r.Body.Close()
	if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	return nil
}

// ─── Env Helpers ────────────────────────────────────────────────────────────

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	var n int
	if _, err := fmt.Sscanf(v, "%d", &n); err != nil {
		return fallback
	}
	return n
}

// ─── SPA Handler ────────────────────────────────────────────────────────────

// spaHandler serves a Single-Page Application from the filesystem.
// Static assets are served directly; all other paths receive index.html
// so the client-side router (React Router) handles navigation.
func (s *Server) spaHandler(root string) http.Handler {
	fileServer := http.FileServer(http.Dir(root))

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Attempt to serve the file directly (JS, CSS, images, fonts, etc.)
		path := filepath.Join(root, filepath.Clean(r.URL.Path))
		info, err := os.Stat(path)
		if err == nil && !info.IsDir() {
			fileServer.ServeHTTP(w, r)
			return
		}

		// For all other paths, serve index.html (SPA client-side routing)
		http.ServeFile(w, r, filepath.Join(root, "index.html"))
	})
}


func timeNowUnixMilli() int64 {
	return time.Now().UnixMilli()
}

// withTimeout derives a child context with a reasonable deadline for dashboard operations.
// This addresses reliable-go-systems context discipline for ES/LLM/git calls originating from APIs.
func (s *Server) withTimeout(parent context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	if d <= 0 {
		d = 15 * time.Second
	}
	return context.WithTimeout(parent, d)
}

// ─── Simple Rate Limiter (Medium priority) ──────────────────────────────────

type rateLimiter struct {
	mu       sync.Mutex
	visitors map[string]*visitor
	limit    int
	window   time.Duration
}

type visitor struct {
	tokens    int
	lastSeen  time.Time
}

func newRateLimiter(limitPerMin int) *rateLimiter {
	if limitPerMin <= 0 {
		limitPerMin = 2000 // sane default from config
	}
	rl := &rateLimiter{
		visitors: make(map[string]*visitor),
		limit:    limitPerMin,
		window:   time.Minute,
	}
	// Background cleanup goroutine
	go rl.cleanup()
	return rl
}

func (rl *rateLimiter) allow(ip string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()

	v, ok := rl.visitors[ip]
	now := time.Now()

	if !ok || now.Sub(v.lastSeen) > rl.window {
		rl.visitors[ip] = &visitor{tokens: rl.limit - 1, lastSeen: now}
		return true
	}

	if v.tokens > 0 {
		v.tokens--
		v.lastSeen = now
		return true
	}
	return false
}

func (rl *rateLimiter) cleanup() {
	for {
		time.Sleep(rl.window)
		rl.mu.Lock()
		for ip, v := range rl.visitors {
			if time.Since(v.lastSeen) > rl.window*2 {
				delete(rl.visitors, ip)
			}
		}
		rl.mu.Unlock()
	}
}

// isInternalIP returns true for private, loopback, and link-local addresses.
// This is used to exempt internal Docker/Kubernetes/service-to-service traffic
// from rate limiting (per reliable-go-systems principle of not breaking internal reliability).
func isInternalIP(ipStr string) bool {
	// Strip port if present (e.g. "10.0.0.5:12345")
	if host, _, err := net.SplitHostPort(ipStr); err == nil {
		ipStr = host
	}

	ip := net.ParseIP(ipStr)
	if ip == nil {
		return false
	}

	// IsPrivate() covers RFC 1918 ranges + some others (Go 1.17+)
	// We also explicitly check loopback and link-local for robustness in container environments.
	return ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast()
}

// logReasoning is the standardized way to emit both regular structured logs
// and rich LogAgentReasoning (for UI popouts + Logloom graphs).
// This implements the flume-go requirement for consistent observability on
// important decisions and state changes.
func (s *Server) logReasoning(ctx context.Context, taskOrSystemID, role, message string, meta map[string]any) {
	fields := []any{
		slog.String("role", role),
		slog.String("id", taskOrSystemID),
	}
	for k, v := range meta {
		fields = append(fields, slog.Any(k, v))
	}

	s.logger.Info(message, fields...)
	flumelogger.LogAgentReasoning(ctx, taskOrSystemID, role, message, meta)
}

// logDecision is a lighter variant for non-task events (e.g. config changes, node operations).
func (s *Server) logDecision(ctx context.Context, component, action string, meta map[string]any) {
	fields := []any{
		slog.String("component", component),
		slog.String("action", action),
	}
	for k, v := range meta {
		fields = append(fields, slog.Any(k, v))
	}
	s.logger.Info("decision", fields...)

	// Also emit reasoning for auditability when component is system-level
	if component == "system" || component == "config" || component == "workflow" {
		flumelogger.LogAgentReasoning(ctx, "system", component, action, meta)
	}
}

// ─── Type aliases for request bodies ────────────────────────────────────────

// TaskTransitionRequest is the request body for POST /api/tasks/{id}/transition.
// Derived from Python: api/models.py TaskTransitionRequest(BaseModel).
type TaskTransitionRequest struct {
	Status              string `json:"status"`
	Instruction         string `json:"instruction,omitempty"`
	AutoRecoveryPrompt  *bool  `json:"auto_recovery_prompt,omitempty"`
	// Phase 1+ recovery support: allow ops/manual transitions (e.g. review -> blocked under LLM outage)
	// without requiring full terminal evidence. When set, AuditReason is recorded in agent_log + execution_thoughts.
	ForceAudit  bool   `json:"force_audit,omitempty"`
	AuditReason string `json:"audit_reason,omitempty"`
}

// BulkRequeueRequest is the request body for POST /api/tasks/bulk-requeue.
type BulkRequeueRequest struct {
	TaskIDs []string `json:"task_ids"`
}

// BulkUpdateRequest is the request body for POST /api/tasks/bulk-update.
type BulkUpdateRequest struct {
	IDs    []string `json:"ids"`
	Action string   `json:"action"`
	Repo   string   `json:"repo,omitempty"`
}

// Compile-time check that types are referenced.
var _ = types.TaskStatusReady
var _ = utf8.RuneLen
