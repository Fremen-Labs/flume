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

	"github.com/Fremen-Labs/flume/internal/secrets"
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

// ─── GET /api/security/validate ─────────────────────────────────────────────

// handleSecurityValidate checks if the provided admin token (via Authorization or X-Flume-Admin-Token)
// is valid against the server's FLUME_ADMIN_TOKEN env. Safe no-op call for UI "test credentials".
// Returns 200 {"valid": true} on success or 401 on failure.
func (s *Server) handleSecurityValidate(w http.ResponseWriter, r *http.Request) {
	if !s.verifyAdminAccess(r) {
		writeError(w, http.StatusUnauthorized, "invalid admin token")
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"valid": true,
	})
}

// ─── POST /api/security/secrets/reveal ──────────────────────────────────────

// handleSecuritySecretsReveal retrieves the plaintext value of a secret key from OpenBao.
// Request body: {"key": "..."}
// Requires admin access (verifyAdminAccess).
func (s *Server) handleSecuritySecretsReveal(w http.ResponseWriter, r *http.Request) {
	if !s.verifyAdminAccess(r) {
		writeError(w, http.StatusForbidden, "admin access required")
		return
	}

	var req struct {
		Key string `json:"key"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	key := strings.TrimSpace(req.Key)
	if key == "" {
		writeError(w, http.StatusBadRequest, "key is required")
		return
	}

	ctx := r.Context()
	vaultAddr := envOr("OPENBAO_ADDR", envOr("VAULT_ADDR", ""))
	vaultToken := envOr("OPENBAO_TOKEN", envOr("VAULT_TOKEN", ""))

	if vaultAddr == "" || vaultToken == "" {
		writeError(w, http.StatusInternalServerError, "OpenBao/Vault is not configured or offline")
		return
	}

	// Fetch current keys from secret/data/flume/keys
	client := secrets.NewOpenBaoClient(vaultAddr, vaultToken, s.logger)
	data, err := client.KVGet(ctx, "flume/keys")
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("failed to fetch from Vault: %v", err))
		return
	}

	if data == nil {
		writeError(w, http.StatusNotFound, "secret key not found")
		return
	}

	val, exists := data[key]
	if !exists {
		writeError(w, http.StatusNotFound, "secret key not found")
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"key":   key,
		"value": fmt.Sprintf("%v", val),
	})
}

// ─── POST /api/security/secrets/update ──────────────────────────────────────

// handleSecuritySecretsUpdate adds or edits a secret key-value pair in OpenBao.
// Request body: {"key": "...", "value": "..."}
// Requires admin access (verifyAdminAccess).
func (s *Server) handleSecuritySecretsUpdate(w http.ResponseWriter, r *http.Request) {
	if !s.verifyAdminAccess(r) {
		writeError(w, http.StatusForbidden, "admin access required")
		return
	}

	var req struct {
		Key   string `json:"key"`
		Value string `json:"value"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	key := strings.TrimSpace(req.Key)
	val := strings.TrimSpace(req.Value)
	if key == "" || val == "" {
		writeError(w, http.StatusBadRequest, "key and value are required")
		return
	}

	ctx := r.Context()
	vaultAddr := envOr("OPENBAO_ADDR", envOr("VAULT_ADDR", ""))
	vaultToken := envOr("OPENBAO_TOKEN", envOr("VAULT_TOKEN", ""))

	if vaultAddr == "" || vaultToken == "" {
		writeError(w, http.StatusInternalServerError, "OpenBao/Vault is not configured or offline")
		return
	}

	client := secrets.NewOpenBaoClient(vaultAddr, vaultToken, s.logger)
	
	// Fetch existing keys first to merge them (KV-V2 overwrites everything at the path unless merged)
	data, err := client.KVGet(ctx, "flume/keys")
	if err != nil {
		data = make(map[string]interface{})
	}
	if data == nil {
		data = make(map[string]interface{})
	}

	data[key] = val

	if err := client.KVPut(ctx, "flume/keys", data); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("failed to save to Vault: %v", err))
		return
	}

	s.logger.Info("secret updated in OpenBao", slog.String("key", key))
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
	})
}

// ─── POST /api/security/secrets/delete ──────────────────────────────────────

// handleSecuritySecretsDelete removes a secret key from OpenBao.
// Request body: {"key": "..."}
// Requires admin access (verifyAdminAccess).
func (s *Server) handleSecuritySecretsDelete(w http.ResponseWriter, r *http.Request) {
	if !s.verifyAdminAccess(r) {
		writeError(w, http.StatusForbidden, "admin access required")
		return
	}

	var req struct {
		Key string `json:"key"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	key := strings.TrimSpace(req.Key)
	if key == "" {
		writeError(w, http.StatusBadRequest, "key is required")
		return
	}

	ctx := r.Context()
	vaultAddr := envOr("OPENBAO_ADDR", envOr("VAULT_ADDR", ""))
	vaultToken := envOr("OPENBAO_TOKEN", envOr("VAULT_TOKEN", ""))

	if vaultAddr == "" || vaultToken == "" {
		writeError(w, http.StatusInternalServerError, "OpenBao/Vault is not configured or offline")
		return
	}

	client := secrets.NewOpenBaoClient(vaultAddr, vaultToken, s.logger)
	
	// Fetch existing keys to check/delete
	data, err := client.KVGet(ctx, "flume/keys")
	if err != nil || data == nil {
		writeError(w, http.StatusNotFound, "no secrets found")
		return
	}

	if _, exists := data[key]; !exists {
		writeError(w, http.StatusNotFound, "secret key not found")
		return
	}

	delete(data, key)

	if err := client.KVPut(ctx, "flume/keys", data); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("failed to update Vault: %v", err))
		return
	}

	s.logger.Info("secret deleted from OpenBao", slog.String("key", key))
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
	})
}

