// api_repos.go — Repository browsing: branches, tree, file, diff.
//
// Direct port of Python: src/dashboard/api/repos.py (3 AST nodes).
// These endpoints support both local repos (git subprocess) and remote
// repos (GitHostClient REST API — wired in Phase 3).
package dashboard

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/Fremen-Labs/flume/internal/git"
)

// ─── GET /api/repos/{project_id}/branches ───────────────────────────────────

// handleRepoBranches returns branch list for a project.
// Derived from Python: api/repos.py api_repo_branches().
func (s *Server) handleRepoBranches(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	projectID := r.PathValue("project_id")

	src, err := s.es.GetDoc(ctx, projectsIndex, projectID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch project")
		return
	}
	if src == nil {
		writeError(w, http.StatusNotFound, fmt.Sprintf("Project '%s' not found", projectID))
		return
	}

	var proj map[string]interface{}
	if err := unmarshalRaw(src, &proj); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to parse project document")
		return
	}

	cs, _ := proj["clone_status"].(string)
	if cs == "" {
		cs = "no_repo"
	}
	var cloneErrorVal string
	if ce, ok := proj["clone_error"].(string); ok {
		cloneErrorVal = ce
	}

	// In-flight states: clone/ingest still running — send polling hint to UI
	if cs == "cloning" || cs == "indexing" || cs == "pending" {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"gitAvailable": false,
			"cloneStatus":  cs,
			"cloneError":   nil,
			"branches":     []interface{}{},
			"message":      "Repository is being cloned in the background…",
		})
		return
	}

	// Local clone precedence check
	localPath, _ := proj["path"].(string)
	hasLocalClone := false
	if localPath != "" {
		if _, err := os.Stat(filepath.Join(localPath, ".git")); err == nil {
			hasLocalClone = true
		}
	}

	// Remote repo path: use GitHostClient REST API
	repoURL, _ := proj["repoUrl"].(string)
	isRemote := repoURL != "" && (strings.Contains(repoURL, "github.com") || strings.Contains(repoURL, "dev.azure.com") || strings.Contains(repoURL, "visualstudio.com") || strings.HasPrefix(repoURL, "http://") || strings.HasPrefix(repoURL, "https://"))

	if !hasLocalClone && (cs == "indexed" || cs == "cloned") && isRemote {
		client, err := git.GetClient(ctx, proj)
		if err != nil {
			if _, ok := err.(*git.AuthError); ok {
				writeJSON(w, http.StatusUnauthorized, map[string]interface{}{
					"gitAvailable": false,
					"error":        "No credentials configured. Add a PAT in Settings → Repositories.",
					"detail":       err.Error(),
				})
				return
			}
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		branches, err := client.GetBranches(ctx)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		defaultBranch, err := client.GetDefaultBranch(ctx)
		if err != nil {
			defaultBranch = "main"
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{
			"gitAvailable": true,
			"branches":     branches,
			"default":      defaultBranch,
		})
		return
	}

	// Local repo path: original git subprocess
	if localPath == "" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "No local repo path available. This project has no local clone.",
		})
		return
	}

	if !hasLocalClone {
		var message string
		if cs == "failed" {
			message = fmt.Sprintf("Clone failed: %s", cloneErrorVal)
		} else {
			message = "This project is not a Git repository. Add one by creating the project with a clone URL or run \"git init\" in the project folder."
		}
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"gitAvailable": false,
			"cloneStatus":  cs,
			"cloneError":   cloneErrorVal,
			"branches":     []interface{}{},
			"message":      message,
		})
		return
	}

	// Run git branch subprocess
	rc, stdout, stderr, _ := runCmd(ctx, localPath, "git", "branch", "-a", "--format=%(refname:short)")
	if rc == 128 {
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{
			"error": "git branch exited 128: Repository refs may be corrupt.",
		})
		return
	}
	if rc != 0 {
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{
			"error": fmt.Sprintf("git branch failed: %s", stderr),
		})
		return
	}

	allBranches := strings.Split(stdout, "\n")
	seen := make(map[string]bool)
	var branches []string
	for _, b := range allBranches {
		b = strings.TrimSpace(b)
		if b == "" {
			continue
		}
		name := b
		if strings.HasPrefix(b, "origin/") {
			name = strings.TrimPrefix(b, "origin/")
		}
		if name != "HEAD" && !seen[name] {
			seen[name] = true
			branches = append(branches, name)
		}
	}

	// Resolve default branch
	defaultBranch := "main"
	if gitflow, ok := proj["gitflow"].(map[string]interface{}); ok {
		if df, ok := gitflow["defaultBranch"].(string); ok && df != "" {
			defaultBranch = df
		}
	}
	if defaultBranch == "main" {
		rc, stdout, _, _ := runCmd(ctx, localPath, "git", "symbolic-ref", "refs/remotes/origin/HEAD")
		if rc == 0 {
			stdout = strings.TrimSpace(stdout)
			parts := strings.Split(stdout, "/")
			if len(parts) > 0 {
				defaultBranch = parts[len(parts)-1]
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"gitAvailable": true,
		"branches":     branches,
		"default":      defaultBranch,
	})
}

// ─── GET /api/repos/{project_id}/tree ───────────────────────────────────────

// handleRepoTree returns the file tree for a branch.
// Derived from Python: api/repos.py api_repo_tree().
func (s *Server) handleRepoTree(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	projectID := r.PathValue("project_id")
	branch := r.URL.Query().Get("branch")

	src, err := s.es.GetDoc(ctx, projectsIndex, projectID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch project")
		return
	}
	if src == nil {
		writeError(w, http.StatusNotFound, fmt.Sprintf("Project '%s' not found", projectID))
		return
	}

	var proj map[string]interface{}
	if err := unmarshalRaw(src, &proj); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to parse project document")
		return
	}

	cs, _ := proj["clone_status"].(string)
	if cs == "cloning" || cs == "indexing" || cs == "pending" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "Repository is currently being cloned.",
		})
		return
	}

	// Local clone precedence check
	localPath, _ := proj["path"].(string)
	hasLocalClone := false
	if localPath != "" {
		if _, err := os.Stat(filepath.Join(localPath, ".git")); err == nil {
			hasLocalClone = true
		}
	}

	// Remote repo path: use GitHostClient REST API
	repoURL, _ := proj["repoUrl"].(string)
	isRemote := repoURL != "" && (strings.Contains(repoURL, "github.com") || strings.Contains(repoURL, "dev.azure.com") || strings.Contains(repoURL, "visualstudio.com") || strings.HasPrefix(repoURL, "http://") || strings.HasPrefix(repoURL, "https://"))

	if !hasLocalClone && (cs == "indexed" || cs == "cloned") && isRemote {
		client, err := git.GetClient(ctx, proj)
		if err != nil {
			if _, ok := err.(*git.AuthError); ok {
				writeJSON(w, http.StatusUnauthorized, map[string]interface{}{
					"error":  "No credentials — add a PAT in Settings → Repositories.",
					"detail": err.Error(),
				})
				return
			}
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		if branch == "" {
			branch, err = client.GetDefaultBranch(ctx)
			if err != nil {
				branch = "main"
			}
		}

		entries, err := client.GetTree(ctx, branch)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{
			"branch":  branch,
			"entries": entries,
		})
		return
	}

	// Local repo path: git subprocess
	if localPath == "" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "No local repo path. This project has no local clone.",
		})
		return
	}

	if !hasLocalClone {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "Not a git repository",
		})
		return
	}

	if branch == "" {
		defaultBranch := "main"
		if gitflow, ok := proj["gitflow"].(map[string]interface{}); ok {
			if df, ok := gitflow["defaultBranch"].(string); ok && df != "" {
				defaultBranch = df
			}
		}
		if defaultBranch == "main" {
			rc, stdout, _, _ := runCmd(ctx, localPath, "git", "symbolic-ref", "refs/remotes/origin/HEAD")
			if rc == 0 {
				stdout = strings.TrimSpace(stdout)
				parts := strings.Split(stdout, "/")
				if len(parts) > 0 {
					defaultBranch = parts[len(parts)-1]
				}
			}
		}
		branch = defaultBranch
	}

	rc, stdout, stderr, _ := runCmd(ctx, localPath, "git", "ls-tree", "-r", "--long", "--full-tree", branch)
	if rc != 0 {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": fmt.Sprintf("Could not read tree for branch '%s': %s", branch, stderr),
		})
		return
	}

	var entries []git.TreeEntry
	lines := strings.Split(stdout, "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.SplitN(line, "\t", 2)
		if len(parts) < 2 {
			continue
		}
		meta := parts[0]
		metaParts := strings.Fields(meta)
		if len(metaParts) < 3 {
			continue
		}
		objType := metaParts[1]
		size := "-"
		filePath := ""
		if len(parts) == 2 {
			filePath = strings.TrimSpace(parts[1])
		}
		if len(metaParts) >= 4 {
			size = strings.TrimSpace(metaParts[3])
		} else {
			size = strings.TrimSpace(metaParts[len(metaParts)-1])
		}

		entryType := "tree"
		if objType == "blob" {
			entryType = "blob"
		}
		entries = append(entries, git.TreeEntry{
			Path: filePath,
			Type: entryType,
			Size: size,
		})
	}

	dirsSeen := make(map[string]bool)
	var dirEntries []git.TreeEntry
	for _, e := range entries {
		partsPath := strings.Split(e.Path, "/")
		for depth := 1; depth < len(partsPath); depth++ {
			dirPath := strings.Join(partsPath[:depth], "/")
			if !dirsSeen[dirPath] {
				dirsSeen[dirPath] = true
				dirEntries = append(dirEntries, git.TreeEntry{
					Path: dirPath,
					Type: "tree",
					Size: "-",
				})
			}
		}
	}

	allEntries := append(entries, dirEntries...)
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"branch":  branch,
		"entries": allEntries,
	})
}

// ─── GET /api/repos/{project_id}/file ───────────────────────────────────────

// handleRepoFile returns file content.
// Derived from Python: api/repos.py api_repo_file().
func (s *Server) handleRepoFile(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	projectID := r.PathValue("project_id")
	path := r.URL.Query().Get("path")
	branch := r.URL.Query().Get("branch")

	if path == "" {
		writeError(w, http.StatusBadRequest, "path is required")
		return
	}

	src, err := s.es.GetDoc(ctx, projectsIndex, projectID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch project")
		return
	}
	if src == nil {
		writeError(w, http.StatusNotFound, fmt.Sprintf("Project '%s' not found", projectID))
		return
	}

	var proj map[string]interface{}
	if err := unmarshalRaw(src, &proj); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to parse project document")
		return
	}

	cs, _ := proj["clone_status"].(string)

	// Sanitise path — prevent directory traversal
	cleanPath := strings.TrimLeft(path, "/")
	parts := strings.Split(cleanPath, "/")
	for _, p := range parts {
		if p == ".." {
			writeError(w, http.StatusBadRequest, "Invalid path")
			return
		}
	}

	// Local clone precedence check
	localPath, _ := proj["path"].(string)
	hasLocalClone := false
	if localPath != "" {
		if _, err := os.Stat(filepath.Join(localPath, ".git")); err == nil {
			hasLocalClone = true
		}
	}

	// Remote repo path: use GitHostClient REST API
	repoURL, _ := proj["repoUrl"].(string)
	isRemote := repoURL != "" && (strings.Contains(repoURL, "github.com") || strings.Contains(repoURL, "dev.azure.com") || strings.Contains(repoURL, "visualstudio.com") || strings.HasPrefix(repoURL, "http://") || strings.HasPrefix(repoURL, "https://"))

	if !hasLocalClone && (cs == "indexed" || cs == "cloned") && isRemote {
		client, err := git.GetClient(ctx, proj)
		if err != nil {
			if _, ok := err.(*git.AuthError); ok {
				writeJSON(w, http.StatusUnauthorized, map[string]interface{}{
					"error":  "No credentials — add a PAT in Settings → Repositories.",
					"detail": err.Error(),
				})
				return
			}
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		if branch == "" {
			branch, err = client.GetDefaultBranch(ctx)
			if err != nil {
				branch = "main"
			}
		}

		contentBytes, err := client.GetFile(ctx, cleanPath, branch)
		if err != nil {
			if _, ok := err.(*git.NotFoundError); ok {
				writeJSON(w, http.StatusNotFound, map[string]interface{}{
					"error": fmt.Sprintf("File '%s' not found on branch '%s'", cleanPath, branch),
				})
				return
			}
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		resp := makeFileResponse(contentBytes, cleanPath)
		writeJSON(w, http.StatusOK, resp)
		return
	}

	// Local repo path: git subprocess
	if localPath == "" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "No local repo path. This project has no local clone.",
		})
		return
	}

	if !hasLocalClone {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "Not a git repository",
		})
		return
	}

	if branch == "" {
		defaultBranch := "main"
		if gitflow, ok := proj["gitflow"].(map[string]interface{}); ok {
			if df, ok := gitflow["defaultBranch"].(string); ok && df != "" {
				defaultBranch = df
			}
		}
		if defaultBranch == "main" {
			rc, stdout, _, _ := runCmd(ctx, localPath, "git", "symbolic-ref", "refs/remotes/origin/HEAD")
			if rc == 0 {
				stdout = strings.TrimSpace(stdout)
				parts := strings.Split(stdout, "/")
				if len(parts) > 0 {
					defaultBranch = parts[len(parts)-1]
				}
			}
		}
		branch = defaultBranch
	}

	rc, stdout, stderr, _ := runCmd(ctx, localPath, "git", "show", fmt.Sprintf("%s:%s", branch, cleanPath))
	if rc != 0 {
		if strings.Contains(stderr, "does not exist") || strings.Contains(stderr, "exists on disk") || rc == 128 {
			writeJSON(w, http.StatusNotFound, map[string]interface{}{
				"error": fmt.Sprintf("File '%s' not found on branch '%s'", cleanPath, branch),
			})
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]interface{}{
			"error": fmt.Sprintf("git show failed: %s", stderr),
		})
		return
	}

	resp := makeFileResponse([]byte(stdout), cleanPath)
	writeJSON(w, http.StatusOK, resp)
}

// ─── GET /api/repos/{project_id}/diff ───────────────────────────────────────

// handleRepoDiff returns diff between two branches.
// Derived from Python: api/repos.py api_repo_diff().
func (s *Server) handleRepoDiff(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	projectID := r.PathValue("project_id")
	base := r.URL.Query().Get("base")
	head := r.URL.Query().Get("head")

	if base == "" || head == "" {
		writeError(w, http.StatusBadRequest, "base and head branch parameters are required")
		return
	}

	src, err := s.es.GetDoc(ctx, projectsIndex, projectID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch project")
		return
	}
	if src == nil {
		writeError(w, http.StatusNotFound, fmt.Sprintf("Project '%s' not found", projectID))
		return
	}

	var proj map[string]interface{}
	if err := unmarshalRaw(src, &proj); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to parse project document")
		return
	}

	// AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
	localPath, _ := proj["path"].(string)
	hasLocalClone := false
	if localPath != "" {
		if _, err := os.Stat(filepath.Join(localPath, ".git")); err == nil {
			hasLocalClone = true
		}
	}

	if base == head {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"base":      base,
			"head":      head,
			"files":     []interface{}{},
			"diff":      "",
			"truncated": false,
			"identical": true,
		})
		return
	}

	// Remote: use GitHostClient
	repoURL, _ := proj["repoUrl"].(string)
	cs, _ := proj["clone_status"].(string)
	isRemote := repoURL != "" && (strings.Contains(repoURL, "github.com") || strings.Contains(repoURL, "dev.azure.com") || strings.Contains(repoURL, "visualstudio.com") || strings.HasPrefix(repoURL, "http://") || strings.HasPrefix(repoURL, "https://"))

	if !hasLocalClone && (cs == "indexed" || cs == "cloned") && isRemote {
		client, err := git.GetClient(ctx, proj)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		result, err := client.GetDiff(ctx, base, head)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}

		identical := result.Diff == "" && len(result.Files) == 0
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"base":      result.Base,
			"head":      result.Head,
			"files":     result.Files,
			"diff":      result.Diff,
			"truncated": result.Truncated,
			"identical": identical,
		})
		return
	}

	if localPath == "" {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "No local repo path. Diff requires a locally-mounted repo.",
		})
		return
	}

	if !hasLocalClone {
		writeJSON(w, http.StatusBadRequest, map[string]interface{}{
			"error": "Not a git repository",
		})
		return
	}

	maxDiffLines := 3000
	ref := fmt.Sprintf("%s...%s", base, head)

	// Best-effort fetch
	_, _, _, _ = runCmd(ctx, localPath, "git", "fetch", "origin", "--quiet")

	var files []git.DiffFile
	rc, stdout, _, _ := runCmd(ctx, localPath, "git", "diff", "--stat", "--stat-width=1000", ref)
	if rc == 0 {
		lines := strings.Split(stdout, "\n")
		for _, line := range lines {
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			parts := strings.Split(line, "|")
			if len(parts) != 2 {
				continue
			}
			pathPart := strings.TrimSpace(parts[0])
			changePart := strings.TrimSpace(parts[1])
			if pathPart == "" || strings.HasPrefix(pathPart, "changed") {
				continue
			}
			ins := 0
			dels := 0
			for _, char := range changePart {
				if char == '+' {
					ins++
				} else if char == '-' {
					dels++
				}
			}
			files = append(files, git.DiffFile{
				Path:       pathPart,
				Insertions: ins,
				Deletions:  dels,
				Status:     "modified",
			})
		}
	}

	diffText := ""
	truncated := false
	rcDiff, stdoutDiff, _, _ := runCmd(ctx, localPath, "git", "diff", ref)
	if rcDiff == 0 {
		diffLines := strings.Split(stdoutDiff, "\n")
		if len(diffLines) > maxDiffLines {
			diffText = strings.Join(diffLines[:maxDiffLines], "\n")
			truncated = true
		} else {
			diffText = stdoutDiff
		}
	}

	identical := diffText == "" && len(files) == 0
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"base":      base,
		"head":      head,
		"files":     files,
		"diff":      diffText,
		"truncated": truncated,
		"identical": identical,
	})
}

// ─── Helpers ────────────────────────────────────────────────────────────────

func runCmd(ctx context.Context, dir string, name string, args ...string) (int, string, string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	if dir != "" {
		cmd.Dir = dir
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	rc := 0
	if err != nil {
		if exitError, ok := err.(*exec.ExitError); ok {
			rc = exitError.ExitCode()
		} else {
			rc = -1
		}
	}
	return rc, stdout.String(), stderr.String(), err
}

func makeFileResponse(contentBytes []byte, path string) map[string]interface{} {
	limit := 8192
	if len(contentBytes) < limit {
		limit = len(contentBytes)
	}
	sample := contentBytes[:limit]
	isBinary := bytes.Contains(sample, []byte{0})

	if isBinary {
		return map[string]interface{}{
			"binary":  true,
			"content": nil,
			"size":    len(contentBytes),
		}
	}

	return map[string]interface{}{
		"binary":  false,
		"content": string(contentBytes),
		"size":    len(contentBytes),
	}
}
