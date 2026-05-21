// api_repos.go — Repository browsing: branches, tree, file, diff.
//
// Direct port of Python: src/dashboard/api/repos.py (3 AST nodes).
// These endpoints support both local repos (git subprocess) and remote
// repos (GitHostClient REST API — wired in Phase 3).
package dashboard

import (
	"net/http"
)

// ─── GET /api/repos/{project_id}/branches ───────────────────────────────────

// handleRepoBranches returns branch list for a project.
// Derived from Python: api/repos.py api_repo_branches().
func (s *Server) handleRepoBranches(w http.ResponseWriter, r *http.Request) {
	// TODO: Implement via local git or GitHostClient (Phase 3).
	projectID := r.PathValue("project_id")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"project_id": projectID,
		"branches":   []interface{}{},
		"error":      "branch listing not yet implemented in Go dashboard",
	})
}

// ─── GET /api/repos/{project_id}/tree ───────────────────────────────────────

// handleRepoTree returns the file tree for a branch.
// Derived from Python: api/repos.py api_repo_tree().
func (s *Server) handleRepoTree(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("project_id")
	branch := r.URL.Query().Get("branch")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"project_id": projectID,
		"branch":     branch,
		"tree":       []interface{}{},
		"error":      "tree listing not yet implemented in Go dashboard",
	})
}

// ─── GET /api/repos/{project_id}/file ───────────────────────────────────────

// handleRepoFile returns file content.
// Derived from Python: api/repos.py api_repo_file().
func (s *Server) handleRepoFile(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("project_id")
	path := r.URL.Query().Get("path")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"project_id": projectID,
		"path":       path,
		"content":    "",
		"error":      "file content not yet implemented in Go dashboard",
	})
}

// ─── GET /api/repos/{project_id}/diff ───────────────────────────────────────

// handleRepoDiff returns diff between two branches.
// Derived from Python: api/repos.py api_repo_diff().
func (s *Server) handleRepoDiff(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("project_id")
	base := r.URL.Query().Get("base")
	head := r.URL.Query().Get("head")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"project_id": projectID,
		"base":       base,
		"head":       head,
		"diff":       "",
		"error":      "repo diff not yet implemented in Go dashboard",
	})
}
