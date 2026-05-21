// api_nodes.go — Node mesh management: list, add, delete, test, routing policy.
//
// Direct port of Python: src/dashboard/api/nodes.py (18 AST nodes).
// These endpoints proxy to the Go gateway's /admin/nodes API.
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
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/admin/nodes", s.gatewayBase()))
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

	resp, err := s.es.HTTPPost(ctx, fmt.Sprintf("%s/admin/nodes", s.gatewayBase()), body)
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
	resp, err := s.es.HTTPDelete(ctx, fmt.Sprintf("%s/admin/nodes/%s", s.gatewayBase(), nodeID))
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
	resp, err := s.es.HTTPPost(ctx, fmt.Sprintf("%s/admin/nodes/%s/test", s.gatewayBase(), nodeID), nil)
	if err != nil {
		writeError(w, http.StatusBadGateway, "gateway error")
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/routing-policy ────────────────────────────────────────────────

func (s *Server) handleRoutingPolicyGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/admin/routing-policy", s.gatewayBase()))
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

	resp, err := s.es.HTTPPut(ctx, fmt.Sprintf("%s/admin/routing-policy", s.gatewayBase()), body)
	if err != nil {
		writeError(w, http.StatusBadGateway, "gateway error")
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/frontier-models ───────────────────────────────────────────────

func (s *Server) handleFrontierModels(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/admin/frontier-models", s.gatewayBase()))
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"models": []interface{}{}, "error": "gateway unreachable"})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/nodes/utilization ─────────────────────────────────────────────

func (s *Server) handleNodesUtilization(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	resp, err := s.es.HTTPGet(ctx, fmt.Sprintf("%s/admin/nodes/utilization", s.gatewayBase()))
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"nodes": []interface{}{}, "error": "gateway unreachable"})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// Compile-time guard for strings usage.
var _ = strings.TrimSpace
