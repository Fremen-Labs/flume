// Package git provides provider-agnostic Git remote API clients for Flume.
//
// Direct port of Python:
//   - utils/git_host_client.py (16 AST nodes) — GitHub + Azure DevOps REST clients
//   - utils/git_credentials.py (4 AST nodes) — credential embedding + repo detection
//
// Replaces local `git -C <path>` subprocess calls in dashboard endpoints with
// REST API calls to GitHub or Azure DevOps. This eliminates the pod-local clone
// dependency that was the root cause of AP-4 (K8s readiness issue #177).
//
// Supported providers:
//
//	GitHub       → GitHub REST API v3 (api.github.com)
//	Azure DevOps → ADO Git REST API 7.1 (dev.azure.com)
package git

import (
	"bytes"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// ─── Errors ─────────────────────────────────────────────────────────────────

// HostError is the base error for all GitHostClient errors.
type HostError struct {
	Message string
	Code    int
}

func (e *HostError) Error() string { return e.Message }

// AuthError is returned when credentials are missing or rejected (401/403).
type AuthError struct{ HostError }

// NotFoundError is returned when a resource does not exist (404).
type NotFoundError struct{ HostError }

// ─── Interface ──────────────────────────────────────────────────────────────

// TreeEntry represents a file or directory in a repository tree.
type TreeEntry struct {
	Path string `json:"path"`
	Type string `json:"type"` // "blob" or "tree"
	Size string `json:"size"`
}

// DiffResult holds a diff comparison between two refs.
type DiffResult struct {
	Base      string     `json:"base"`
	Head      string     `json:"head"`
	Files     []DiffFile `json:"files"`
	Diff      string     `json:"diff"`
	Truncated bool       `json:"truncated"`
	Error     string     `json:"error,omitempty"`
}

// DiffFile represents a changed file in a diff.
type DiffFile struct {
	Path       string `json:"path"`
	Insertions int    `json:"insertions"`
	Deletions  int    `json:"deletions"`
	Status     string `json:"status"`
}

// Commit represents a git commit.
type Commit struct {
	SHA     string `json:"sha"`
	Author  string `json:"author"`
	Date    string `json:"date"`
	Message string `json:"message"`
}

// PRResult holds the result of a pull request creation.
type PRResult struct {
	PRURL    string `json:"pr_url"`
	PRNumber int    `json:"pr_number,omitempty"`
}

// HostClient is the abstract interface for a remote git host.
// Derived from Python: GitHostClient(ABC).
type HostClient interface {
	GetBranches() ([]string, error)
	GetDefaultBranch() (string, error)
	GetTree(branch string) ([]TreeEntry, error)
	GetFile(path, branch string) ([]byte, error)
	GetDiff(base, head string) (*DiffResult, error)
	GetCommits(branch, base string) ([]Commit, error)
	EnsureIntegrationBranch(branchName string) (bool, error)
	CreatePullRequest(title, body, head, base string) (*PRResult, error)
}

// ─── Shared HTTP helpers ────────────────────────────────────────────────────

var insecureTransport = &http.Transport{
	TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec
}

var httpClient = &http.Client{
	Timeout:   20 * time.Second,
	Transport: insecureTransport,
}

// httpJSON performs an authenticated JSON HTTP request.
// Derived from Python: _http_json().
func httpJSON(reqURL, method, token string, body interface{}, extraHeaders map[string]string) (interface{}, error) {
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("git: marshal failed: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	}

	req, err := http.NewRequest(method, reqURL, bodyReader)
	if err != nil {
		return nil, &HostError{Message: fmt.Sprintf("request build failed for %s: %v", reqURL, err)}
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range extraHeaders {
		req.Header.Set(k, v)
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		slog.Error("git API request failed", slog.String("url", reqURL), slog.String("error", err.Error()))
		return nil, &HostError{Message: fmt.Sprintf("request failed for %s: %v", reqURL, err)}
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	switch {
	case resp.StatusCode == 401 || resp.StatusCode == 403:
		slog.Warn("git API auth failure", slog.String("url", reqURL), slog.Int("status", resp.StatusCode))
		return nil, &AuthError{HostError{
			Message: fmt.Sprintf("authentication failed (%d) for %s: %s", resp.StatusCode, reqURL, truncate(string(respBody), 300)),
			Code:    resp.StatusCode,
		}}
	case resp.StatusCode == 404:
		return nil, &NotFoundError{HostError{
			Message: fmt.Sprintf("not found (%d) for %s: %s", resp.StatusCode, reqURL, truncate(string(respBody), 300)),
			Code:    resp.StatusCode,
		}}
	case resp.StatusCode >= 300:
		slog.Error("git API HTTP error", slog.String("url", reqURL), slog.Int("status", resp.StatusCode))
		return nil, &HostError{
			Message: fmt.Sprintf("HTTP %d for %s: %s", resp.StatusCode, reqURL, truncate(string(respBody), 300)),
			Code:    resp.StatusCode,
		}
	}

	if len(respBody) == 0 {
		return map[string]interface{}{}, nil
	}

	var result interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("git: response decode failed: %w", err)
	}
	return result, nil
}

// httpRaw returns raw bytes from a URL (for file content).
// Derived from Python: _http_raw().
func httpRaw(reqURL, token string) ([]byte, error) {
	req, err := http.NewRequest(http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, &HostError{Message: fmt.Sprintf("request build failed: %v", err)}
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, &HostError{Message: fmt.Sprintf("request failed for %s: %v", reqURL, err)}
	}
	defer resp.Body.Close()

	if resp.StatusCode == 404 {
		return nil, &NotFoundError{HostError{Message: fmt.Sprintf("file not found: %s", reqURL), Code: 404}}
	}
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return nil, &AuthError{HostError{Message: fmt.Sprintf("auth failed (%d): %s", resp.StatusCode, reqURL), Code: resp.StatusCode}}
	}
	if resp.StatusCode >= 300 {
		return nil, &HostError{Message: fmt.Sprintf("HTTP %d: %s", resp.StatusCode, reqURL), Code: resp.StatusCode}
	}

	return io.ReadAll(resp.Body)
}

// ─── GitHub ─────────────────────────────────────────────────────────────────

// GitHubClient implements HostClient for GitHub REST API v3.
// Derived from Python: GitHubClient class.
type GitHubClient struct {
	Owner         string
	Repo          string
	Token         string
	defaultBranch string
}

const githubBase = "https://api.github.com"

func (g *GitHubClient) apiURL(path string) string {
	clean := strings.TrimLeft(path, "/")
	base := fmt.Sprintf("%s/repos/%s/%s", githubBase, g.Owner, g.Repo)
	if clean == "" {
		return base
	}
	return base + "/" + clean
}

func (g *GitHubClient) ghHeaders() map[string]string {
	return map[string]string{"X-GitHub-Api-Version": "2022-11-28"}
}

func (g *GitHubClient) get(path string, params map[string]string) (interface{}, error) {
	u := g.apiURL(path)
	if len(params) > 0 {
		v := url.Values{}
		for k, val := range params {
			v.Set(k, val)
		}
		u += "?" + v.Encode()
	}
	return httpJSON(u, http.MethodGet, g.Token, nil, g.ghHeaders())
}

func (g *GitHubClient) post(path string, body interface{}) (interface{}, error) {
	return httpJSON(g.apiURL(path), http.MethodPost, g.Token, body, g.ghHeaders())
}

func (g *GitHubClient) put(path string, body interface{}) (interface{}, error) {
	return httpJSON(g.apiURL(path), http.MethodPut, g.Token, body, g.ghHeaders())
}

func (g *GitHubClient) delete(path string) (interface{}, error) {
	return httpJSON(g.apiURL(path), http.MethodDelete, g.Token, nil, g.ghHeaders())
}

// GetDefaultBranch returns the repository's default branch.
func (g *GitHubClient) GetDefaultBranch() (string, error) {
	if g.defaultBranch != "" {
		return g.defaultBranch, nil
	}
	data, err := g.get("", nil)
	if err != nil {
		return "main", err
	}
	m, _ := data.(map[string]interface{})
	if branch, ok := m["default_branch"].(string); ok {
		g.defaultBranch = branch
		return branch, nil
	}
	g.defaultBranch = "main"
	return "main", nil
}

// GetBranches returns all branch names (paginated, up to 500).
func (g *GitHubClient) GetBranches() ([]string, error) {
	var branches []string
	for page := 1; page <= 5; page++ {
		data, err := g.get("branches", map[string]string{
			"per_page": "100",
			"page":     fmt.Sprintf("%d", page),
		})
		if err != nil {
			return branches, err
		}
		arr, _ := data.([]interface{})
		if len(arr) == 0 {
			break
		}
		for _, b := range arr {
			bm, _ := b.(map[string]interface{})
			if name, ok := bm["name"].(string); ok {
				branches = append(branches, name)
			}
		}
		if len(arr) < 100 {
			break
		}
	}
	return branches, nil
}

// GetTree returns a flat list of all blob and tree entries.
func (g *GitHubClient) GetTree(branch string) ([]TreeEntry, error) {
	if branch == "" {
		var err error
		branch, err = g.GetDefaultBranch()
		if err != nil {
			return nil, err
		}
	}
	data, err := g.get(fmt.Sprintf("git/trees/%s", branch), map[string]string{"recursive": "1"})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	tree, _ := m["tree"].([]interface{})
	var entries []TreeEntry
	for _, item := range tree {
		im, _ := item.(map[string]interface{})
		entryType := "tree"
		if im["type"] == "blob" {
			entryType = "blob"
		}
		entries = append(entries, TreeEntry{
			Path: strVal(im["path"]),
			Type: entryType,
			Size: fmt.Sprintf("%v", im["size"]),
		})
	}
	return entries, nil
}

// GetFile returns the raw bytes of a file at the given path and branch.
func (g *GitHubClient) GetFile(path, branch string) ([]byte, error) {
	if branch == "" {
		var err error
		branch, err = g.GetDefaultBranch()
		if err != nil {
			return nil, err
		}
	}
	clean := strings.TrimLeft(path, "/")
	data, err := g.get(fmt.Sprintf("contents/%s", clean), map[string]string{"ref": branch})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	contentB64, _ := m["content"].(string)
	if contentB64 == "" {
		// Large file: fall back to download_url
		downloadURL, _ := m["download_url"].(string)
		if downloadURL != "" {
			return httpRaw(downloadURL, g.Token)
		}
		return nil, &NotFoundError{HostError{Message: fmt.Sprintf("no content returned for %s", path)}}
	}
	// Remove newlines GitHub inserts into the base64 block
	cleaned := strings.ReplaceAll(contentB64, "\n", "")
	return base64.StdEncoding.DecodeString(cleaned)
}

// GetDiff returns a diff summary between two refs.
func (g *GitHubClient) GetDiff(base, head string) (*DiffResult, error) {
	const maxDiff = 80_000
	encodedBase := url.PathEscape(base)
	encodedHead := url.PathEscape(head)

	data, err := g.get(fmt.Sprintf("compare/%s...%s", encodedBase, encodedHead), nil)
	if err != nil {
		if _, ok := err.(*NotFoundError); ok {
			return &DiffResult{
				Base:  base,
				Head:  head,
				Error: fmt.Sprintf("branch '%s' not found on remote", head),
			}, nil
		}
		return nil, err
	}

	m, _ := data.(map[string]interface{})
	filesRaw, _ := m["files"].([]interface{})

	var files []DiffFile
	var diffParts []string

	for _, f := range filesRaw {
		fm, _ := f.(map[string]interface{})
		files = append(files, DiffFile{
			Path:       strVal(fm["filename"]),
			Insertions: intVal(fm["additions"]),
			Deletions:  intVal(fm["deletions"]),
			Status:     strVal(fm["status"]),
		})
		if patch, ok := fm["patch"].(string); ok && patch != "" {
			diffParts = append(diffParts,
				fmt.Sprintf("diff --git a/%s b/%s\n%s", fm["filename"], fm["filename"], patch))
		}
	}

	diffText := strings.Join(diffParts, "\n")
	truncated := false
	if len(diffText) > maxDiff {
		diffText = diffText[:maxDiff] + "\n\n... [diff truncated at 80k chars] ..."
		truncated = true
	}

	return &DiffResult{
		Base:      base,
		Head:      head,
		Files:     files,
		Diff:      diffText,
		Truncated: truncated,
	}, nil
}

// GetCommits returns commits on branch not in base.
func (g *GitHubClient) GetCommits(branch, base string) ([]Commit, error) {
	if base == "" {
		var err error
		base, err = g.GetDefaultBranch()
		if err != nil {
			return nil, err
		}
	}
	data, err := g.get(
		fmt.Sprintf("compare/%s...%s", url.PathEscape(base), url.PathEscape(branch)),
		nil,
	)
	if err != nil {
		if _, ok := err.(*NotFoundError); ok {
			return nil, nil
		}
		return nil, err
	}

	m, _ := data.(map[string]interface{})
	commitsRaw, _ := m["commits"].([]interface{})

	var commits []Commit
	for i, c := range commitsRaw {
		if i >= 50 {
			break
		}
		cm, _ := c.(map[string]interface{})
		commitObj, _ := cm["commit"].(map[string]interface{})
		authorObj, _ := commitObj["author"].(map[string]interface{})

		msg := strVal(commitObj["message"])
		if idx := strings.Index(msg, "\n"); idx >= 0 {
			msg = msg[:idx]
		}

		sha := strVal(cm["sha"])
		if len(sha) > 40 {
			sha = sha[:40]
		}

		commits = append(commits, Commit{
			SHA:     sha,
			Author:  strVal(authorObj["name"]),
			Date:    strVal(authorObj["date"]),
			Message: msg,
		})
	}
	return commits, nil
}

// EnsureIntegrationBranch creates the branch at the default tip if absent.
func (g *GitHubClient) EnsureIntegrationBranch(branchName string) (bool, error) {
	name := strings.TrimSpace(branchName)
	if name == "" {
		return false, nil
	}
	defaultBranch, _ := g.GetDefaultBranch()
	if name == defaultBranch {
		return true, nil
	}

	// Check if branch exists
	enc := url.PathEscape(name)
	_, err := g.get(fmt.Sprintf("git/ref/heads/%s", enc), nil)
	if err == nil {
		return true, nil
	}

	// Get default branch SHA
	refData, err := g.get(fmt.Sprintf("git/ref/heads/%s", url.PathEscape(defaultBranch)), nil)
	if err != nil {
		return false, err
	}
	rm, _ := refData.(map[string]interface{})
	obj, _ := rm["object"].(map[string]interface{})
	sha := strVal(obj["sha"])
	if sha == "" {
		return false, nil
	}

	_, err = g.post("git/refs", map[string]interface{}{
		"ref": fmt.Sprintf("refs/heads/%s", name),
		"sha": sha,
	})
	if err != nil {
		errStr := strings.ToLower(err.Error())
		if strings.Contains(errStr, "already exists") || strings.Contains(errStr, "422") {
			return true, nil
		}
		slog.Warn("ensure_integration_branch GitHub failed",
			slog.String("branch", name),
			slog.String("error", truncate(err.Error(), 200)),
		)
		return false, err
	}

	slog.Info("created integration branch on GitHub",
		slog.String("branch", name),
		slog.String("sha", sha[:7]),
	)
	return true, nil
}

// CreatePullRequest creates a pull request on GitHub.
func (g *GitHubClient) CreatePullRequest(title, body, head, base string) (*PRResult, error) {
	data, err := g.post("pulls", map[string]interface{}{
		"title": title,
		"body":  body,
		"head":  head,
		"base":  base,
	})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	return &PRResult{
		PRURL:    strVal(m["html_url"]),
		PRNumber: intVal(m["number"]),
	}, nil
}

// DeleteRemoteBranch deletes a branch on the remote.
func (g *GitHubClient) DeleteRemoteBranch(branch string) error {
	b := url.PathEscape(strings.TrimSpace(branch))
	if b == "" {
		return &HostError{Message: "delete_remote_branch: empty branch name"}
	}
	_, err := g.delete(fmt.Sprintf("git/refs/heads/%s", b))
	return err
}

// ─── Helpers ────────────────────────────────────────────────────────────────

func strVal(v interface{}) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

func intVal(v interface{}) int {
	if v == nil {
		return 0
	}
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return 0
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen]
}

// ─── URL Parsing ────────────────────────────────────────────────────────────

var (
	reGitHubHTTPS = regexp.MustCompile(`https?://(?:[^@]*@)?github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$`)
	reGitHubSSH   = regexp.MustCompile(`git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?$`)
)

// ParseGitHubOwnerRepo extracts (owner, repo) from a GitHub URL.
// Derived from Python: _parse_github_owner_repo().
func ParseGitHubOwnerRepo(repoURL string) (owner, repo string, ok bool) {
	u := strings.TrimSpace(repoURL)
	if m := reGitHubHTTPS.FindStringSubmatch(u); m != nil {
		return m[1], m[2], true
	}
	if m := reGitHubSSH.FindStringSubmatch(u); m != nil {
		return m[1], m[2], true
	}
	return "", "", false
}
