// api_projects.go — Project lifecycle: create, delete, clone status, task listing.
//
// Direct port of Python: src/dashboard/api/projects.py (6 AST nodes).
// Core logic from: src/dashboard/core/projects_store.py (8 nodes),
//                  src/dashboard/core/project_lifecycle.py (14 nodes).
package dashboard

import (
	"fmt"
	"log/slog"
	"net/http"
	"strings"
)

const projectsIndex = "flume-projects"

// ─── POST /api/projects ─────────────────────────────────────────────────────

// handleProjectCreate creates a new project in the ES registry.
// Derived from Python: api/projects.py api_create_project().
func (s *Server) handleProjectCreate(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req struct {
		Name        string `json:"name"`
		RepoURL     string `json:"repoUrl,omitempty"`
		Branch      string `json:"branch,omitempty"`
		Description string `json:"description,omitempty"`
		Path        string `json:"path,omitempty"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	name := strings.TrimSpace(req.Name)
	if name == "" {
		writeError(w, http.StatusBadRequest, "name is required")
		return
	}

	// Generate a slug-style ID from the name
	id := strings.ToLower(strings.ReplaceAll(name, " ", "-"))

	now := nowISO()
	doc := map[string]interface{}{
		"id":          id,
		"name":        name,
		"repoUrl":     strings.TrimSpace(req.RepoURL),
		"branch":      strings.TrimSpace(req.Branch),
		"description": strings.TrimSpace(req.Description),
		"path":        strings.TrimSpace(req.Path),
		"created_at":  now,
		"updated_at":  now,
	}

	// Determine clone_status
	if req.Path != "" {
		doc["clone_status"] = "local"
	} else if req.RepoURL != "" {
		doc["clone_status"] = "pending"
	}

	if err := s.es.IndexDoc(ctx, projectsIndex, id, doc); err != nil {
		s.logger.Error("project create failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to create project")
		return
	}

	s.logger.Info("project created", slog.String("id", id), slog.String("name", name))
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"project": doc,
	})
}

// ─── GET /api/projects/{project_id}/clone-status ────────────────────────────

func (s *Server) handleProjectCloneStatus(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("project_id")
	ctx := r.Context()

	src, err := s.es.GetDoc(ctx, projectsIndex, projectID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch project")
		return
	}
	if src == nil {
		writeError(w, http.StatusNotFound, "project not found")
		return
	}

	var proj map[string]interface{}
	if err := unmarshalRaw(src, &proj); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to parse project")
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"id":           projectID,
		"clone_status": proj["clone_status"],
		"path":         proj["path"],
	})
}

// ─── GET /api/projects/{project_id}/tasks ───────────────────────────────────

// handleProjectTasks returns all tasks for a project.
// Derived from Python: api/projects.py api_project_tasks().
func (s *Server) handleProjectTasks(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("project_id")
	ctx := r.Context()

	// Status filter from query string
	statusFilter := r.URL.Query().Get("status")

	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]interface{}{"repo": projectID}},
			},
			"must_not": []interface{}{
				map[string]interface{}{"term": map[string]interface{}{"status": "archived"}},
			},
		},
	}

	if statusFilter != "" {
		must := query["bool"].(map[string]interface{})["must"].([]interface{})
		must = append(must, map[string]interface{}{"term": map[string]interface{}{"status": statusFilter}})
		query["bool"].(map[string]interface{})["must"] = must
	}

	result, err := s.es.SearchRaw(ctx, "agent-task-records", map[string]interface{}{
		"size":  500,
		"query": query,
		"sort": []interface{}{
			map[string]interface{}{"updated_at": map[string]string{"order": "desc", "unmapped_type": "date"}},
		},
	})
	if err != nil {
		s.logger.Error("project tasks: search failed", slog.String("error", err.Error()))
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}

	hits, _ := result["hits"].(map[string]interface{})
	hitsArr, _ := hits["hits"].([]interface{})
	var tasks []interface{}
	for _, h := range hitsArr {
		hit, _ := h.(map[string]interface{})
		src, _ := hit["_source"].(map[string]interface{})
		esID, _ := hit["_id"].(string)
		if src != nil {
			// Map to frontend-expected shape
			task := map[string]interface{}{
				"_id":     esID,
				"id":      src["id"],
				"title":   src["title"],
				"status":  src["status"],
				"priority": src["priority"],
				"owner":   src["owner"],
				"branch":  src["branch"],
				"updated_at": orIface(src["updated_at"], src["last_update"]),
			}
			tasks = append(tasks, task)
		}
	}

	writeJSON(w, http.StatusOK, orSliceIface(tasks))
}

// ─── POST /api/projects/{project_id}/delete ─────────────────────────────────

func (s *Server) handleProjectDelete(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("project_id")
	ctx := r.Context()

	if err := s.es.DeleteDoc(ctx, projectsIndex, projectID); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("failed to delete project: %s", err))
		return
	}

	s.logger.Info("project deleted", slog.String("id", projectID))
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"id":      projectID,
	})
}
