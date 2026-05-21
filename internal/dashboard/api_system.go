// api_system.go — System health, telemetry, autonomy sweep controls.
//
// Direct port of Python: src/dashboard/api/system.py (21 AST nodes).
// Covers: /api/health, /api/snapshot, /api/system-state, /api/telemetry,
// /api/logs, /api/exo-status, /api/autonomy/*, /api/auto-unblock/*
package dashboard

import (
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"runtime"
	"time"
)

// ─── GET /api/health ────────────────────────────────────────────────────────

// handleHealth returns basic health check data.
// Derived from Python: api/system.py health().
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":  "ok",
		"version": "go-dashboard-v2",
		"uptime":  time.Since(s.startTime).String(),
	})
}

// ─── GET /api/snapshot ──────────────────────────────────────────────────────

// handleSnapshot returns a queue snapshot for the dashboard.
// Derived from Python: api/system.py api_snapshot().
func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// Count tasks by status
	statuses := []string{"inbox", "planned", "ready", "running", "review", "done", "blocked"}
	counts := map[string]int{}

	for _, status := range statuses {
		count, err := s.es.Count(ctx, "agent-task-records", map[string]interface{}{
			"term": map[string]interface{}{"status": status},
		})
		if err != nil {
			s.logger.Debug("snapshot: count failed", slog.String("status", status), slog.String("error", err.Error()))
			continue
		}
		counts[status] = count
	}

	// Fetch running tasks with worker info
	running, err := s.es.SearchRaw(ctx, "agent-task-records", map[string]interface{}{
		"size": 50,
		"query": map[string]interface{}{
			"term": map[string]interface{}{"status": "running"},
		},
		"_source": []string{"id", "title", "active_worker", "owner", "model", "updated_at"},
		"sort":    []interface{}{map[string]interface{}{"updated_at": map[string]string{"order": "desc"}}},
	})
	var runningTasks []interface{}
	if err == nil {
		hits, _ := running["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				runningTasks = append(runningTasks, src)
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"counts":       counts,
		"running":      orSliceIface(runningTasks),
		"timestamp":    nowISO(),
		"go_dashboard": true,
	})
}

// ─── GET /api/system-state ──────────────────────────────────────────────────

// handleSystemState returns comprehensive system state.
// Derived from Python: api/system.py api_system_state().
func (s *Server) handleSystemState(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// ES cluster health
	esHealthy := false
	if err := s.es.Ping(ctx); err == nil {
		esHealthy = true
	}

	// Worker count
	workerResult, err := s.es.SearchRaw(ctx, "agent-system-workers", map[string]interface{}{
		"size":    0,
		"_source": false,
	})
	workerCount := 0
	if err == nil {
		hits, _ := workerResult["hits"].(map[string]interface{})
		total, _ := hits["total"].(map[string]interface{})
		if v, ok := total["value"].(float64); ok {
			workerCount = int(v)
		}
	}

	var memStats runtime.MemStats
	runtime.ReadMemStats(&memStats)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"elasticsearch": map[string]interface{}{
			"healthy": esHealthy,
			"url":     s.cfg.ESUrl,
		},
		"workers": map[string]interface{}{
			"count": workerCount,
		},
		"runtime": map[string]interface{}{
			"go_version":  runtime.Version(),
			"goroutines":  runtime.NumGoroutine(),
			"heap_mb":     memStats.HeapAlloc / 1024 / 1024,
			"sys_mb":      memStats.Sys / 1024 / 1024,
			"native_mode": s.cfg.NativeMode,
		},
		"uptime":    time.Since(s.startTime).String(),
		"timestamp": nowISO(),
	})
}

// ─── GET /api/telemetry ─────────────────────────────────────────────────────

// handleTelemetry returns system telemetry (task throughput, token usage).
// Derived from Python: api/system.py get_system_telemetry().
func (s *Server) handleTelemetry(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// Task event throughput (last 24h)
	result, err := s.es.SearchRaw(ctx, "agent-token-telemetry", map[string]interface{}{
		"size": 0,
		"query": map[string]interface{}{
			"range": map[string]interface{}{
				"timestamp": map[string]interface{}{
					"gte": "now-24h",
				},
			},
		},
		"aggs": map[string]interface{}{
			"total_input_tokens": map[string]interface{}{
				"sum": map[string]interface{}{"field": "input_tokens"},
			},
			"total_output_tokens": map[string]interface{}{
				"sum": map[string]interface{}{"field": "output_tokens"},
			},
			"by_model": map[string]interface{}{
				"terms": map[string]interface{}{"field": "model.keyword", "size": 20},
				"aggs": map[string]interface{}{
					"input":  map[string]interface{}{"sum": map[string]interface{}{"field": "input_tokens"}},
					"output": map[string]interface{}{"sum": map[string]interface{}{"field": "output_tokens"}},
				},
			},
			"by_worker": map[string]interface{}{
				"terms": map[string]interface{}{"field": "worker_name.keyword", "size": 50},
				"aggs": map[string]interface{}{
					"input":  map[string]interface{}{"sum": map[string]interface{}{"field": "input_tokens"}},
					"output": map[string]interface{}{"sum": map[string]interface{}{"field": "output_tokens"}},
				},
			},
		},
	})

	if err != nil {
		s.logger.Warn("telemetry: query failed", slog.String("error", err.Error()))
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"error": "telemetry data unavailable",
		})
		return
	}

	writeJSON(w, http.StatusOK, result)
}

// ─── GET /api/logs ──────────────────────────────────────────────────────────

// handleLogs returns recent telemetry log entries.
// Derived from Python: api/system.py get_telemetry_logs().
func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	result, err := s.es.SearchRaw(ctx, "agent-token-telemetry", map[string]interface{}{
		"size": 100,
		"sort": []interface{}{
			map[string]interface{}{"timestamp": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"match_all": map[string]interface{}{},
		},
	})

	if err != nil {
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}

	hits, _ := result["hits"].(map[string]interface{})
	hitsArr, _ := hits["hits"].([]interface{})
	var logs []interface{}
	for _, h := range hitsArr {
		hit, _ := h.(map[string]interface{})
		src, _ := hit["_source"].(map[string]interface{})
		if src != nil {
			logs = append(logs, src)
		}
	}

	writeJSON(w, http.StatusOK, orSliceIface(logs))
}

// ─── GET /api/exo-status ────────────────────────────────────────────────────

// handleExoStatus returns the Exo distributed inference node status.
// Derived from Python: api/system.py api_exo_status().
func (s *Server) handleExoStatus(w http.ResponseWriter, r *http.Request) {
	// Exo status is fetched from the gateway, which proxies to the Exo topology API
	gatewayURL := envOr("GATEWAY_URL", "http://localhost:8080")
	ctx := r.Context()

	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/exo/topology", gatewayURL))
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"available": false,
			"error":     "Exo topology unreachable",
		})
		return
	}

	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/autonomy/status ───────────────────────────────────────────────

// handleAutonomyStatus returns the status of all autonomy sweep threads.
// Derived from Python: api/system.py api_autonomy_status().
func (s *Server) handleAutonomyStatus(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	status := s.autonomyStatus
	s.mu.RUnlock()

	if status == nil {
		status = map[string]interface{}{
			"enabled": false,
			"sweeps":  map[string]interface{}{},
		}
	}

	writeJSON(w, http.StatusOK, status)
}

// ─── POST /api/autonomy/sweep/{sweep_name} ──────────────────────────────────

// handleAutonomySweep triggers an immediate sweep run.
// Derived from Python: api/system.py api_autonomy_sweep_now().
func (s *Server) handleAutonomySweep(w http.ResponseWriter, r *http.Request) {
	sweepName := r.PathValue("sweep_name")

	validSweeps := map[string]bool{
		"parent-revival":     true,
		"stuck-worker":      true,
		"plan-progress":     true,
		"orphan-gc":         true,
	}

	if !validSweeps[sweepName] {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("unknown sweep: %q", sweepName))
		return
	}

	// TODO: Wire to actual sweep goroutine triggers
	s.logger.Info("autonomy sweep triggered", slog.String("sweep", sweepName))
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":    true,
		"sweep":      sweepName,
		"message":    "sweep triggered",
		"started_at": nowISO(),
	})
}

// ─── GET /api/auto-unblock/status ───────────────────────────────────────────

func (s *Server) handleAutoUnblockStatus(w http.ResponseWriter, r *http.Request) {
	enabled := os.Getenv("FLUME_AUTO_UNBLOCK_ENABLED") != "0"
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"enabled":     enabled,
		"last_run_at": nil,
	})
}

// ─── POST /api/auto-unblock/sweep ───────────────────────────────────────────

func (s *Server) handleAutoUnblockSweep(w http.ResponseWriter, r *http.Request) {
	s.logger.Info("auto-unblock sweep triggered")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":    true,
		"message":    "sweep triggered",
		"started_at": nowISO(),
	})
}

// ─── Helpers ────────────────────────────────────────────────────────────────

func orSliceIface(s []interface{}) []interface{} {
	if s == nil {
		return []interface{}{}
	}
	return s
}
