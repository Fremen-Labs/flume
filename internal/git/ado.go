// ado.go — Azure DevOps Git REST API 7.1 client.
//
// Direct port of Python: git_host_client.py AzureDevOpsClient class.
// Docs: https://learn.microsoft.com/en-us/rest/api/azure/devops/git/
package git

import (
	"context"
	"encoding/base64"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"github.com/Fremen-Labs/flume/internal/logger"
)

// AzureDevOpsClient implements HostClient for Azure DevOps Git REST API 7.1.
type AzureDevOpsClient struct {
	Org           string
	Project       string
	Repo          string
	Token         string
	BaseURL       string
	defaultBranch string
}

const adoAPIVersion = "7.1-preview.1"

func (a *AzureDevOpsClient) apiURL(path string) string {
	encProject := url.PathEscape(a.Project)
	encRepo := url.PathEscape(a.Repo)
	baseURL := a.BaseURL
	if baseURL == "" {
		baseURL = "https://dev.azure.com"
	}
	baseURL = strings.TrimRight(baseURL, "/")
	return fmt.Sprintf("%s/%s/%s/_apis/git/repositories/%s/%s",
		baseURL, a.Org, encProject, encRepo, strings.TrimLeft(path, "/"))
}

func (a *AzureDevOpsClient) adoAuth() string {
	raw := ":" + a.Token
	return base64.StdEncoding.EncodeToString([]byte(raw))
}

func (a *AzureDevOpsClient) get(ctx context.Context, path string, params map[string]string) (interface{}, error) {
	p := map[string]string{"api-version": adoAPIVersion}
	for k, v := range params {
		p[k] = v
	}
	v := url.Values{}
	for k, val := range p {
		v.Set(k, val)
	}
	u := a.apiURL(path) + "?" + v.Encode()
	return httpJSON(ctx, u, http.MethodGet, "", nil, map[string]string{
		"Authorization": "Basic " + a.adoAuth(),
	})
}

func (a *AzureDevOpsClient) post(ctx context.Context, path string, body interface{}, params map[string]string) (interface{}, error) {
	p := map[string]string{"api-version": adoAPIVersion}
	for k, v := range params {
		p[k] = v
	}
	v := url.Values{}
	for k, val := range p {
		v.Set(k, val)
	}
	u := a.apiURL(path) + "?" + v.Encode()
	return httpJSON(ctx, u, http.MethodPost, "", body, map[string]string{
		"Authorization": "Basic " + a.adoAuth(),
	})
}

// GetDefaultBranch returns the repository's default branch.
func (a *AzureDevOpsClient) GetDefaultBranch(ctx context.Context) (string, error) {
	if a.defaultBranch != "" {
		return a.defaultBranch, nil
	}
	data, err := a.get(ctx, "", nil)
	if err != nil {
		return "main", err
	}
	m, _ := data.(map[string]interface{})
	raw, _ := m["defaultBranch"].(string)
	if raw == "" {
		raw = "refs/heads/main"
	}
	// "refs/heads/main" → "main"
	a.defaultBranch = strings.TrimPrefix(raw, "refs/heads/")
	return a.defaultBranch, nil
}

// GetBranches returns all branch names.
func (a *AzureDevOpsClient) GetBranches(ctx context.Context) ([]string, error) {
	data, err := a.get(ctx, "refs", map[string]string{"filter": "heads"})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	values, _ := m["value"].([]interface{})
	var branches []string
	for _, ref := range values {
		rm, _ := ref.(map[string]interface{})
		name, _ := rm["name"].(string)
		if strings.HasPrefix(name, "refs/heads/") {
			branches = append(branches, strings.TrimPrefix(name, "refs/heads/"))
		}
	}
	return branches, nil
}

// GetTree returns a flat list of all entries for the given branch.
func (a *AzureDevOpsClient) GetTree(ctx context.Context, branch string) ([]TreeEntry, error) {
	if branch == "" {
		var err error
		branch, err = a.GetDefaultBranch(ctx)
		if err != nil {
			return nil, err
		}
	}
	data, err := a.get(ctx, "items", map[string]string{
		"scopePath":                       "/",
		"recursionLevel":                  "full",
		"versionDescriptor.version":       branch,
		"versionDescriptor.versionType":   "branch",
	})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	values, _ := m["value"].([]interface{})
	var entries []TreeEntry
	for _, item := range values {
		im, _ := item.(map[string]interface{})
		path := strings.TrimLeft(strVal(im["path"]), "/")
		if path == "" {
			continue
		}
		entryType := "blob"
		if isFolder, ok := im["isFolder"].(bool); ok && isFolder {
			entryType = "tree"
		}
		entries = append(entries, TreeEntry{
			Path: path,
			Type: entryType,
			Size: "-",
		})
	}
	return entries, nil
}

// GetFile returns the raw bytes of a file at the given path and branch.
func (a *AzureDevOpsClient) GetFile(ctx context.Context, path, branch string) ([]byte, error) {
	if branch == "" {
		var err error
		branch, err = a.GetDefaultBranch(ctx)
		if err != nil {
			return nil, err
		}
	}
	clean := "/" + strings.TrimLeft(path, "/")
	p := map[string]string{
		"api-version":                   adoAPIVersion,
		"path":                          clean,
		"versionDescriptor.version":     branch,
		"versionDescriptor.versionType": "branch",
		"$format":                       "octetStream",
	}
	v := url.Values{}
	for k, val := range p {
		v.Set(k, val)
	}
	u := a.apiURL("items") + "?" + v.Encode()
	return httpRaw(ctx, u, "Basic "+a.adoAuth())
}

// GetDiff returns a diff summary between two refs.
func (a *AzureDevOpsClient) GetDiff(ctx context.Context, base, head string) (*DiffResult, error) {
	data, err := a.get(ctx, "diffs/commits", map[string]string{
		"baseVersionDescriptor.version":     base,
		"baseVersionDescriptor.versionType": "branch",
		"targetVersionDescriptor.version":   head,
		"targetVersionDescriptor.versionType": "branch",
	})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	changes, _ := m["changes"].([]interface{})
	var files []DiffFile
	for _, change := range changes {
		cm, _ := change.(map[string]interface{})
		item, _ := cm["item"].(map[string]interface{})
		changeType, _ := cm["changeType"].(string)
		if changeType == "" {
			changeType = "edit"
		}
		files = append(files, DiffFile{
			Path:       strings.TrimLeft(strVal(item["path"]), "/"),
			Insertions: 0,
			Deletions:  0,
			Status:     changeType,
		})
	}
	return &DiffResult{
		Base:  base,
		Head:  head,
		Files: files,
		Diff:  "", // ADO does not provide unified diff via REST
	}, nil
}

// GetCommits returns commits on branch not in base.
func (a *AzureDevOpsClient) GetCommits(ctx context.Context, branch, base string) ([]Commit, error) {
	if base == "" {
		var err error
		base, err = a.GetDefaultBranch(ctx)
		if err != nil {
			return nil, err
		}
	}
	data, err := a.get(ctx, "commits", map[string]string{
		"searchCriteria.itemVersion.version":       branch,
		"searchCriteria.itemVersion.versionType":   "branch",
		"searchCriteria.compareVersion.version":     base,
		"searchCriteria.compareVersion.versionType": "branch",
		"searchCriteria.$top":                       "50",
	})
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	values, _ := m["value"].([]interface{})
	var commits []Commit
	for _, c := range values {
		cm, _ := c.(map[string]interface{})
		author, _ := cm["author"].(map[string]interface{})
		sha := strVal(cm["commitId"])
		if len(sha) > 40 {
			sha = sha[:40]
		}
		msg := strVal(cm["comment"])
		if idx := strings.Index(msg, "\n"); idx >= 0 {
			msg = msg[:idx]
		}
		commits = append(commits, Commit{
			SHA:     sha,
			Author:  strVal(author["name"]),
			Date:    strVal(author["date"]),
			Message: msg,
		})
	}
	return commits, nil
}

// EnsureIntegrationBranch creates the branch at the default tip if absent.
func (a *AzureDevOpsClient) EnsureIntegrationBranch(ctx context.Context, branchName string) (bool, error) {
	log := logger.WithContext(ctx)
	name := strings.TrimSpace(branchName)
	if name == "" {
		return false, nil
	}
	defaultBranch, _ := a.GetDefaultBranch(ctx)
	if name == defaultBranch {
		return true, nil
	}

	branches, _ := a.GetBranches(ctx)
	for _, b := range branches {
		if b == name {
			return true, nil
		}
	}

	// Get default branch SHA
	refsData, err := a.get(ctx, "refs", map[string]string{"filter": "heads/" + defaultBranch})
	if err != nil {
		return false, err
	}
	rm, _ := refsData.(map[string]interface{})
	values, _ := rm["value"].([]interface{})
	if len(values) == 0 {
		return false, nil
	}
	first, _ := values[0].(map[string]interface{})
	newObjectID := strings.TrimSpace(strVal(first["objectId"]))
	if newObjectID == "" {
		return false, nil
	}

	body := []map[string]string{
		{
			"name":        fmt.Sprintf("refs/heads/%s", name),
			"oldObjectId": "0000000000000000000000000000000000000000",
			"newObjectId": newObjectID,
		},
	}
	_, err = a.post(ctx, "refs", body, nil)
	if err != nil {
		errStr := strings.ToLower(err.Error())
		if strings.Contains(errStr, "already exists") || strings.Contains(errStr, "name already exists") {
			return true, nil
		}
		log.Warn("ensure_integration_branch ADO failed",
			slog.String("branch", name),
			slog.String("error", truncate(err.Error(), 200)),
		)
		return false, err
	}

	log.Info("created integration branch on Azure DevOps",
		slog.String("branch", name),
	)
	return true, nil
}

// CreatePullRequest creates a pull request on Azure DevOps.
func (a *AzureDevOpsClient) CreatePullRequest(ctx context.Context, title, body, head, base string) (*PRResult, error) {
	data, err := a.post(ctx, "pullrequests", map[string]interface{}{
		"title":         title,
		"description":   body,
		"sourceRefName": fmt.Sprintf("refs/heads/%s", head),
		"targetRefName": fmt.Sprintf("refs/heads/%s", base),
	}, nil)
	if err != nil {
		return nil, err
	}
	m, _ := data.(map[string]interface{})
	prID := intVal(m["pullRequestId"])
	prURL := strVal(m["url"])
	if prID > 0 {
		encProject := url.PathEscape(a.Project)
		encRepo := url.PathEscape(a.Repo)
		prURL = fmt.Sprintf("https://dev.azure.com/%s/%s/_git/%s/pullrequest/%d",
			a.Org, encProject, encRepo, prID)
	}
	return &PRResult{
		PRURL:    prURL,
		PRNumber: prID,
	}, nil
}

// ─── ADO URL Parsing ────────────────────────────────────────────────────────

var (
	reADODevAzure    = regexp.MustCompile(`https?://dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?]+)`)
	reADOVisualDC    = regexp.MustCompile(`https?://([^.]+)\.visualstudio\.com/DefaultCollection/([^/]+)/_git/([^/?]+)`)
	reADOVisualPlain = regexp.MustCompile(`https?://([^.]+)\.visualstudio\.com/([^/]+)/_git/([^/?]+)`)
)

// ParseADOComponents extracts (org, project, repo) from an Azure DevOps URL.
// Derived from Python: _parse_ado_components().
func ParseADOComponents(repoURL string) (org, project, repo string, ok bool) {
	u := strings.TrimSpace(repoURL)
	// Strip trailing .git and /
	u = regexp.MustCompile(`\.git$`).ReplaceAllString(u, "")
	u = strings.TrimRight(u, "/")
	// Strip embedded username: https://user@host → https://host
	u = regexp.MustCompile(`(https?://)([^@]+@)`).ReplaceAllString(u, "$1")

	if m := reADODevAzure.FindStringSubmatch(u); m != nil {
		return m[1], m[2], m[3], true
	}
	if m := reADOVisualDC.FindStringSubmatch(u); m != nil {
		return m[1], m[2], m[3], true
	}
	if m := reADOVisualPlain.FindStringSubmatch(u); m != nil {
		return m[1], m[2], m[3], true
	}
	return "", "", "", false
}
