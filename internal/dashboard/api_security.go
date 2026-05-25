// api_security.go — Security audit, vault status, kill switch (stop/resume-all).
//
// Direct port of Python: src/dashboard/api/security.py (9 AST nodes).
package dashboard

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// ─── GET /api/security ──────────────────────────────────────────────────────

// handleSecurity returns a security audit dashboard.
// Derived from Python: api/security.py api_security().
func (s *Server) handleSecurity(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	vaultAddr := envOr("OPENBAO_ADDR", envOr("VAULT_ADDR", ""))
	vaultToken := envOr("OPENBAO_TOKEN", envOr("VAULT_TOKEN", ""))

	vaultActive := false
	openbaoKeys := map[string]string{}

	if vaultAddr != "" {
		client := &http.Client{Timeout: 5 * time.Second}

		// 1. Check health
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/v1/sys/health", vaultAddr), nil)
		if err == nil {
			resp, err := client.Do(req)
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode == 200 || resp.StatusCode == 429 {
					vaultActive = true
				}
			}
		}

		// 2. Fetch keys from secret/data/flume/keys
		if vaultToken != "" {
			req2, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/v1/secret/data/flume/keys", vaultAddr), nil)
			if err == nil {
				req2.Header.Set("X-Vault-Token", vaultToken)
				resp2, err := client.Do(req2)
				if err == nil {
					defer resp2.Body.Close()
					if resp2.StatusCode == 200 {
						var result struct {
							Data struct {
								Data map[string]interface{} `json:"data"`
							} `json:"data"`
						}
						if err := json.NewDecoder(resp2.Body).Decode(&result); err == nil {
							for k := range result.Data.Data {
								openbaoKeys[k] = "secured"
							}
						}
					}
				}
			}
		}
	}

	// Fallback to defaults if empty and vault active (just in case)
	if vaultActive && len(openbaoKeys) == 0 {
		openbaoKeys["ES_API_KEY"] = "secured"
		openbaoKeys["OPENAI_API_KEY"] = "secured"
	}

	// 3. Fetch audit logs from agent-security-audits index
	var auditLogs []interface{}
	resp, err := s.es.SearchRaw(ctx, "agent-security-audits", map[string]interface{}{
		"size": 15,
		"sort": []interface{}{
			map[string]interface{}{"@timestamp": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"match_all": map[string]interface{}{},
		},
	})
	if err == nil && resp != nil {
		hits, _ := resp["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				timestamp := src["@timestamp"]
				if timestamp == nil {
					timestamp = nowISO()
				}
				message := src["message"]
				if message == nil {
					message = "OpenBao KV securely accessed"
				}
				agentRoles := src["agent_roles"]
				if agentRoles == nil {
					agentRoles = "System"
				}
				workerName := src["worker_name"]
				if workerName == nil {
					workerName = "Orchestrator"
				}
				secretPath := src["secret_path"]
				if secretPath == nil {
					secretPath = "secret/data/flume/keys"
				}
				keysRetrieved := src["keys_retrieved"]
				if keysRetrieved == nil {
					keysRetrieved = []string{}
				}
				auditLogs = append(auditLogs, map[string]interface{}{
					"@timestamp":     timestamp,
					"message":        message,
					"agent_roles":    agentRoles,
					"worker_name":    workerName,
					"secret_path":    secretPath,
					"keys_retrieved": keysRetrieved,
				})
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"vault_active": vaultActive,
		"openbao_keys": openbaoKeys,
		"audit_logs":   auditLogs,
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
