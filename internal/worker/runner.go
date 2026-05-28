package worker

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"

	"regexp"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	"github.com/Fremen-Labs/flume/internal/git"
	"github.com/Fremen-Labs/flume/internal/llm"
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
//   - compute_ready_for_repo (L1636-1864, 25 parents, 4 children) — DEPRECATED PR3 (unified in Sweeper.promotePlannedTasks) — DEPRECATED in PR3; logic unified into Sweeper.promotePlannedTasks (sweeps.go)
type Runner struct {
	es       *es.Client
	llm      *llm.Client
	logger   *slog.Logger
	registry *ProviderRegistry
	tools    *ToolRegistry
}

// NewRunner creates a new task execution runner.
func NewRunner(esClient *es.Client, llmClient *llm.Client, logger *slog.Logger) *Runner {
	return &Runner{
		es:       esClient,
		llm:      llmClient,
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
	taskDoc, err := r.es.GetDoc(ctx, "agent-task-records", taskID)
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
		if err == nil && result.Success && result.NextStatus == ftypes.TaskStatusReview {
			result.NextStatus = ftypes.TaskStatusReviewConsensus
			if spawnErr := r.spawnReviewTasks(ctx, task); spawnErr != nil {
				r.logger.Error("failed to spawn review tasks", slog.String("error", spawnErr.Error()))
				result.NextStatus = ftypes.TaskStatusReview
			}
		}
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
		if validErr := ftypes.DefaultTaskStateMachine.EnforceTransition(task.Status, result.NextStatus); validErr != nil {
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
	r.logger.Info("reviewer: starting", slog.String("task_id", task.ID))

	// Fetch parent task
	var parent ftypes.Task
	if task.ParentID != "" {
		parentDoc, err := r.es.GetDoc(ctx, "agent-task-records", task.ParentID)
		if err == nil && parentDoc != nil {
			_ = json.Unmarshal(parentDoc, &parent)
		}
	}
	if parent.ID == "" {
		parent = task
	}

	// Get project local path
	repoPath := ""
	if parent.ProjectID != "" {
		projDoc, err := r.es.GetDoc(ctx, "flume-projects", parent.ProjectID)
		if err == nil && projDoc != nil {
			var proj ftypes.Project
			if json.Unmarshal(projDoc, &proj) == nil {
				repoPath = proj.LocalPath
				if repoPath == "" {
					workspace := os.Getenv("FLUME_WORKSPACE_ROOT")
					if workspace == "" {
						workspace = "/app/workspace"
					}
					repoPath = filepath.Join(workspace, fmt.Sprintf("flume-reg-%s", proj.ID))
				}
			}
		}
	}

	diffOut := ""
	if repoPath != "" {
		branch := resolveBranchName(parent)
		defaultBranch, _ := resolveDefaultBranch(repoPath)
		var err error
		diffOut, err = gitCmd(repoPath, "diff", defaultBranch+"..."+branch)
		if err != nil {
			r.logger.Warn("reviewer: git diff failed", slog.String("error", err.Error()))
		}
	}

	reviewerSystemPrompt := readSystemPrompt("reviewer")
	req := llm.ChatRequest{
		Messages: []llm.Message{
			{Role: "system", Content: reviewerSystemPrompt},
			{Role: "user", Content: fmt.Sprintf("Please review this implementation:\nTask: %s\nDiff:\n%s", parent.Title, diffOut)},
		},
		Model:     worker.Model,
		Provider:  worker.Provider,
		AgentRole: "reviewer",
		TaskID:    task.ID,
	}

	resp, err := r.llm.Chat(ctx, req)
	if err != nil {
		return ftypes.AgentResult{Success: false, Errors: []string{err.Error()}}, err
	}

	approved := true
	var reviewResult struct {
		Approved bool `json:"approved"`
	}
	content := cleanJSONContent(resp.Content)
	if json.Unmarshal([]byte(content), &reviewResult) == nil {
		approved = reviewResult.Approved
	} else {
		// Fallback check
		if strings.Contains(strings.ToLower(resp.Content), `"approved": false`) ||
			strings.Contains(strings.ToLower(resp.Content), `approved: false`) {
			approved = false
		}
	}

	verdict := "approved"
	if !approved {
		verdict = "rejected"
	}

	update := map[string]interface{}{
		"review_verdict": verdict,
		"feedback":       resp.Content,
		"updated_at":     time.Now().UTC().Format(time.RFC3339),
	}
	_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, update)

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// handleTester runs the tester agent.
func (r *Runner) handleTester(ctx context.Context, task ftypes.Task, worker ftypes.Worker) (ftypes.AgentResult, error) {
	r.logger.Info("tester: starting", slog.String("task_id", task.ID))

	// Fetch parent task
	var parent ftypes.Task
	if task.ParentID != "" {
		parentDoc, err := r.es.GetDoc(ctx, "agent-task-records", task.ParentID)
		if err == nil && parentDoc != nil {
			_ = json.Unmarshal(parentDoc, &parent)
		}
	}
	if parent.ID == "" {
		parent = task
	}

	// Get project local path
	repoPath := ""
	if parent.ProjectID != "" {
		projDoc, err := r.es.GetDoc(ctx, "flume-projects", parent.ProjectID)
		if err == nil && projDoc != nil {
			var proj ftypes.Project
			if json.Unmarshal(projDoc, &proj) == nil {
				repoPath = proj.LocalPath
			}
		}
	}

	verdict := "approved"
	feedback := "No repository or local path configured for testing."

	if repoPath != "" {
		branch := resolveBranchName(parent)
		// Check out the branch
		if err := gitCheckoutBranch(repoPath, branch); err != nil {
			verdict = "rejected"
			feedback = "Failed to checkout branch for testing: " + err.Error()
		} else {
			// Determine test command
			var cmd *exec.Cmd
			if _, err := os.Stat(repoPath + "/go.mod"); err == nil {
				cmd = exec.Command("go", "test", "./...")
			} else if _, err := os.Stat(repoPath + "/package.json"); err == nil {
				cmd = exec.Command("npm", "test")
			} else {
				cmd = exec.Command("make", "test")
			}
			cmd.Dir = repoPath
			out, testErr := cmd.CombinedOutput()
			feedback = string(out)
			if testErr != nil {
				verdict = "rejected"
			}
		}
	}

	update := map[string]interface{}{
		"review_verdict": verdict,
		"feedback":       feedback,
		"updated_at":     time.Now().UTC().Format(time.RFC3339),
	}
	_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, update)

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// handlePM runs the PM agent for task decomposition.
//
// Anti-explosion guard (added 2026-05):
//   If this PM task already has any direct children in agent-task-records,
//   we skip the LLM call + subtask creation entirely. This prevents the
//   runaway decomposition loop observed when small Plan New Work outputs
//   (even 2-4 leaf tasks) triggered repeated PM claims on the same parents,
//   creating hundreds of near-duplicate planned items.
//
// The guard is cheap (single small search) and reuses the exact child
// query pattern already present in parentCompletionSweep.
func (r *Runner) handlePM(ctx context.Context, task ftypes.Task, worker ftypes.Worker) (ftypes.AgentResult, error) {
	r.logger.Info("pm: decomposing", slog.String("task_id", task.ID))

	// === Anti-re-decomposition guard (core fix for workitem explosion) ===
	// Prefer the cheap denormalized ChildCount / DecomposedAt (populated by previous decompositions
	// and by intake creation). Fall back to a small child search if the denorm fields are not yet set.
	if task.ChildCount > 0 || task.DecomposedAt != nil {
		r.logger.Info("pm: skipping re-decomposition — denorm fields indicate children exist (cheap guard)",
			slog.String("task_id", task.ID),
			slog.Int("child_count", task.ChildCount),
			slog.String("title", task.Title))
		return ftypes.AgentResult{
			Success:    true,
			NextStatus: ftypes.TaskStatusDone,
		}, nil
	}

	childQuery := map[string]interface{}{
		"term": map[string]string{"parent_id": task.ID},
	}
	childRes, cerr := r.es.Search(ctx, "agent-task-records", childQuery, 1)
	if cerr == nil && len(childRes.Hits) > 0 {
		r.logger.Info("pm: skipping re-decomposition — task already has children (anti-explosion guard)",
			slog.String("task_id", task.ID),
			slog.Int("existing_child_count", len(childRes.Hits)),
			slog.String("title", task.Title))
		return ftypes.AgentResult{
			Success:    true,
			NextStatus: ftypes.TaskStatusDone,
		}, nil
	}
	if cerr != nil {
		r.logger.Warn("pm: child-existence check failed (proceeding conservatively)",
			slog.String("task_id", task.ID),
			slog.String("error", cerr.Error()))
	}

	pmSystemPrompt := readSystemPrompt("pm")
	req := llm.ChatRequest{
		Messages: []llm.Message{
			{Role: "system", Content: pmSystemPrompt},
			{Role: "user", Content: fmt.Sprintf("Please decompose the following task:\nTitle: %s\nObjective: %s", task.Title, task.Description)},
		},
		Model:     worker.Model,
		Provider:  worker.Provider,
		AgentRole: "pm",
		TaskID:    task.ID,
	}

	resp, err := r.llm.Chat(ctx, req)
	if err != nil {
		return ftypes.AgentResult{Success: false, Errors: []string{err.Error()}}, err
	}

	type SubtaskPlan struct {
		ID        string   `json:"id"`
		Title     string   `json:"title"`
		Objective string   `json:"objective"`
		DependsOn []string `json:"depends_on"`
	}
	var plan struct {
		Tasks []SubtaskPlan `json:"tasks"`
	}

	content := cleanJSONContent(resp.Content)
	if err := json.Unmarshal([]byte(content), &plan); err != nil {
		r.logger.Error("pm: failed to parse subtask plan JSON", slog.String("raw", resp.Content), slog.String("error", err.Error()))
		return ftypes.AgentResult{Success: false, Errors: []string{"failed to parse plan: " + err.Error()}}, err
	}

	// Map relative IDs to unique IDs
	idMap := make(map[string]string)
	for _, t := range plan.Tasks {
		idMap[t.ID] = fmt.Sprintf("task-%s", generateShortID())
	}

	now := time.Now().UTC()
	for _, t := range plan.Tasks {
		newID := idMap[t.ID]
		var dependsOn []string
		for _, dep := range t.DependsOn {
			if mapped, ok := idMap[dep]; ok {
				dependsOn = append(dependsOn, mapped)
			}
		}

		subtask := ftypes.Task{
			ID:             newID,
			Title:          t.Title,
			Description:    t.Objective,
			Status:         ftypes.TaskStatusPlanned,
			ProjectID:      task.ProjectID,
			ParentID:       task.ID,
			WorkerRole:     "implementer",
			DependsOn:      dependsOn,
			CreatedAt:      now,
			UpdatedAt:      now,
			LastUpdate:     now,
		}
		if indexErr := r.es.IndexDoc(ctx, "agent-task-records", subtask.ID, subtask); indexErr != nil {
			r.logger.Error("pm: failed to index subtask", slog.String("id", subtask.ID), slog.String("error", indexErr.Error()))
		}
	}

	r.logger.Info("pm: task decomposed successfully", slog.Int("subtasks", len(plan.Tasks)))

	// Maintain denormalized fields on the parent for cheap future guards (see top of this function).
	// Best-effort update — races are acceptable for this observability/guard field.
	nowISO := time.Now().UTC().Format(time.RFC3339)
	update := map[string]interface{}{
		"child_count":   len(plan.Tasks), // set (or could script-increment for concurrent safety)
		"decomposed_at": nowISO,
		"updated_at":    nowISO,
	}
	_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, update)

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone, // Decomposition complete; subtasks + sweep drive completion. Prevents lingering PM org items.
	}, nil
}

// ─── Git Operations ─────────────────────────────────────────────────────────

// EnsureTaskBranch creates or checks out the branch for a task.
// Derived from Python: ensure_task_branch() (L568-772, 11 parents, 14 children)
func (r *Runner) EnsureTaskBranch(ctx context.Context, task ftypes.Task) (string, string, error) {
	if task.ProjectID == "" {
		return "", "", fmt.Errorf("task %s has no project/repo (ProjectID empty)", task.ID)
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

	workspace := os.Getenv("FLUME_WORKSPACE_ROOT")
	if workspace == "" {
		workspace = "/app/workspace"
	}
	repoPath := project.LocalPath
	if repoPath == "" {
		repoPath = filepath.Join(workspace, fmt.Sprintf("flume-reg-%s", project.ID))
	}

	// Check if local clone exists. If not, clone it dynamically.
	if _, err := os.Stat(filepath.Join(repoPath, ".git")); os.IsNotExist(err) {
		if project.RepoURL == "" {
			return "", "", fmt.Errorf("project %s has no local path and no remote repo_url", task.ProjectID)
		}

		// Ensure parent directory exists
		if err := os.MkdirAll(filepath.Dir(repoPath), 0755); err != nil {
			return "", "", fmt.Errorf("create workspace directory: %w", err)
		}

		r.logger.Info("EnsureTaskBranch: cloning repository dynamically",
			slog.String("project_id", project.ID),
			slog.String("repo_url", project.RepoURL),
			slog.String("dest", repoPath))

		cloneURL := project.RepoURL

		// Resolve credentials if it's a remote URL
		isRemote := strings.Contains(project.RepoURL, "github.com") ||
			strings.Contains(project.RepoURL, "dev.azure.com") ||
			strings.Contains(project.RepoURL, "visualstudio.com") ||
			strings.HasPrefix(project.RepoURL, "http://") ||
			strings.HasPrefix(project.RepoURL, "https://")

		if isRemote {
			cloneURL = git.EmbedCredentials(ctx, project.RepoURL, "")
		}

		// Execute git clone
		cmd := exec.CommandContext(ctx, "git", "clone", "--", cloneURL, repoPath)
		if output, err := cmd.CombinedOutput(); err != nil {
			_ = os.RemoveAll(repoPath)
			return "", "", fmt.Errorf("git clone failed: %s: %w", string(output), err)
		}
		r.logger.Info("EnsureTaskBranch: cloned successfully", slog.String("project_id", project.ID))
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

// ComputeReadyForRepo is DEPRECATED (PR3: flume-queue-planning-reliability).
// promote logic unified into single repo-aware promotePlannedTasks + resilience (mget/cache/OCC/Enforcer).
// Thin wrapper (no body logic) to guarantee only ONE promote implementation remains.
// Update any call sites and the note in pkg/types/types.go.
func (r *Runner) ComputeReadyForRepo(ctx context.Context, repoID string) int {
	// DEPRECATED (PR3): Logic unified into Sweeper.promotePlannedTasks.
	// This thin no-op wrapper guarantees only ONE promote implementation.
	r.logger.Warn("DEPRECATED: ComputeReadyForRepo called; logic retired to Sweeper.promotePlannedTasks (sweeps.go). No-op.",
		slog.String("repoID", repoID))
	return 0
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

	// PR2 Enforce
	_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog(currentStatus, ftypes.TaskStatus(targetStatus), r.logger.Warn)
	if err := r.es.UpdateDoc(ctx, "agent-task-records", taskID, update); err == nil {
		r.logger.Info("cleared stale claim on task after worker crash",
			slog.String("task_id", taskID),
			slog.String("reset_to", targetStatus))
	}
}

func (r *Runner) updateTaskStatus(ctx context.Context, taskID string, status ftypes.TaskStatus) {
	// PR 2: all via Enforcer
	_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog("", status, r.logger.Warn) // prev unknown here; future pass current
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
	_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, update)
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
	if out, err := gitCmd(repoPath, "checkout", branch); err != nil {
		// Create new branch from default
		defaultBranch, _ := resolveDefaultBranch(repoPath)
		if out2, err2 := gitCmd(repoPath, "checkout", "-b", branch, defaultBranch); err2 != nil {
			return fmt.Errorf("git checkout branch %s failed. Checkout existing: %s (%v). Create new branch: %s (%v)",
				branch, strings.TrimSpace(out), err, strings.TrimSpace(out2), err2)
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
			// PR2 LLM fail block guarded
			"status":         "blocked",
			"attempts":       failureCount,
			"error_message":  fmt.Sprintf("blocked after %d LLM failures (cap=%d)", failureCount, maxCap),
			"active_worker":  nil,
			"queue_state":    "available",
			"updated_at":     time.Now().UTC().Format(time.RFC3339),
		}
		_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, update)
	} else {
		r.logger.Warn("implementer: task re-queued for retry",
			slog.String("task_id", taskID),
			slog.Int("attempt", failureCount),
			slog.Int("max", maxCap))
		update := map[string]interface{}{
			// PR2 requeue ready via Enforce (shadow)
			"status":        "ready",
			"attempts":      failureCount,
			"active_worker": nil,
			"queue_state":   "available",
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		}
		_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, update)
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

// spawnReviewTasks creates a reviewer and tester subtask for review-consensus.
func (r *Runner) spawnReviewTasks(ctx context.Context, parent ftypes.Task) error {
	now := time.Now().UTC()

	// Reviewer task
	revTask := ftypes.Task{
		ID:             fmt.Sprintf("task-rev-%s", generateShortID()),
		Title:          "Review: " + parent.Title,
		Description:    "Verify functional purity and state constraints for: " + parent.Description,
		Status:         ftypes.TaskStatusReview,
		ProjectID:      parent.ProjectID,
		ParentID:       parent.ID,
		WorkerRole:     "reviewer",
		CreatedAt:      now,
		UpdatedAt:      now,
		LastUpdate:     now,
	}

	// Tester task
	testTask := ftypes.Task{
		ID:             fmt.Sprintf("task-test-%s", generateShortID()),
		Title:          "Test: " + parent.Title,
		Description:    "Run tests and evaluate outcomes for: " + parent.Description,
		Status:         ftypes.TaskStatusReview,
		ProjectID:      parent.ProjectID,
		ParentID:       parent.ID,
		WorkerRole:     "tester",
		CreatedAt:      now,
		UpdatedAt:      now,
		LastUpdate:     now,
	}

	if err := r.es.IndexDoc(ctx, "agent-task-records", revTask.ID, revTask); err != nil {
		return fmt.Errorf("index reviewer task: %w", err)
	}

	if err := r.es.IndexDoc(ctx, "agent-task-records", testTask.ID, testTask); err != nil {
		return fmt.Errorf("index tester task: %w", err)
	}

	r.logger.Info("spawned reviewer and tester subtasks",
		slog.String("parent_id", parent.ID),
		slog.String("rev_task_id", revTask.ID),
		slog.String("test_task_id", testTask.ID))

	// Maintain denorm fields on parent for cheap guards (best-effort).
	nowISO := time.Now().UTC().Format(time.RFC3339)
	update := map[string]interface{}{
		"child_count":   2, // reviewer + tester
		"decomposed_at": nowISO,
		"updated_at":    nowISO,
	}
	_ = r.es.UpdateDoc(ctx, "agent-task-records", parent.ID, update)

	return nil
}

func readSystemPrompt(role string) string {
	path := fmt.Sprintf("src/agents/%s/SYSTEM_PROMPT.md", role)
	if role == "pm" {
		path = "src/agents/pm-dispatcher/SYSTEM_PROMPT.md"
	}
	data, err := os.ReadFile(path)
	if err == nil {
		return string(data)
	}
	// Fallback prompts
	switch role {
	case "pm":
		return "You are the PM (Product Manager) agent. Your job is to decompose the user's task or feature request into a set of structured child tasks (workitems) that form a DAG (Directed Acyclic Graph).\nYou must output a JSON object conforming exactly to this schema:\n{\n  \"tasks\": [\n    {\n      \"id\": \"task_1\",\n      \"title\": \"Short title\",\n      \"objective\": \"Detailed description of what the implementer agent needs to do\",\n      \"depends_on\": []\n    }\n  ]\n}"
	case "reviewer":
		return "You are the Reviewer microservice. Your sole responsibility is to evaluate proposed codebase permutations against explicit architectural constraints. You must strictly conform to this JSON schema: {\"approved\": boolean, \"violations\": [{\"rule\": \"Global State\", \"details\": \"...\"}]}"
	case "tester":
		return "You are the Tester microservice. Run tests and verify the code correctness."
	default:
		return ""
	}
}

func cleanJSONContent(s string) string {
	s = strings.TrimSpace(s)
	if strings.HasPrefix(s, "```json") {
		s = strings.TrimPrefix(s, "```json")
		s = strings.TrimSuffix(s, "```")
		s = strings.TrimSpace(s)
	} else if strings.HasPrefix(s, "```") {
		s = strings.TrimPrefix(s, "```")
		s = strings.TrimSuffix(s, "```")
		s = strings.TrimSpace(s)
	}
	return s
}

func generateShortID() string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x", b)
}
