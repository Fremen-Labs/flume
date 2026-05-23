// api_projects.go — Project lifecycle: create, delete, clone status, task listing.
//
// Direct port of Python: src/dashboard/api/projects.py (6 AST nodes).
// Core logic from: src/dashboard/core/projects_store.py (8 nodes),
//                  src/dashboard/core/project_lifecycle.py (14 nodes).
package dashboard

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/git"
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

	if doc["clone_status"] == "pending" {
		go s.cloneAndSetupProject(id, name, req.RepoURL)
	} else if req.Path != "" {
		go s.runLocalASTIngest(id, name, req.Path)
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

// cloneAndSetupProject is the Go implementation of the background task:
// clone remote repository, ingest AST, and clean up.
func (s *Server) cloneAndSetupProject(id string, name string, repoURL string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	s.logger.Info("Starting project clone background task", slog.String("id", id), slog.String("repoURL", repoURL))

	// 1. Update status to cloning
	s.updateProjectStatus(id, "cloning", nil, nil)

	// 2. Resolve safe clone path
	workspace := os.Getenv("FLUME_WORKSPACE")
	if workspace == "" {
		workspace = "./workspace"
	}
	if err := os.MkdirAll(workspace, 0755); err != nil {
		s.logger.Error("failed to create workspace dir", slog.String("path", workspace), slog.String("error", err.Error()))
		errStr := fmt.Sprintf("Failed to create workspace directory: %s", err)
		s.updateProjectStatus(id, "failed", &errStr, nil)
		return
	}
	destPath := filepath.Join(workspace, fmt.Sprintf("flume-reg-%s", id))

	// 3. Prep URL (embed credentials if possible)
	repoType := git.DetectRepoType(repoURL)
	cloneURL := git.EmbedCredentials(repoURL, repoType)

	// Clean up any stale directory before cloning
	_ = os.RemoveAll(destPath)

	// 4. Git clone
	s.logger.Info("Running git clone", slog.String("id", id), slog.String("destPath", destPath))
	cmd := exec.CommandContext(ctx, "git", "clone", "--", cloneURL, destPath)
	if output, err := cmd.CombinedOutput(); err != nil {
		s.logger.Error("git clone failed", slog.String("id", id), slog.String("error", err.Error()), slog.String("output", string(output)))
		errStr := fmt.Sprintf("Git clone failed: %s (Output: %s)", err, string(output))
		s.updateProjectStatus(id, "failed", &errStr, nil)
		return
	}

	s.logger.Info("Git clone succeeded", slog.String("id", id))

	// 5. Immediately mark as 'cloned' with the local path so the project
	//    is browseable via either local git or remote API regardless of
	//    whether the optional AST ingestion succeeds.
	s.updateProjectStatus(id, "cloned", nil, &destPath)

	// 6. Run elastro AST ingestion (best-effort — failure is non-fatal)
	elastroBin := "elastro"
	if resolved, err := exec.LookPath("elastro"); err == nil {
		elastroBin = resolved
	} else if _, err := os.Stat("/opt/venv/bin/elastro"); err == nil {
		elastroBin = "/opt/venv/bin/elastro"
	}

	ingestCmd := exec.CommandContext(ctx, elastroBin, "rag", "ingest", destPath, "-i", "flume-elastro-graph")
	ingestCmd.Env = os.Environ()
	esURL := s.cfg.ESUrl
	if esURL != "" {
		ingestCmd.Env = append(ingestCmd.Env, fmt.Sprintf("ELASTIC_URL=%s", esURL))
		ingestCmd.Env = append(ingestCmd.Env, fmt.Sprintf("ELASTIC_ELASTICSEARCH_HOSTS=%s", esURL))
		ingestCmd.Env = append(ingestCmd.Env, "ELASTIC_ELASTICSEARCH_VERIFY_CERTS=false")
		ingestCmd.Env = append(ingestCmd.Env, "ELASTIC_VERIFY_CERTS=false")
	}
	if s.cfg.ESApiKey != "" {
		ingestCmd.Env = append(ingestCmd.Env, fmt.Sprintf("ELASTIC_ELASTICSEARCH_AUTH_API_KEY=%s", s.cfg.ESApiKey))
		ingestCmd.Env = append(ingestCmd.Env, "ELASTIC_ELASTICSEARCH_AUTH_TYPE=api_key")
	}

	s.logger.Info("Executing elastro rag ingest", slog.String("id", id), slog.String("bin", elastroBin))
	if output, err := ingestCmd.CombinedOutput(); err != nil {
		// AST ingestion failed — log the error but keep status as 'cloned'
		// so the repo remains browseable via the remote REST API.
		s.logger.Warn("elastro ingestion failed (non-fatal — project remains browseable)",
			slog.String("id", id), slog.String("error", err.Error()),
			slog.String("output", string(output)))
		// Clean up the ephemeral clone since we'll fall back to remote API
		_ = os.RemoveAll(destPath)
		// Keep status 'cloned' but clear the now-deleted local path
		s.updateProjectStatus(id, "cloned", nil, nil)
		s.logger.Info("Project cloned successfully (AST ingest skipped)", slog.String("id", id))
		return
	}

	// 7. Delete ephemeral clone post-ingest — remote API is sufficient
	s.logger.Info("Deleting ephemeral clone post-ingest", slog.String("id", id))
	_ = os.RemoveAll(destPath)

	// 8. Update status to indexed (path cleared since clone is deleted)
	s.updateProjectStatus(id, "indexed", nil, nil)
	s.logger.Info("Project cloned and indexed successfully", slog.String("id", id))
}

// runLocalASTIngest runs elastro AST ingestion on a local repository path.
func (s *Server) runLocalASTIngest(id string, name string, localPath string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	s.logger.Info("Starting local AST ingest task", slog.String("id", id), slog.String("path", localPath))

	elastroBin := "elastro"
	if resolved, err := exec.LookPath("elastro"); err == nil {
		elastroBin = resolved
	} else if _, err := os.Stat("/opt/venv/bin/elastro"); err == nil {
		elastroBin = "/opt/venv/bin/elastro"
	}

	ingestCmd := exec.CommandContext(ctx, elastroBin, "rag", "ingest", localPath, "-i", "flume-elastro-graph")
	ingestCmd.Env = os.Environ()
	esURL := s.cfg.ESUrl
	if esURL != "" {
		ingestCmd.Env = append(ingestCmd.Env, fmt.Sprintf("ELASTIC_URL=%s", esURL))
		ingestCmd.Env = append(ingestCmd.Env, fmt.Sprintf("ELASTIC_ELASTICSEARCH_HOSTS=%s", esURL))
		ingestCmd.Env = append(ingestCmd.Env, "ELASTIC_ELASTICSEARCH_VERIFY_CERTS=false")
		ingestCmd.Env = append(ingestCmd.Env, "ELASTIC_VERIFY_CERTS=false")
	}
	if s.cfg.ESApiKey != "" {
		ingestCmd.Env = append(ingestCmd.Env, fmt.Sprintf("ELASTIC_ELASTICSEARCH_AUTH_API_KEY=%s", s.cfg.ESApiKey))
		ingestCmd.Env = append(ingestCmd.Env, "ELASTIC_ELASTICSEARCH_AUTH_TYPE=api_key")
	}

	if output, err := ingestCmd.CombinedOutput(); err != nil {
		s.logger.Error("local AST ingestion failed", slog.String("id", id), slog.String("error", err.Error()), slog.String("output", string(output)))
		errStr := fmt.Sprintf("AST ingestion failed: %s (Output: %s)", err, string(output))
		s.updateProjectStatus(id, "ast_failed", &errStr, &localPath)
	} else {
		s.updateProjectStatus(id, "local", nil, &localPath)
		s.logger.Info("Local project indexed successfully", slog.String("id", id))
	}
}

// updateProjectStatus updates a project's clone_status, clone_error, and path in ES.
func (s *Server) updateProjectStatus(id string, status string, errStr *string, path *string) {
	ctx := context.Background()
	src, err := s.es.GetDoc(ctx, projectsIndex, id)
	if err != nil {
		s.logger.Error("failed to get project doc to update status", slog.String("id", id), slog.String("error", err.Error()))
		return
	}
	if src == nil {
		s.logger.Error("project doc not found to update status", slog.String("id", id))
		return
	}
	var proj map[string]interface{}
	if err := json.Unmarshal(src, &proj); err != nil {
		s.logger.Error("failed to unmarshal project doc", slog.String("id", id), slog.String("error", err.Error()))
		return
	}

	proj["clone_status"] = status
	if errStr != nil {
		proj["clone_error"] = *errStr
	} else {
		proj["clone_error"] = nil
	}
	if path != nil {
		proj["path"] = *path
	} else {
		proj["path"] = nil
	}
	proj["updated_at"] = nowISO()

	if err := s.es.IndexDoc(ctx, projectsIndex, id, proj); err != nil {
		s.logger.Error("failed to update project status in ES", slog.String("id", id), slog.String("error", err.Error()))
	}
}

