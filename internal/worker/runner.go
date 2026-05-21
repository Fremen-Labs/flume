package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"

	"regexp"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Runner contains the core agent execution loop.
// Derived from Python: worker_handlers.py (2410 LOC, 112 AST nodes) —
// the densest module in the Flume codebase.
//
// Key functions ported:
//   - run_worker            (L2186-2231, 24 parents, 31 children)
//   - ensure_task_branch    (L568-772, 11 parents, 14 children)
//   - auto_commit_and_push  (L1918-2023, 11 parents, 3 children)
//   - create_pr_for_task    (L959-1110, 14 parents, 8 children)
//   - compute_ready_for_repo (L1636-1864, 25 parents, 4 children)
type Runner struct {
	es       *es.Client
	logger   *slog.Logger
	registry *ProviderRegistry
	tools    *ToolRegistry
}

// NewRunner creates a new task execution runner.
func NewRunner(esClient *es.Client, logger *slog.Logger) *Runner {
	return &Runner{
		es:       esClient,
		logger:   logger.With(slog.String("component", "runner")),
		registry: NewProviderRegistry(logger),
		tools:    NewToolRegistry(logger),
	}
}

// RunWorker executes a single worker cycle for a claimed task.
// Derived from Python: run_worker() (L2186-2231, 24 parents, 31 children)
func (r *Runner) RunWorker(ctx context.Context, worker ftypes.Worker, taskID string) error {
	r.logger.Info("worker heartbeat",
		slog.String("worker", worker.Name),
		slog.String("task_id", taskID))

	// 1. Fetch the task document
	taskDoc, err := r.es.GetDoc(ctx, "flume-tasks", taskID)
	if err != nil || taskDoc == nil {
		r.logger.Warn("worker could not find task",
			slog.String("worker", worker.Name),
			slog.String("task_id", taskID))
		return fmt.Errorf("task %s not found", taskID)
	}

	var task ftypes.Task
	if err := json.Unmarshal(taskDoc, &task); err != nil {
		return fmt.Errorf("unmarshal task %s: %w", taskID, err)
	}

	// 2. Execute based on role
	var result ftypes.AgentResult
	switch worker.Role {
	case "implementer":
		result, err = r.handleImplementer(ctx, task, worker)
	case "reviewer":
		result, err = r.handleReviewer(ctx, task, worker)
	case "tester":
		result, err = r.handleTester(ctx, task, worker)
	case "pm":
		result, err = r.handlePM(ctx, task, worker)
	default:
		err = fmt.Errorf("unknown role: %s", worker.Role)
	}

	// 3. Handle errors and update task
	if err != nil {
		r.logger.Error("worker error",
			slog.String("worker", worker.Name),
			slog.String("task_id", taskID),
			slog.String("error", err.Error()))

		// Clear stale claim
		r.clearStaleClaim(ctx, taskID, task.Status)
		return err
	}

	// 4. Transition task status
	if result.NextStatus != "" && result.NextStatus != task.Status {
		if validErr := ftypes.ValidateTransition(task.Status, result.NextStatus); validErr != nil {
			r.logger.Error("invalid state transition",
				slog.String("task_id", taskID),
				slog.String("from", string(task.Status)),
				slog.String("to", string(result.NextStatus)),
				slog.String("error", validErr.Error()))
		} else {
			r.updateTaskStatus(ctx, taskID, result.NextStatus)
		}
	}

	return nil
}

// handleImplementer runs the implementer agent.
// Derived from Python: handlers/implementer.handle_implementer_worker() (412 LOC)
func (r *Runner) handleImplementer(ctx context.Context, task ftypes.Task, worker ftypes.Worker) (ftypes.AgentResult, error) {
	r.logger.Info("implementer: starting",
		slog.String("task_id", task.ID),
		slog.String("title", task.Title))

	// Check if task requires code
	if !TaskRequiresCode(task) {
		r.logger.Info("implementer: non-code task, completing",
			slog.String("task_id", task.ID))
		return ftypes.AgentResult{
			Success:    true,
			NextStatus: ftypes.TaskStatusDone,
		}, nil
	}

	// 1. Ensure task branch exists
	repoPath, branch, err := r.EnsureTaskBranch(ctx, task)
	if err != nil {
		return ftypes.AgentResult{
			Success: false,
			Errors:  []string{err.Error()},
		}, err
	}

	// 2. Build LLM context and execute agent loop
	// (This will be fully fleshed out when internal/llm is implemented in Phase 3)
	r.logger.Info("implementer: executing agent loop",
		slog.String("task_id", task.ID),
		slog.String("repo_path", repoPath),
		slog.String("branch", branch))

	// 3. Auto-commit and push changes
	commitSHA, err := r.AutoCommitAndPush(ctx, repoPath, branch,
		fmt.Sprintf("[Flume] %s", task.Title), task.ID)
	if err != nil {
		r.logger.Error("auto_commit: failed",
			slog.String("task_id", task.ID),
			slog.String("error", err.Error()))
	}

	// 4. Create PR if branch has new commits
	if commitSHA != "" {
		r.logger.Info("implementer: committed and pushed",
			slog.String("task_id", task.ID),
			slog.String("sha", commitSHA))
	}

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusReview,
	}, nil
}

// handleReviewer runs the reviewer agent.
func (r *Runner) handleReviewer(ctx context.Context, task ftypes.Task, worker ftypes.Worker) (ftypes.AgentResult, error) {
	r.logger.Info("reviewer: starting",
		slog.String("task_id", task.ID))

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// handleTester runs the tester agent.
func (r *Runner) handleTester(ctx context.Context, task ftypes.Task, worker ftypes.Worker) (ftypes.AgentResult, error) {
	r.logger.Info("tester: starting",
		slog.String("task_id", task.ID))

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// handlePM runs the PM agent for task decomposition.
func (r *Runner) handlePM(ctx context.Context, task ftypes.Task, worker ftypes.Worker) (ftypes.AgentResult, error) {
	r.logger.Info("pm: decomposing",
		slog.String("task_id", task.ID))

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// ─── Git Operations ─────────────────────────────────────────────────────────

// EnsureTaskBranch creates or checks out the branch for a task.
// Derived from Python: ensure_task_branch() (L568-772, 11 parents, 14 children)
func (r *Runner) EnsureTaskBranch(ctx context.Context, task ftypes.Task) (string, string, error) {
	if task.ProjectID == "" {
		return "", "", fmt.Errorf("task %s has no project_id", task.ID)
	}

	// Load project to get repo info
	projDoc, err := r.es.GetDoc(ctx, "flume-projects", task.ProjectID)
	if err != nil || projDoc == nil {
		return "", "", fmt.Errorf("project %s not found", task.ProjectID)
	}

	var project ftypes.Project
	if err := json.Unmarshal(projDoc, &project); err != nil {
		return "", "", fmt.Errorf("unmarshal project: %w", err)
	}

	repoPath := project.LocalPath
	if repoPath == "" {
		return "", "", fmt.Errorf("project %s has no local_path", task.ProjectID)
	}

	// Determine branch name
	branch := resolveBranchName(task)

	// Create/checkout branch
	if err := gitCheckoutBranch(repoPath, branch); err != nil {
		return "", "", fmt.Errorf("checkout branch %s: %w", branch, err)
	}

	return repoPath, branch, nil
}

// AutoCommitAndPush stages, commits, and pushes changes.
// Derived from Python: auto_commit_and_push() (L1918-2023, 11 parents, 3 children)
func (r *Runner) AutoCommitAndPush(ctx context.Context, repoPath, branch, message, taskID string) (string, error) {
	// Check if there are changes
	out, err := gitCmd(repoPath, "status", "--porcelain")
	if err != nil {
		return "", fmt.Errorf("git status: %w", err)
	}
	if strings.TrimSpace(out) == "" {
		r.logger.Info("auto_commit: no changes to commit",
			slog.String("task_id", taskID))
		return "", nil
	}

	// Stage all changes
	if _, err := gitCmd(repoPath, "add", "-A"); err != nil {
		return "", fmt.Errorf("git add: %w", err)
	}

	// Commit
	if _, err := gitCmd(repoPath, "commit", "-m", message); err != nil {
		return "", fmt.Errorf("git commit: %w", err)
	}

	// Rebase before push
	if _, err := gitCmd(repoPath, "pull", "--rebase", "origin", branch); err != nil {
		// Check for rebase conflict
		if strings.Contains(err.Error(), "CONFLICT") || strings.Contains(err.Error(), "conflict") {
			r.logger.Error("auto_commit: rebase conflict detected",
				slog.String("task_id", taskID),
				slog.String("branch", branch))
			_, _ = gitCmd(repoPath, "rebase", "--abort")
			return "", fmt.Errorf("rebase conflict on %s", branch)
		}
		r.logger.Warn("auto_commit: pre-push rebase error",
			slog.String("task_id", taskID),
			slog.String("error", err.Error()))
	}

	// Push
	if _, err := gitCmd(repoPath, "push", "origin", branch); err != nil {
		r.logger.Error("auto_commit: push failed",
			slog.String("task_id", taskID),
			slog.String("branch", branch),
			slog.String("error", err.Error()))
		return "", fmt.Errorf("git push: %w", err)
	}

	// Get commit SHA
	sha, _ := gitCmd(repoPath, "rev-parse", "HEAD")
	sha = strings.TrimSpace(sha)

	r.logger.Info("auto_commit: committed and pushed",
		slog.String("task_id", taskID),
		slog.String("branch", branch),
		slog.String("sha", sha))

	return sha, nil
}

// ─── Task Helpers ───────────────────────────────────────────────────────────

// TaskRequiresCode checks if a task description implies code changes.
// Derived from Python: task_requires_code() (L2041-2100)
func TaskRequiresCode(task ftypes.Task) bool {
	lower := strings.ToLower(task.Description + " " + task.Title)
	codeKeywords := []string{
		"implement", "fix", "refactor", "create", "update", "add",
		"remove", "delete", "modify", "change", "build", "develop",
		"code", "function", "class", "method", "api", "endpoint",
	}
	for _, kw := range codeKeywords {
		if strings.Contains(lower, kw) {
			return true
		}
	}
	return false
}

// ComputeReadyForRepo scans a repo's tasks and promotes eligible ones.
// Derived from Python: compute_ready_for_repo() (L1636-1864, 25 parents, 4 children)
func (r *Runner) ComputeReadyForRepo(ctx context.Context, repoID string) int {
	// Fetch all tasks for this repo
	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]string{"project_id": repoID}},
			},
		},
	}

	result, err := r.es.Search(ctx, "flume-tasks", query, 500)
	if err != nil {
		r.logger.Warn("compute_ready: search failed", slog.String("error", err.Error()))
		return 0
	}

	byID := make(map[string]ftypes.Task)
	for _, hit := range result.Hits {
		var task ftypes.Task
		if json.Unmarshal(hit, &task) == nil {
			byID[task.ID] = task
		}
	}

	promoted := 0
	for id, task := range byID {
		if task.Status != ftypes.TaskStatusPlanned {
			continue
		}

		// Check if all dependencies (parent) are met
		if task.ParentID != "" {
			parent, exists := byID[task.ParentID]
			if !exists || (parent.Status != ftypes.TaskStatusDone && parent.Status != ftypes.TaskStatusArchived) {
				continue
			}
		}

		// Promote to ready
		update := map[string]interface{}{
			"status":     "ready",
			"updated_at": time.Now().UTC().Format(time.RFC3339),
		}
		if err := r.es.IndexDoc(ctx, "flume-tasks", id, update); err == nil {
			promoted++
			r.logger.Info("compute_ready: promoted to ready",
				slog.String("task_id", id))
		}
	}

	// Check if all children of a parent are terminal → mark parent done
	for id, task := range byID {
		if task.ParentID != "" {
			continue // only check parents
		}
		if task.Status == ftypes.TaskStatusDone || task.Status == ftypes.TaskStatusArchived {
			continue
		}

		allChildrenDone := true
		hasChildren := false
		for _, child := range byID {
			if child.ParentID == id {
				hasChildren = true
				if child.Status != ftypes.TaskStatusDone && child.Status != ftypes.TaskStatusArchived {
					allChildrenDone = false
					break
				}
			}
		}

		if hasChildren && allChildrenDone {
			update := map[string]interface{}{
				"status":     "done",
				"updated_at": time.Now().UTC().Format(time.RFC3339),
			}
			if err := r.es.IndexDoc(ctx, "flume-tasks", id, update); err == nil {
				r.logger.Info("compute_ready: marked parent done — all children terminal",
					slog.String("task_id", id),
					slog.String("title", task.Title))
			}
		}
	}

	return promoted
}

// clearStaleClaim resets a task after a worker crash.
// Derived from Python: run_worker() error handling (L2215-2227)
func (r *Runner) clearStaleClaim(ctx context.Context, taskID string, currentStatus ftypes.TaskStatus) {
	targetStatus := "ready"
	if currentStatus == ftypes.TaskStatusReview {
		targetStatus = "review"
	}

	update := map[string]interface{}{
		"status":        targetStatus,
		"active_worker": nil,
		"queue_state":   "available",
		"updated_at":    time.Now().UTC().Format(time.RFC3339),
	}

	if err := r.es.IndexDoc(ctx, "flume-tasks", taskID, update); err == nil {
		r.logger.Info("cleared stale claim on task after worker crash",
			slog.String("task_id", taskID),
			slog.String("reset_to", targetStatus))
	}
}

func (r *Runner) updateTaskStatus(ctx context.Context, taskID string, status ftypes.TaskStatus) {
	update := map[string]interface{}{
		"status":        string(status),
		"active_worker": nil,
		"queue_state":   "available",
		"updated_at":    time.Now().UTC().Format(time.RFC3339),
	}
	if status == ftypes.TaskStatusDone {
		now := time.Now().UTC().Format(time.RFC3339)
		update["completed_at"] = now
	}
	_ = r.es.IndexDoc(ctx, "flume-tasks", taskID, update)
}

// ─── Git Helpers ────────────────────────────────────────────────────────────

func gitCmd(repoPath string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = repoPath
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func gitCheckoutBranch(repoPath, branch string) error {
	// Try checkout existing branch
	if _, err := gitCmd(repoPath, "checkout", branch); err != nil {
		// Create new branch from default
		defaultBranch, _ := resolveDefaultBranch(repoPath)
		if _, err := gitCmd(repoPath, "checkout", "-b", branch, defaultBranch); err != nil {
			return err
		}
	}
	return nil
}

// resolveDefaultBranch determines the repo's default branch.
// Derived from Python: resolve_default_branch() (L814-840)
func resolveDefaultBranch(repoPath string) (string, error) {
	if override := os.Getenv("FLUME_DEFAULT_BRANCH"); override != "" {
		return override, nil
	}
	out, err := gitCmd(repoPath, "symbolic-ref", "--short", "HEAD")
	if err != nil {
		return "main", nil // safe default
	}
	return strings.TrimSpace(out), nil
}

var branchSanitizeRe = regexp.MustCompile(`[^a-zA-Z0-9._/-]`)

// resolveBranchName generates a deterministic branch name for a task.
// Derived from Python: _sanitize_git_branch_segment() + _resolve_branch_scope_id()
func resolveBranchName(task ftypes.Task) string {
	scope := task.ID
	if task.ParentID != "" {
		scope = task.ParentID
	}

	hash := sha256.Sum256([]byte(scope))
	shortHash := hex.EncodeToString(hash[:4])

	segment := branchSanitizeRe.ReplaceAllString(task.Title, "-")
	if len(segment) > 40 {
		segment = segment[:40]
	}
	segment = strings.Trim(segment, "-")

	prefix := flumeAutoPRScope()
	return fmt.Sprintf("%s/%s-%s", prefix, segment, shortHash)
}

// flumeAutoPRScope returns the branch prefix.
// Derived from Python: _flume_auto_pr_scope() (L430-433)
func flumeAutoPRScope() string {
	if scope := os.Getenv("FLUME_AUTO_PR_SCOPE"); scope != "" {
		return scope
	}
	return "feature"
}

// BranchHasNewCommits checks if a branch has commits not on the default branch.
// Derived from Python: _branch_has_new_commits() (L2026-2040)
func BranchHasNewCommits(repoPath, branch string) bool {
	defaultBranch, _ := resolveDefaultBranch(repoPath)
	out, err := gitCmd(repoPath, "rev-list", "--count", fmt.Sprintf("%s..%s", defaultBranch, branch))
	if err != nil {
		return false
	}
	return strings.TrimSpace(out) != "0"
}

// ─── LLM Failure Handling ───────────────────────────────────────────────────

// ImplementerHandleLLMFailure handles LLM errors with retry/block logic.
// Derived from Python: _implementer_handle_llm_failure() (L2129-2183)
func (r *Runner) ImplementerHandleLLMFailure(ctx context.Context, taskID string, task ftypes.Task) {
	failureCount := task.Attempts + 1
	maxCap := implementerMaxLLMFailuresCap()

	if failureCount >= maxCap {
		r.logger.Error("implementer: task blocked after LLM failures",
			slog.String("task_id", taskID),
			slog.Int("failures", failureCount),
			slog.Int("cap", maxCap))
		update := map[string]interface{}{
			"status":         "blocked",
			"attempts":       failureCount,
			"error_message":  fmt.Sprintf("blocked after %d LLM failures (cap=%d)", failureCount, maxCap),
			"active_worker":  nil,
			"queue_state":    "available",
			"updated_at":     time.Now().UTC().Format(time.RFC3339),
		}
		_ = r.es.IndexDoc(ctx, "flume-tasks", taskID, update)
	} else {
		r.logger.Warn("implementer: task re-queued for retry",
			slog.String("task_id", taskID),
			slog.Int("attempt", failureCount),
			slog.Int("max", maxCap))
		update := map[string]interface{}{
			"status":        "ready",
			"attempts":      failureCount,
			"active_worker": nil,
			"queue_state":   "available",
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		}
		_ = r.es.IndexDoc(ctx, "flume-tasks", taskID, update)
	}
}

// implementerMaxLLMFailuresCap returns the max retry cap.
// Derived from Python: _implementer_max_llm_failures_cap() (L2107-2120)
func implementerMaxLLMFailuresCap() int {
	cap := 3 // default
	if v := os.Getenv("FLUME_IMPLEMENTER_MAX_LLM_FAILURES"); v != "" {
		if _, err := fmt.Sscanf(v, "%d", &cap); err != nil || cap < 1 {
			cap = 3
		}
	}
	return cap
}
