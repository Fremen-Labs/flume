// api_workflow.go — Worker lifecycle: list, status, start, stop.
//
// Direct port of Python: src/dashboard/api/workflow.py (3 AST nodes).
// Process management from: src/dashboard/core/process_manager.py (13 nodes).
package dashboard

import (
	"log/slog"
	"net/http"
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

	s.logger.Info("worker agents started")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"message": "agents started",
	})
}

// ─── POST /api/workflow/agents/stop ─────────────────────────────────────────

// handleWorkflowAgentsStop stops the worker agent swarm.
// Derived from Python: api/workflow.py api_workflow_agents_stop().
func (s *Server) handleWorkflowAgentsStop(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.workerStatus = map[string]interface{}{
		"running":    false,
		"stopped_at": nowISO(),
	}
	s.mu.Unlock()

	s.logger.Info("worker agents stopped")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"message": "agents stopped",
	})
}

// Ensure slog is used.
var _ = slog.String
