// api_workflow.go — Worker lifecycle: list, status, start, stop.
//
// Direct port of Python: src/dashboard/api/workflow.py (3 AST nodes).
// Process management from: src/dashboard/core/process_manager.py (13 nodes).
package dashboard

import (
	"fmt"
	"log/slog"
	"net/http"

	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
)

// ─── GET /api/workflow/workers ───────────────────────────────────────────────

// handleWorkflowWorkers returns the list of worker processes.
// Derived from Python: api/workflow.py api_workflow_workers().
func (s *Server) handleWorkflowWorkers(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	result, err := s.es.SearchRaw(ctx, "agent-system-workers", map[string]interface{}{
		"size": 100,
		"sort": []interface{}{
			map[string]interface{}{"updated_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
	})

	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"workers": []interface{}{}})
		return
	}

	hits, _ := result["hits"].(map[string]interface{})
	hitsArr, _ := hits["hits"].([]interface{})
	var workers []interface{}
	for _, h := range hitsArr {
		hit, _ := h.(map[string]interface{})
		src, _ := hit["_source"].(map[string]interface{})
		if src != nil {
			nodeWorkers, _ := src["workers"].([]interface{})
			workers = append(workers, nodeWorkers...)
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"workers": orSliceIface(workers),
	})
}

// ─── GET /api/workflow/agents/status ────────────────────────────────────────

// handleWorkflowAgentsStatus returns agent swarm status.
// Derived from Python: api/workflow.py api_workflow_agents_status().
func (s *Server) handleWorkflowAgentsStatus(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	status := s.workerStatus
	s.mu.RUnlock()

	if status == nil {
		status = map[string]interface{}{
			"running": false,
			"pids":    []interface{}{},
		}
	}

	writeJSON(w, http.StatusOK, status)
}

// ─── POST /api/workflow/agents/start ────────────────────────────────────────

// handleWorkflowAgentsStart starts the worker agent swarm.
// Derived from Python: api/workflow.py api_workflow_agents_start().
func (s *Server) handleWorkflowAgentsStart(w http.ResponseWriter, r *http.Request) {
	// In the Go architecture, workers run as goroutines within the same process.
	// This replaces Python's subprocess.Popen() worker spawning.
	s.mu.Lock()
	s.workerStatus = map[string]interface{}{
		"running":    true,
		"started_at": nowISO(),
	}
	s.mu.Unlock()

	repo := r.URL.Query().Get("repo")
	if repo != "" {
		// Resume a specific project (clears the emergency pause so work can flow again)
		now := nowISO()
		update := map[string]interface{}{
			"work_paused":  false,
			"pause_reason": "",
			"paused_at":    "",
			"updated_at":   now,
		}
		_ = s.es.UpdateDoc(r.Context(), "flume-projects", repo, update)
		s.logger.Info("project work resumed (WorkPaused cleared)", slog.String("repo", repo))
	}

	s.logger.Info("worker agents started")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"message": "agents started",
		"resumed_repo": repo,
	})
}

// ─── POST /api/workflow/agents/stop ─────────────────────────────────────────

// handleWorkflowAgentsStop stops the worker agent swarm **and** sets the real
// project-level WorkPaused flag that all claimer / runner / spawner paths now respect.
// This is the production "halt the swarm" / emergency brake the user needed when
// a documentation task exploded past 258 items and the old in-memory flag did nothing.
//
// Query/body param "repo" (optional): if supplied, only that project is paused.
// If omitted, we attempt to pause all known projects (best-effort) so the swarm truly stops generating work.
func (s *Server) handleWorkflowAgentsStop(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.workerStatus = map[string]interface{}{
		"running":    false,
		"stopped_at": nowISO(),
	}
	s.mu.Unlock()

	repo := r.URL.Query().Get("repo")
	if repo == "" {
		// Try body as well
		var body struct {
			Repo string `json:"repo"`
		}
		_ = decodeBody(r, &body) // ignore error; repo may legitimately be empty
		repo = body.Repo
	}

	pausedCount := 0
	now := nowISO()

	if repo != "" {
		// Pause only the requested project (the common case when one plan is exploding)
		update := map[string]interface{}{
			"work_paused":  true,
			"pause_reason": "manual emergency stop via /api/workflow/agents/stop",
			"paused_at":    now,
			"updated_at":   now,
		}
		if err := s.es.UpdateDoc(r.Context(), "flume-projects", repo, update); err == nil {
			pausedCount = 1
			s.logger.Warn("EMERGENCY HALT: project paused (no new work will be claimed or spawned)",
				slog.String("repo", repo))

			// Rich reasoning for the critical "halt the swarm" action (flume-go requirement)
			flumelogger.LogAgentReasoning(r.Context(), "system", "dashboard",
				fmt.Sprintf("EMERGENCY HALT: WorkPaused set on project %s via /agents/stop", repo),
				map[string]any{"repo": repo, "action": "agents_stop", "scope": "single"})
		}
	} else {
		// Global halt: best-effort pause every project (for the "the whole swarm is out of control" case)
		// In practice users should pass ?repo=... when they know which plan exploded.
		projRes, _ := s.es.SearchRaw(r.Context(), "flume-projects", map[string]interface{}{"size": 200})
		if projRes != nil {
			if hits, ok := projRes["hits"].(map[string]interface{}); ok {
				if arr, ok := hits["hits"].([]interface{}); ok {
					for _, h := range arr {
						if hit, ok := h.(map[string]interface{}); ok {
							if id, ok := hit["_id"].(string); ok {
								update := map[string]interface{}{
									"work_paused":  true,
									"pause_reason": "global emergency stop via /api/workflow/agents/stop (no repo specified)",
									"paused_at":    now,
									"updated_at":   now,
								}
								if s.es.UpdateDoc(r.Context(), "flume-projects", id, update) == nil {
									pausedCount++
								}
							}
						}
					}
				}
			}
		}
		s.logger.Warn("EMERGENCY GLOBAL HALT: attempted to pause all projects", slog.Int("paused", pausedCount))

		// Rich reasoning for global halt (critical for diagnosing swarm explosions)
		flumelogger.LogAgentReasoning(r.Context(), "system", "dashboard",
			"EMERGENCY GLOBAL HALT: WorkPaused set on multiple projects via /agents/stop (no repo specified)",
			map[string]any{"action": "agents_stop", "scope": "global", "paused_count": pausedCount})
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":      true,
		"message":      "agents stopped + work generation halted (WorkPaused set on projects)",
		"paused_count": pausedCount,
		"repo":         repo,
	})
}

// Ensure slog is used.
var _ = slog.String
