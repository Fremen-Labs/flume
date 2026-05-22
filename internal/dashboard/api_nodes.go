// api_nodes.go — Node mesh management: list, add, delete, test, routing policy.
//
// Direct port of Python: src/dashboard/api/nodes.py (18 AST nodes).
// These endpoints proxy to the Go gateway's /api/nodes API.
package dashboard

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
)

// gatewayBase returns the gateway URL.
// Derived from Python: api/nodes.py _gateway_base().
func (s *Server) gatewayBase() string {
	return envOr("GATEWAY_URL", "http://localhost:8080")
}

// ─── GET /api/nodes ─────────────────────────────────────────────────────────

// handleNodesList returns the list of Ollama inference nodes.
// Derived from Python: api/nodes.py api_nodes_list().
func (s *Server) handleNodesList(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/api/nodes", s.gatewayBase()))
	if err != nil {
		s.logger.Warn("nodes list: gateway unreachable", slog.String("error", err.Error()))
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"nodes": []interface{}{},
			"error": "gateway unreachable",
		})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── POST /api/nodes ────────────────────────────────────────────────────────

// handleNodesAdd adds a new Ollama node to the mesh.
// Derived from Python: api/nodes.py api_nodes_add().
func (s *Server) handleNodesAdd(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body json.RawMessage
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	r.Body.Close()

	resp, err := s.es.HTTPPost(ctx, fmt.Sprintf("%s/api/nodes", s.gatewayBase()), body)
	if err != nil {
		s.logger.Error("nodes add failed", slog.String("error", err.Error()))
		writeError(w, http.StatusBadGateway, "gateway error")
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── DELETE /api/nodes/{node_id} ────────────────────────────────────────────

func (s *Server) handleNodesDelete(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	nodeID := r.PathValue("node_id")
	resp, err := s.es.HTTPDelete(ctx, fmt.Sprintf("%s/api/nodes/%s", s.gatewayBase(), nodeID))
	if err != nil {
		writeError(w, http.StatusBadGateway, "gateway error")
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── POST /api/nodes/{node_id}/test ─────────────────────────────────────────

func (s *Server) handleNodesTest(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	nodeID := r.PathValue("node_id")
	resp, err := s.es.HTTPPost(ctx, fmt.Sprintf("%s/api/nodes/%s/test", s.gatewayBase(), nodeID), nil)
	if err != nil {
		writeError(w, http.StatusBadGateway, "gateway error")
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/routing-policy ────────────────────────────────────────────────

func (s *Server) handleRoutingPolicyGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/api/routing-policy", s.gatewayBase()))
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"error": "gateway unreachable"})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── PUT /api/routing-policy ────────────────────────────────────────────────

func (s *Server) handleRoutingPolicyPut(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body json.RawMessage
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	r.Body.Close()

	resp, err := s.es.HTTPPut(ctx, fmt.Sprintf("%s/api/routing-policy", s.gatewayBase()), body)
	if err != nil {
		writeError(w, http.StatusBadGateway, "gateway error")
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/frontier-models ───────────────────────────────────────────────

func (s *Server) handleFrontierModels(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/api/frontier-models", s.gatewayBase()))
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"models": []interface{}{}, "error": "gateway unreachable"})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/nodes/utilization ─────────────────────────────────────────────

func (s *Server) handleNodesUtilization(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// 1. Fetch node registry for capacity data
	nodesResp, err := s.es.SearchRaw(ctx, "flume-node-registry", map[string]interface{}{
		"size":  100,
		"query": map[string]interface{}{"match_all": map[string]interface{}{}},
	})
	if err != nil {
		s.logger.Error("nodes utilization: failed to search node-registry", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to query nodes registry")
		return
	}

	// 2. Fetch worker state
	workersResp, err := s.es.SearchRaw(ctx, "agent-system-workers", map[string]interface{}{
		"size":  100,
		"query": map[string]interface{}{"match_all": map[string]interface{}{}},
	})
	if err != nil {
		s.logger.Error("nodes utilization: failed to search system-workers", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to query workers")
		return
	}

	// Process workers
	nodeRunningTasks := map[string]int{}
	totalWorkers := 0

	workerHits, _ := workersResp["hits"].(map[string]interface{})
	workerHitsArr, _ := workerHits["hits"].([]interface{})
	for _, wh := range workerHitsArr {
		whm, _ := wh.(map[string]interface{})
		if whm == nil {
			continue
		}
		esID, _ := whm["_id"].(string)
		src, _ := whm["_source"].(map[string]interface{})
		if src == nil {
			continue
		}
		workersList, _ := src["workers"].([]interface{})
		totalWorkers += len(workersList)
		for _, wItem := range workersList {
			wm, _ := wItem.(map[string]interface{})
			if wm == nil {
				continue
			}
			if wm["status"] == "claimed" {
				nodeID, _ := wm["node_id"].(string)
				if nodeID == "" {
					nodeID = esID
				}
				nodeRunningTasks[nodeID] = nodeRunningTasks[nodeID] + 1
			}
		}
	}

	// 3. Fetch completed / running task status counts (last 24h)
	completedResp, err := s.es.SearchRaw(ctx, "flume-task-events", map[string]interface{}{
		"size": 0,
		"query": map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []interface{}{
					map[string]interface{}{"term": map[string]string{"event_type": "doc_update"}},
					map[string]interface{}{"range": map[string]interface{}{"timestamp": map[string]string{"gte": "now-24h"}}},
				},
			},
		},
		"aggs": map[string]interface{}{
			"by_status": map[string]interface{}{
				"terms": map[string]interface{}{"field": "details.status.keyword", "size": 10},
			},
		},
	})

	completedCount := 0
	runningCount := 0
	if err == nil && completedResp != nil {
		aggs, _ := completedResp["aggregations"].(map[string]interface{})
		byStatus, _ := aggs["by_status"].(map[string]interface{})
		if byStatus != nil {
			buckets, _ := byStatus["buckets"].([]interface{})
			for _, b := range buckets {
				bm, _ := b.(map[string]interface{})
				if bm == nil {
					continue
				}
				key, _ := bm["key"].(string)
				docCountVal, _ := bm["doc_count"].(float64)
				docCount := int(docCountVal)
				if key == "done" {
					completedCount += docCount
				} else if key == "running" {
					runningCount += docCount
				}
			}
		}
	}

	// 4. Build per-node utilization
	var nodeList []interface{}
	totalCap := 0

	hits, _ := nodesResp["hits"].(map[string]interface{})
	hitsArr, _ := hits["hits"].([]interface{})
	for _, n := range hitsArr {
		nm, _ := n.(map[string]interface{})
		if nm == nil {
			continue
		}
		esID, _ := nm["_id"].(string)
		src, _ := nm["_source"].(map[string]interface{})
		if src == nil {
			continue
		}
		nodeID, _ := src["id"].(string)
		if nodeID == "" {
			nodeID = esID
		}
		capVal, _ := src["concurrency_cap"].(float64)
		capInt := int(capVal)
		if capInt == 0 {
			capInt = 4 // Default cap
		}
		totalCap += capInt
		health, _ := src["health"].(map[string]interface{})
		if health == nil {
			health = map[string]interface{}{}
		}
		running := nodeRunningTasks[nodeID]

		utilizationPct := 0.0
		if capInt > 0 {
			utilizationPct = float64(running) / float64(capInt) * 100.0
		}

		nodeList = append(nodeList, map[string]interface{}{
			"id":              nodeID,
			"host":            src["host"],
			"model":           src["model_tag"],
			"capacity":        capInt,
			"running_tasks":   running,
			"utilization_pct": float64(int(utilizationPct*10+0.5)) / 10.0, // round to 1 decimal
			"health_status":   health["status"],
			"latency_ms":      health["latency_ms"],
			"current_load":    health["current_load"],
		})
	}

	sumRunningTasks := 0
	for _, v := range nodeRunningTasks {
		sumRunningTasks += v
	}
	totalsUtilizationPct := 0.0
	if totalCap > 0 {
		totalsUtilizationPct = float64(sumRunningTasks) / float64(totalCap) * 100.0
	}

	totals := map[string]interface{}{
		"tasks_completed_24h": completedCount,
		"tasks_running":       runningCount,
		"total_capacity":      totalCap,
		"worker_pool_size":    totalWorkers,
		"utilization_pct":     float64(int(totalsUtilizationPct*10+0.5)) / 10.0,
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"nodes":  nodeList,
		"totals": totals,
	})
}

// Compile-time guard for strings usage.
var _ = strings.TrimSpace
