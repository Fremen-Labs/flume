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
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type LastCommit struct {
	Hash    string `json:"hash"`
	Author  string `json:"author"`
	Date    string `json:"date"`
	Subject string `json:"subject"`
}

type RepoInfo struct {
	ID            string      `json:"id"`
	Path          string      `json:"path"`
	Exists        bool        `json:"exists"`
	IsGit         bool        `json:"is_git"`
	CurrentBranch string      `json:"current_branch,omitempty"`
	LastCommit    *LastCommit `json:"last_commit,omitempty"`
}

func gitRepoInfo(id string, path string) RepoInfo {
	info := RepoInfo{
		ID:     id,
		Path:   path,
		Exists: false,
		IsGit:  false,
	}

	fi, err := os.Stat(path)
	if err != nil || !fi.IsDir() {
		return info
	}
	info.Exists = true

	gitDir := filepath.Join(path, ".git")
	_, err = os.Stat(gitDir)
	if err != nil {
		return info
	}
	info.IsGit = true

	// 1. Get current branch
	cmd := exec.Command("git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD")
	if out, err := cmd.Output(); err == nil {
		info.CurrentBranch = strings.TrimSpace(string(out))
	}

	// 2. Get last commit info
	cmd2 := exec.Command("git", "-C", path, "log", "-1", "--pretty=format:%H\n%an\n%ai\n%s")
	if out, err := cmd2.Output(); err == nil {
		lines := strings.Split(string(out), "\n")
		if len(lines) >= 4 {
			info.LastCommit = &LastCommit{
				Hash:    lines[0],
				Author:  lines[1],
				Date:    lines[2],
				Subject: lines[3],
			}
		}
	}

	return info
}

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

	// 1. Projects
	var projects []interface{}
	projectsRes, err := s.es.SearchRaw(ctx, "flume-projects", map[string]interface{}{
		"size": 1000,
		"query": map[string]interface{}{
			"match_all": map[string]interface{}{},
		},
	})
	if err == nil && projectsRes != nil {
		hits, _ := projectsRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				projects = append(projects, src)
			}
		}
	}

	// 2. Tasks (recent, not archived, limit 1000)
	var tasks []interface{}
	tasksRes, err := s.es.SearchRaw(ctx, "agent-task-records", map[string]interface{}{
		"size": 1000,
		"sort": []interface{}{
			map[string]interface{}{"updated_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"bool": map[string]interface{}{
				"must_not": []interface{}{
					map[string]interface{}{"term": map[string]string{"status": "archived"}},
				},
			},
		},
	})
	if err == nil && tasksRes != nil {
		hits, _ := tasksRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			esID, _ := hit["_id"].(string)
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				taskMap := map[string]interface{}{}
				for k, v := range src {
					taskMap[k] = v
				}
				taskMap["_id"] = esID
				thoughts, _ := src["execution_thoughts"].([]interface{})
				taskMap["execution_thoughts_count"] = len(thoughts)
				delete(taskMap, "execution_thoughts")
				tasks = append(tasks, taskMap)
			}
		}
	}

	// 3. Reviews
	var reviews []interface{}
	reviewsRes, err := s.es.SearchRaw(ctx, "agent-review-records", map[string]interface{}{
		"size": 100,
		"sort": []interface{}{
			map[string]interface{}{"created_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"match_all": map[string]interface{}{},
		},
	})
	if err == nil && reviewsRes != nil {
		hits, _ := reviewsRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			esID, _ := hit["_id"].(string)
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				item := map[string]interface{}{}
				for k, v := range src {
					item[k] = v
				}
				item["_id"] = esID
				reviews = append(reviews, item)
			}
		}
	}

	// 4. Failures
	var failures []interface{}
	failuresRes, err := s.es.SearchRaw(ctx, "agent-failure-records", map[string]interface{}{
		"size": 100,
		"sort": []interface{}{
			map[string]interface{}{"updated_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"match_all": map[string]interface{}{},
		},
	})
	if err == nil && failuresRes != nil {
		hits, _ := failuresRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			esID, _ := hit["_id"].(string)
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				item := map[string]interface{}{}
				for k, v := range src {
					item[k] = v
				}
				item["_id"] = esID
				failures = append(failures, item)
			}
		}
	}

	// 5. Provenance
	var provenance []interface{}
	provenanceRes, err := s.es.SearchRaw(ctx, "agent-provenance-records", map[string]interface{}{
		"size": 100,
		"sort": []interface{}{
			map[string]interface{}{"created_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"match_all": map[string]interface{}{},
		},
	})
	if err == nil && provenanceRes != nil {
		hits, _ := provenanceRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			esID, _ := hit["_id"].(string)
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				item := map[string]interface{}{}
				for k, v := range src {
					item[k] = v
				}
				item["_id"] = esID
				provenance = append(provenance, item)
			}
		}
	}

	// 6. Workers
	var workers []interface{}
	workersRes, err := s.es.SearchRaw(ctx, "agent-system-workers", map[string]interface{}{
		"size": 100,
		"sort": []interface{}{
			map[string]interface{}{"updated_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
	})
	if err == nil && workersRes != nil {
		hits, _ := workersRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				nodeWorkers, _ := src["workers"].([]interface{})
				workers = append(workers, nodeWorkers...)
			}
		}
	}

	// 7. Repos
	var repos []interface{}
	for _, p := range projects {
		pm, _ := p.(map[string]interface{})
		if pm == nil {
			continue
		}
		localPath, _ := pm["path"].(string)
		cloneStatus, _ := pm["clone_status"].(string)
		projectID, _ := pm["id"].(string)
		if localPath != "" && cloneStatus == "local" {
			repos = append(repos, gitRepoInfo(projectID, localPath))
		}
	}

	// 8. Token telemetry savings
	tokenMetrics := map[string]interface{}{
		"savings":                      0,
		"baseline_tokens":              0,
		"baseline_full_context_tokens": 0,
		"actual_tokens_sent":           0,
		"total_input_tokens":           0,
		"total_output_tokens":          0,
		"estimated_cost_usd":           0.0,
		"historical_burn":              []interface{}{},
	}
	elastroSavings := 0

	aggRes, err := s.es.SearchRaw(ctx, "agent-token-telemetry", map[string]interface{}{
		"size": 0,
		"aggs": map[string]interface{}{
			"total_elastro_savings":       map[string]interface{}{"sum": map[string]string{"field": "savings"}},
			"total_baseline_tokens":       map[string]interface{}{"sum": map[string]string{"field": "baseline_tokens"}},
			"total_baseline_full_context": map[string]interface{}{"sum": map[string]string{"field": "baseline_full_context_tokens"}},
			"total_actual_tokens":         map[string]interface{}{"sum": map[string]string{"field": "actual_tokens_sent"}},
			"total_input_tokens":          map[string]interface{}{"sum": map[string]string{"field": "input_tokens"}},
			"total_output_tokens":         map[string]interface{}{"sum": map[string]string{"field": "output_tokens"}},
			"by_worker": map[string]interface{}{
				"terms": map[string]interface{}{"field": "worker_name", "size": 100},
				"aggs": map[string]interface{}{
					"input":  map[string]interface{}{"sum": map[string]string{"field": "input_tokens"}},
					"output": map[string]interface{}{"sum": map[string]string{"field": "output_tokens"}},
					"role":   map[string]interface{}{"terms": map[string]string{"field": "worker_role"}},
				},
			},
		},
	})

	if err == nil && aggRes != nil {
		aggs, _ := aggRes["aggregations"].(map[string]interface{})
		if aggs != nil {
			var getSumInt = func(name string) int {
				m, _ := aggs[name].(map[string]interface{})
				if m == nil {
					return 0
				}
				v, _ := m["value"].(float64)
				return int(v)
			}

			tIn := getSumInt("total_input_tokens")
			tOut := getSumInt("total_output_tokens")
			savings := getSumInt("total_elastro_savings")
			elastroSavings = savings

			costIn := 0.002
			costOut := 0.010
			if envCostIn := os.Getenv("FLUME_COST_PER_1K_INPUT"); envCostIn != "" {
				var f float64
				if _, err := fmt.Sscanf(envCostIn, "%f", &f); err == nil {
					costIn = f
				}
			}
			if envCostOut := os.Getenv("FLUME_COST_PER_1K_OUTPUT"); envCostOut != "" {
				var f float64
				if _, err := fmt.Sscanf(envCostOut, "%f", &f); err == nil {
					costOut = f
				}
			}

			estimatedCost := (float64(tIn)/1000.0 * costIn) + (float64(tOut)/1000.0 * costOut)

			var historicalBurn []interface{}
			byWorker, _ := aggs["by_worker"].(map[string]interface{})
			if byWorker != nil {
				buckets, _ := byWorker["buckets"].([]interface{})
				for _, b := range buckets {
					bm, _ := b.(map[string]interface{})
					if bm == nil {
						continue
					}
					workerName, _ := bm["key"].(string)

					inputM, _ := bm["input"].(map[string]interface{})
					inputVal, _ := inputM["value"].(float64)

					outputM, _ := bm["output"].(map[string]interface{})
					outputVal, _ := outputM["value"].(float64)

					role := "unknown"
					roleM, _ := bm["role"].(map[string]interface{})
					if roleM != nil {
						roleBuckets, _ := roleM["buckets"].([]interface{})
						if len(roleBuckets) > 0 {
							rbm, _ := roleBuckets[0].(map[string]interface{})
							if rbm != nil {
								role, _ = rbm["key"].(string)
							}
						}
					}

					historicalBurn = append(historicalBurn, map[string]interface{}{
						"worker_name":   workerName,
						"input_tokens":  int(inputVal),
						"output_tokens": int(outputVal),
						"role":          role,
					})
				}
			}

			tokenMetrics = map[string]interface{}{
				"savings":                      savings,
				"baseline_tokens":              getSumInt("total_baseline_tokens"),
				"baseline_full_context_tokens": getSumInt("total_baseline_full_context"),
				"actual_tokens_sent":           getSumInt("total_actual_tokens"),
				"total_input_tokens":           tIn,
				"total_output_tokens":          tOut,
				"estimated_cost_usd":           estimatedCost,
				"historical_burn":              historicalBurn,
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"workers":         orSliceIface(workers),
		"tasks":           orSliceIface(tasks),
		"reviews":         orSliceIface(reviews),
		"failures":        orSliceIface(failures),
		"provenance":      orSliceIface(provenance),
		"repos":           orSliceIface(repos),
		"projects":        orSliceIface(projects),
		"elastro_savings": elastroSavings,
		"token_metrics":   tokenMetrics,
		"timestamp":       nowISO(),
	})
}

// ─── GET /api/system-state ──────────────────────────────────────────────────

// handleSystemState returns comprehensive system state.
// Derived from Python: api/system.py api_system_state().
func (s *Server) handleSystemState(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// 1. ES cluster health
	esHealthy := false
	if err := s.es.Ping(ctx); err == nil {
		esHealthy = true
	}

	// 2. Fetch workers array
	var workers []interface{}
	workersRes, err := s.es.SearchRaw(ctx, "agent-system-workers", map[string]interface{}{
		"size": 100,
	})
	if err == nil && workersRes != nil {
		hits, _ := workersRes["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			src, _ := hit["_source"].(map[string]interface{})
			if src != nil {
				if nodeWorkers, ok := src["workers"].([]interface{}); ok {
					workers = append(workers, nodeWorkers...)
				}
			}
		}
	}
	if workers == nil {
		workers = []interface{}{}
	}

	// 3. Calculate standby/active worker counts
	totalNodes := len(workers)
	activeStreams := 0
	standbyNodes := 0
	for _, wVal := range workers {
		if wMap, ok := wVal.(map[string]interface{}); ok {
			status, _ := wMap["status"].(string)
			if status == "claimed" || status == "active" || status == "busy" || status == "running" {
				activeStreams++
			} else {
				standbyNodes++
			}
		}
	}

	// 4. Fetch AST count from flume-elastro-graph
	elasticAstCount, err := s.es.Count(ctx, "flume-elastro-graph", map[string]interface{}{})
	if err != nil {
		elasticAstCount = 0
	}

	// 5. Fetch Vault status
	vaultSealed := true
	vaultAddr := envOr("OPENBAO_ADDR", envOr("VAULT_ADDR", ""))
	if vaultAddr != "" {
		client := &http.Client{Timeout: 1 * time.Second}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/v1/sys/health", vaultAddr), nil)
		if err == nil {
			resp, err := client.Do(req)
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode == 200 || resp.StatusCode == 429 {
					vaultSealed = false
				}
			}
		}
	}

	// 6. Fetch Completed Tasks Count
	completedWork, err := s.es.Count(ctx, "agent-task-records", map[string]interface{}{
		"term": map[string]interface{}{"status": "done"},
	})
	if err != nil {
		completedWork = 0
	}

	// 7. Gateway LLM latency check
	llmLatency := "---"
	gatewayURL := os.Getenv("GATEWAY_URL")
	if gatewayURL == "" {
		gatewayURL = "http://localhost:8090"
	}
	start := time.Now()
	client := &http.Client{Timeout: 500 * time.Millisecond}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, gatewayURL+"/health", nil)
	if err == nil {
		resp, err := client.Do(req)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				llmLatency = fmt.Sprintf("%dms", time.Since(start).Milliseconds())
			}
		}
	}

	// 8. Build telemetry
	telemetry := map[string]interface{}{
		"completedWork":   completedWork,
		"llmLatency":      llmLatency,
		"elasticAstCount": elasticAstCount,
		"vaultSealed":     vaultSealed,
	}

	var memStats runtime.MemStats
	runtime.ReadMemStats(&memStats)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"elasticsearch": map[string]interface{}{
			"healthy": esHealthy,
			"url":     s.cfg.ESUrl,
		},
		"workers":       workers,
		"standbyNodes":  standbyNodes,
		"activeStreams": activeStreams,
		"totalNodes":    totalNodes,
		"telemetry":     telemetry,
		"runtime": map[string]interface{}{
			"go_version":  runtime.Version(),
			"goroutines":  runtime.NumGoroutine(),
			"heap_mb":     memStats.HeapAlloc / 1024 / 1024,
			"sys_mb":      memStats.Sys / 1024 / 1024,
			"native_mode": s.cfg.NativeMode,
		},
		"uptime":     time.Since(s.startTime).String(),
		"updated_at": nowISO(),
		"timestamp":  nowISO(),
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
		"parent-revival": true,
		"stuck-worker":   true,
		"plan-progress":  true,
		"orphan-gc":      true,
	}

	if !validSweeps[sweepName] {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("unknown sweep: %q", sweepName))
		return
	}

	s.logger.Info("autonomy sweep triggered", slog.String("sweep", sweepName))

	s.mu.RLock()
	cb := s.onSweepTrigger
	s.mu.RUnlock()

	var err error
	if cb != nil {
		err = cb(sweepName)
	}

	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

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

	s.mu.RLock()
	cb := s.onSweepTrigger
	s.mu.RUnlock()

	var err error
	if cb != nil {
		err = cb("auto-unblock")
	}

	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

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
