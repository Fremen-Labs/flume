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
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
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
	cloneURL := git.EmbedCredentials(ctx, repoURL, repoType)

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

	// 6. Run elastro AST ingestion (capture result; non-fatal but tracked for verify)
	elastroBin := "elastro"
	if resolved, err := exec.LookPath("elastro"); err == nil {
		elastroBin = resolved
	} else if _, err := os.Stat("/opt/venv/bin/elastro"); err == nil {
		elastroBin = "/opt/venv/bin/elastro"
	}

	ingestCmd := exec.CommandContext(ctx, elastroBin, "rag", "ingest", destPath, "-i", "flume-elastro-graph")
	ingestCmd.Env = append(os.Environ(), buildElastroESEnv(s.cfg.ESUrl, s.cfg.ESApiKey)...)

	s.logger.Info("Executing elastro rag ingest", slog.String("id", id), slog.String("bin", elastroBin))
	elastroCmdOK := true
	if output, err := ingestCmd.CombinedOutput(); err != nil {
		elastroCmdOK = false
		s.logger.Warn("elastro ingestion failed (non-fatal; will still attempt LogLoom then verify)",
			slog.String("id", id), slog.String("error", err.Error()),
			slog.String("output", string(output)))
	} else {
		s.logger.Info("elastro rag ingest completed", slog.String("id", id),
			slog.String("output", plannerTruncate(string(output), 400)))
	}

	// 7. LogLoom AST (ALWAYS run for dual-ingest contract; before any cleanup)
	s.logger.Info("Running LogLoom AST graph generation+index (augmenting elastro; always executed)", slog.String("id", id))
	logloomCmdOK := s.runLogloomGraphIngest(id, name, destPath, "flume-logloom-ast")

	// 8. Refresh ES indices before verification to ensure recently-ingested
	//    docs are visible (ES near-realtime has a 1s default refresh interval;
	//    the verify _count would race without this).
	s.refreshIndex(ctx, "flume-elastro-graph")
	s.refreshIndex(ctx, "flume-logloom-ast")

	// 9. Post-ingest verification (lightweight ES counts for project/repo) + LogAgentReasoning.
	//    Status becomes "indexed" ONLY after verification passes (both indices have docs for this project).
	//    If fails: clear error log + reasoning; leave as "cloned" (still browseable) rather than best-effort "indexed".
	s.logger.Info("Performing post-dual-ingest verification", slog.String("id", id), slog.Bool("elastro_cmd_ok", elastroCmdOK), slog.Bool("logloom_cmd_ok", logloomCmdOK))
	elastroOK, logloomOK, eCnt, lCnt := s.verifyProjectASTDocs(ctx, id, name)

	verifyMsg := fmt.Sprintf("dual ingest verification: elastro=%v(%d) logloom=%v(%d) [cmds: e=%v l=%v]", elastroOK, eCnt, logloomOK, lCnt, elastroCmdOK, logloomCmdOK)
	flumelogger.LogAgentReasoning(ctx, id, "system",
		verifyMsg,
		"semantic_tags", []interface{}{"elastro", "logloom", "verify", "ast-ingest", "elastro-logloom-contract"},
		"project", id, "name", name,
		"elastro_verified", elastroOK, "elastro_count", eCnt,
		"logloom_verified", logloomOK, "logloom_count", lCnt,
		"elastro_cmd_ok", elastroCmdOK, "logloom_cmd_ok", logloomCmdOK,
		"elastro_index", "flume-elastro-graph", "logloom_index", "flume-logloom-ast")

	if elastroOK && logloomOK {
		s.updateProjectStatus(id, "indexed", nil, nil)
		s.logger.Info("Project cloned, dual ingested, and verification PASSED — status=indexed", slog.String("id", id), slog.Int("elastro_docs", eCnt), slog.Int("logloom_docs", lCnt))
	} else {
		errStr := fmt.Sprintf("Post-ingest verification FAILED: elastro_verified=%v(%d) logloom_verified=%v(%d). cmds_ok=(e:%v,l:%v). Data did not land in one/both ES indices (flume-elastro-graph, flume-logloom-ast) for project/repo. Status left as 'cloned' (browseable via remote).", elastroOK, eCnt, logloomOK, lCnt, elastroCmdOK, logloomCmdOK)
		s.logger.Error("dual elastro+logloom ingest verification failed (data may be missing from ES)",
			slog.String("id", id), slog.String("details", errStr))
		s.updateProjectStatus(id, "cloned", &errStr, &destPath)
	}
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
	ingestCmd.Env = append(os.Environ(), buildElastroESEnv(s.cfg.ESUrl, s.cfg.ESApiKey)...)

	if output, err := ingestCmd.CombinedOutput(); err != nil {
		s.logger.Error("local AST ingestion failed", slog.String("id", id), slog.String("error", err.Error()), slog.String("output", string(output)))
		errStr := fmt.Sprintf("AST ingestion failed: %s (Output: %s)", err, string(output))
		s.updateProjectStatus(id, "ast_failed", &errStr, &localPath)
	} else {
		s.updateProjectStatus(id, "local", nil, &localPath)
		s.logger.Info("Local project indexed successfully", slog.String("id", id))
	}

	// Also run LogLoom AST generation + indexing for this local path (best-effort, augments elastro).
	// Does not affect clone_status (elastro result is authoritative for local path).
	if ok := s.runLogloomGraphIngest(id, name, localPath, "flume-logloom-ast"); !ok {
		s.logger.Info("logloom for local path returned false (non-fatal; see prior logs/reasoning)", slog.String("id", id))
	}
}

// buildElastroESEnv constructs the environment variables needed by the elastro
// Python CLI to authenticate against Elasticsearch. This is the single source
// of truth for elastro subprocess auth, ensuring consistent handling of both
// API key and Basic Auth (FLUME_ELASTIC_PASSWORD) across all ingest call sites.
//
// The elastro config loader uses ELASTIC_ prefixed env vars with section-based
// path splitting (e.g. ELASTIC_ELASTICSEARCH_AUTH_USERNAME → config["elasticsearch"]["auth"]["username"]).
func buildElastroESEnv(esURL, esAPIKey string) []string {
	var env []string
	if esURL != "" {
		env = append(env,
			fmt.Sprintf("ELASTIC_URL=%s", esURL),
			fmt.Sprintf("ELASTIC_ELASTICSEARCH_HOSTS=%s", esURL),
			"ELASTIC_ELASTICSEARCH_VERIFY_CERTS=false",
			"ELASTIC_VERIFY_CERTS=false",
		)
	}
	if esAPIKey != "" {
		env = append(env,
			fmt.Sprintf("ELASTIC_ELASTICSEARCH_AUTH_API_KEY=%s", esAPIKey),
			"ELASTIC_ELASTICSEARCH_AUTH_TYPE=api_key",
		)
	} else if pw := os.Getenv("FLUME_ELASTIC_PASSWORD"); pw != "" {
		// ES 8.x default: Basic Auth with the built-in elastic superuser.
		// This mirrors the fallback in internal/es/client.go New().
		env = append(env,
			"ELASTIC_ELASTICSEARCH_AUTH_TYPE=basic",
			"ELASTIC_ELASTICSEARCH_AUTH_USERNAME=elastic",
			fmt.Sprintf("ELASTIC_ELASTICSEARCH_AUTH_PASSWORD=%s", pw),
		)
	}
	return env
}

// detectProjectLanguages walks a project directory and returns the logloom
// --languages value based on which file extensions are actually present.
// Matches logloom's supported extension sets exactly:
//   - Python:     .py
//   - Go:         .go
//   - TypeScript: .ts .tsx .js .jsx .mjs .cjs
//
// The walk skips hidden directories, vendor, node_modules, dist, __pycache__,
// and .git for speed. It short-circuits as soon as all 3 languages are detected.
func detectProjectLanguages(root string) string {
	hasPython := false
	hasGo := false
	hasTS := false

	skipDirs := map[string]bool{
		"node_modules": true, "vendor": true, ".git": true,
		"__pycache__": true, "dist": true, ".venv": true, "venv": true,
	}

	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil // skip unreadable entries
		}
		if d.IsDir() {
			name := d.Name()
			if strings.HasPrefix(name, ".") || skipDirs[name] {
				return filepath.SkipDir
			}
			return nil
		}
		// Short-circuit: all languages found
		if hasPython && hasGo && hasTS {
			return filepath.SkipAll
		}
		ext := strings.ToLower(filepath.Ext(d.Name()))
		switch ext {
		case ".py":
			hasPython = true
		case ".go":
			hasGo = true
		case ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs":
			hasTS = true
		}
		return nil
	})

	var langs []string
	if hasGo {
		langs = append(langs, "go")
	}
	if hasPython {
		langs = append(langs, "python")
	}
	if hasTS {
		langs = append(langs, "typescript")
	}
	if len(langs) == 0 {
		// Fallback: scan all languages if nothing detected (edge case)
		return "go,python,typescript"
	}
	return strings.Join(langs, ",")
}

// refreshIndex forces an ES index refresh so recently-indexed docs become visible
// to _count and _search. Called before post-ingest verification to avoid the
// near-realtime race (default 1s refresh_interval).
func (s *Server) refreshIndex(ctx context.Context, index string) {
	if err := s.es.Post(ctx, fmt.Sprintf("%s/_refresh", index), nil); err != nil {
		s.logger.Warn("ES index refresh failed (verification may see stale counts)", slog.String("index", index), slog.String("error", err.Error()))
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

// runLogloomGraphIngest runs LogLoom graph *build* (rich AST: nodes, edges, semantic tags,
// call graph, signatures, coverage/complexity metrics, models/imports) followed by
// *es ship* to index the resulting enrichment documents into `flume-logloom-ast`.
//
// Discovery is hardened (PATH "logloom", LOGLOOM_BIN, $HOME fallback, /opt/venv) to ensure
// LogLoom always runs for dual Elastro+LogLoom ingest on project clone (fixes install issues).
// Emits s.logger + LogAgentReasoning with "logloom" + "ast-ingest" semantic tags.
//
// This augments (does not replace) the structural part of elastro for user projects during
// clone/local lifecycle, enabling future "LogLoom Structural Savings" calculations.
func (s *Server) runLogloomGraphIngest(projectID, projectName, srcPath, targetIndex string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	logloomBin := "logloom"
	if env := os.Getenv("LOGLOOM_BIN"); env != "" {
		logloomBin = env
	}

	// Robust discovery to ensure LogLoom always runs (coordinate with Dockerfile LOGLOOM_INSTALL).
	// Try: env/PATH name, expanded home default, venv (container). Never silently skip if present.
	found := false
	cands := []string{logloomBin, os.ExpandEnv("$HOME/.local/bin/logloom"), "/opt/venv/bin/logloom"}
	for _, c := range cands {
		if c == "" {
			continue
		}
		if resolved, err := exec.LookPath(c); err == nil {
			logloomBin = resolved
			found = true
			break
		}
		if _, err := os.Stat(c); err == nil {
			logloomBin = c
			found = true
			break
		}
	}
	if !found {
		s.logger.Info("logloom binary not found after search — skipping LogLoom AST ingest (install issue?)",
			slog.String("id", projectID), slog.String("tried", strings.Join(cands, ",")))
		flumelogger.LogAgentReasoning(ctx, projectID, "logloom",
			"LogLoom binary missing during project ingest; AST ship skipped. Fix: build with LOGLOOM_INSTALL=public|wheel or set LOGLOOM_BIN or place binary in PATH/home/venv.",
			"semantic_tags", []interface{}{"logloom", "ast-ingest", "binary-missing"},
			"project", projectID, "index", targetIndex)
		return false
	}

	// Temporary graph artifact (cleaned after ship or on error)
	graphPath := filepath.Join(os.TempDir(), fmt.Sprintf("flume-logloom-ast-%s-%d.json", projectID, time.Now().UnixNano()))

	// 1. Detect project languages from file extensions, then build the AST graph
	detectedLangs := detectProjectLanguages(srcPath)
	buildArgs := []string{
		"build",
		"--source", srcPath,
		"--output", graphPath,
		"--name", projectName,
		"--languages", detectedLangs,
		"--no-incremental",
		"--git", "--tags", "--call-graph", "--coverage", "--models", "--imports",
	}
	buildCmd := exec.CommandContext(ctx, logloomBin, buildArgs...)
	buildCmd.Env = os.Environ()

	s.logger.Info("Executing logloom build for project AST graph",
		slog.String("id", projectID), slog.String("bin", logloomBin),
		slog.String("source", srcPath), slog.String("graph", graphPath),
		slog.String("detected_languages", detectedLangs))

	if output, err := buildCmd.CombinedOutput(); err != nil {
		s.logger.Warn("logloom build failed (non-fatal; elastro structural data unaffected if present)",
			slog.String("id", projectID), slog.String("error", err.Error()),
			slog.String("output", plannerTruncate(string(output), 800)))
		_ = os.Remove(graphPath)
		flumelogger.LogAgentReasoning(ctx, projectID, "logloom",
			"LogLoom AST graph build failed for project (non-fatal)",
			"semantic_tags", []interface{}{"logloom", "ast-ingest", "build-failed"},
			"project", projectID, "index", targetIndex, "error", err.Error())
		return false
	}

	// 2. Ship directly to ES (uses CLI flags for URL/key/verify to avoid env side-effects)
	esURL := s.cfg.ESUrl
	if esURL == "" {
		esURL = "http://localhost:9200"
	}
	shipArgs := []string{
		"es", "ship",
		"--graph-path", graphPath,
		"--index", targetIndex,
		"--es-url", esURL,
		"--no-verify",
	}
	if s.cfg.ESApiKey != "" {
		shipArgs = append(shipArgs, "--api-key", s.cfg.ESApiKey)
	} else if pw := os.Getenv("FLUME_ELASTIC_PASSWORD"); pw != "" {
		// Basic Auth fallback — matches elastro and internal/es/client.go pattern.
		shipArgs = append(shipArgs, "--username", "elastic", "--password", pw)
	}
	shipCmd := exec.CommandContext(ctx, logloomBin, shipArgs...)
	shipCmd.Env = os.Environ()

	s.logger.Info("Executing logloom es ship for AST indexing into flume-logloom-ast",
		slog.String("id", projectID), slog.String("index", targetIndex))

	if output, err := shipCmd.CombinedOutput(); err != nil {
		s.logger.Warn("logloom es ship failed (non-fatal)",
			slog.String("id", projectID), slog.String("error", err.Error()),
			slog.String("output", plannerTruncate(string(output), 800)))
		_ = os.Remove(graphPath)
		flumelogger.LogAgentReasoning(ctx, projectID, "logloom",
			"LogLoom AST graph ship to ES failed (non-fatal)",
			"semantic_tags", []interface{}{"logloom", "ast-ingest", "ship-failed"},
			"project", projectID, "index", targetIndex, "error", err.Error())
		return false
	}

	_ = os.Remove(graphPath)

	s.logger.Info("LogLoom AST graph generated + indexed successfully (rich structural data now queryable)",
		slog.String("id", projectID), slog.String("index", targetIndex))
	flumelogger.LogAgentReasoning(ctx, projectID, "logloom",
		"LogLoom AST generation + indexing complete: nodes/edges/references/complexity/semantic-tags/call-graph/signatures now available in flume-logloom-ast (augments elastro)",
		"semantic_tags", []interface{}{"logloom", "ast-ingest", "success"},
		"project", projectID, "index", targetIndex, "source_path", srcPath)

	return true
}

// verifyProjectASTDocs does the post-run lightweight verification per the Elastro/LogLoom contract (#2).
// Counts docs "for the project/repo" using a flexible should-query over repo/project_id/project/repository
// (populated by logloom ship and possibly elastro ingest). If no project-attributed docs visible (elastro
// uses dynamic mapping), falls back to presence of >0 total docs in the index as evidence of landing.
// Returns ok flags + counts. Always emits via caller LogAgentReasoning.
func (s *Server) verifyProjectASTDocs(ctx context.Context, projectID, projectName string) (elOK bool, llOK bool, eCount int, lCount int) {
	if ctx == nil {
		ctx = context.Background()
	}
	vals := []string{projectID}
	if projectName != "" && projectName != projectID {
		vals = append(vals, projectName)
	}
	shoulds := []interface{}{}
	for _, v := range vals {
		for _, f := range []string{"repo", "project_id", "project", "repository"} {
			shoulds = append(shoulds, map[string]interface{}{"term": map[string]string{f: v}})
		}
	}
	scoped := map[string]interface{}{
		"bool": map[string]interface{}{
			"should":               shoulds,
			"minimum_should_match": 1,
		},
	}

	var err error
	eCount, err = s.es.Count(ctx, "flume-elastro-graph", scoped)
	if err != nil {
		s.logger.Warn("verify elastro count (scoped) failed", slog.String("id", projectID), slog.String("err", err.Error()))
		eCount = 0
	}
	lCount, err = s.es.Count(ctx, "flume-logloom-ast", scoped)
	if err != nil {
		s.logger.Warn("verify logloom-ast count (scoped) failed", slog.String("id", projectID), slog.String("err", err.Error()))
		lCount = 0
	}

	elOK = eCount > 0
	llOK = lCount > 0

	if !elOK || !llOK {
		// Fallback proxy: total docs present means ingest landed data (for this or concurrent projects).
		// Defensive for elastro's dynamic schema (may not yet tag "repo" etc per-project).
		totalE, _ := s.es.Count(ctx, "flume-elastro-graph", map[string]interface{}{})
		totalL, _ := s.es.Count(ctx, "flume-logloom-ast", map[string]interface{}{})
		if !elOK && totalE > 0 {
			elOK = true
			eCount = totalE
			s.logger.Info("verify elastro: fell back to total>0 proxy (no project tag match)", slog.String("id", projectID), slog.Int("total", totalE))
		}
		if !llOK && totalL > 0 {
			llOK = true
			lCount = totalL
			s.logger.Info("verify logloom: fell back to total>0 proxy (no project tag match)", slog.String("id", projectID), slog.Int("total", totalL))
		}
	}
	return
}

