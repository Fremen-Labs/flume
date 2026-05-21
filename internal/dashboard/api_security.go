// api_security.go — Security audit, vault status, kill switch (stop/resume-all).
//
// Direct port of Python: src/dashboard/api/security.py (9 AST nodes).
package dashboard

import (
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
)

// ─── GET /api/security ──────────────────────────────────────────────────────

// handleSecurity returns a security audit dashboard.
// Derived from Python: api/security.py api_security().
func (s *Server) handleSecurity(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// Gather security metadata
	vaultAddr := envOr("VAULT_ADDR", "")
	vaultHealthy := false
	if vaultAddr != "" {
		resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/v1/sys/health", vaultAddr))
		if err == nil && resp != nil {
			vaultHealthy = true
		}
	}

	// Check CORS configuration
	corsOrigins := strings.Join(s.cfg.CORSOrigins, ", ")

	// Check admin token presence
	hasAdminToken := os.Getenv("FLUME_ADMIN_TOKEN") != ""

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"vault": map[string]interface{}{
			"configured": vaultAddr != "",
			"healthy":    vaultHealthy,
			"address":    vaultAddr,
		},
		"cors": map[string]interface{}{
			"origins": corsOrigins,
		},
		"admin": map[string]interface{}{
			"token_configured": hasAdminToken,
		},
		"tls": map[string]interface{}{
			"es_verify": os.Getenv("ES_VERIFY") != "0",
		},
	})
}

// ─── GET /api/vault/status ──────────────────────────────────────────────────

// handleVaultStatus returns OpenBao/Vault health status.
// Derived from Python: api/security.py vault_status().
func (s *Server) handleVaultStatus(w http.ResponseWriter, r *http.Request) {
	vaultAddr := envOr("VAULT_ADDR", "")
	if vaultAddr == "" {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"configured": false,
			"status":     "not configured",
		})
		return
	}

	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/v1/sys/health", vaultAddr))
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"configured": true,
			"status":     "unreachable",
			"error":      err.Error(),
		})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"configured": true,
		"status":     "healthy",
		"data":       resp,
	})
}

// ─── POST /api/tasks/stop-all ───────────────────────────────────────────────

// handleTasksStopAll activates the kill switch — pauses all agent work.
// Derived from Python: api/security.py api_tasks_stop_all().
// Requires admin access (FLUME_ADMIN_TOKEN header validation).
func (s *Server) handleTasksStopAll(w http.ResponseWriter, r *http.Request) {
	if !s.verifyAdminAccess(r) {
		writeError(w, http.StatusForbidden, "admin access required")
		return
	}

	ctx := r.Context()
	now := nowISO()

	// Set kill switch in ES
	if err := s.es.Post(ctx, "flume-system-settings/_update/kill-switch", map[string]interface{}{
		"doc":           map[string]interface{}{"active": true, "activated_at": now},
		"doc_as_upsert": true,
	}); err != nil {
		s.logger.Error("stop-all: kill switch failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to activate kill switch")
		return
	}

	s.logger.Warn("kill switch activated — all agents paused")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":      true,
		"message":      "Kill switch activated — all agents paused",
		"activated_at": now,
	})
}

// ─── POST /api/tasks/resume-all ─────────────────────────────────────────────

// handleTasksResumeAll deactivates the kill switch — resumes agents.
// Derived from Python: api/security.py api_tasks_resume_all().
func (s *Server) handleTasksResumeAll(w http.ResponseWriter, r *http.Request) {
	if !s.verifyAdminAccess(r) {
		writeError(w, http.StatusForbidden, "admin access required")
		return
	}

	ctx := r.Context()
	now := nowISO()

	if err := s.es.Post(ctx, "flume-system-settings/_update/kill-switch", map[string]interface{}{
		"doc": map[string]interface{}{"active": false, "deactivated_at": now},
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to deactivate kill switch")
		return
	}

	s.logger.Info("kill switch deactivated — agents resuming")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":        true,
		"message":        "Kill switch deactivated — agents resuming",
		"deactivated_at": now,
	})
}

// ─── Admin Access Verification ──────────────────────────────────────────────

// verifyAdminAccess checks the FLUME_ADMIN_TOKEN header.
// Derived from Python: api/security.py verify_admin_access().
func (s *Server) verifyAdminAccess(r *http.Request) bool {
	token := os.Getenv("FLUME_ADMIN_TOKEN")
	if token == "" {
		// No token configured → admin access is open (dev mode)
		return true
	}

	headerToken := r.Header.Get("X-Flume-Admin-Token")
	if headerToken == "" {
		headerToken = r.Header.Get("Authorization")
		if strings.HasPrefix(headerToken, "Bearer ") {
			headerToken = strings.TrimPrefix(headerToken, "Bearer ")
		}
	}

	return headerToken == token
}
