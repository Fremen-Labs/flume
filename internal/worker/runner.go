package worker

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
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
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// errSpawnGuardFired is a sentinel error returned by spawnReviewTasks when the
// idempotency guard prevents spawning because reviewer/tester children already
// exist for this parent. The caller uses this to distinguish "guard fired" from
// "actual spawn failure" and can skip re-review instead of looping.
var errSpawnGuardFired = fmt.Errorf("spawn guard: reviewer/tester children already exist")

// updateTaskLeaseState is the single choke point for mutating the core lease
// columns of the Work Queue (status, active_worker, queue_state, claimed_at,
// error_message, etc.).
//
// It enforces the TaskStateMachine, emits the mandatory dual Log* observability
// required by the flume-go SKILL, and prefers OCC when seq/prim are supplied.
//
// This implements the "Cross-cutting Writer Rule" from the Phase 3 column
// design document and directly mitigates bare UpdateDoc races on contended
// claim/lease state (a major contributor to thundering herd and status fights
// identified in the 2026-05-30 review).
//
// All future mutations of these columns (in runner, sweeps, claimer, and
// dashboard paths where possible) should route through this helper.
func updateTaskLeaseState(
	ctx context.Context,
	esClient *es.Client,
	logger *slog.Logger,
	taskID string,
	update map[string]interface{},
	seq, prim int64,
	prevStatus ftypes.TaskStatus,
	targetStatus ftypes.TaskStatus,
	workerRole string,
	reason string,
) error {
	// 1. State machine enforcement (shadow mode supported)
	_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog(prevStatus, targetStatus, logger.Warn)

	// 2. Write with OCC preference when we have seq/prim from a prior search
	var err error
	if seq > 0 && prim > 0 {
		err = esClient.UpdateDocOCC(ctx, "agent-task-records", taskID, update, seq, prim)
	} else {
		err = esClient.UpdateDoc(ctx, "agent-task-records", taskID, update)
	}

	if err != nil {
		logger.Warn("lease state update failed",
			slog.String("task_id", taskID),
			slog.String("target_status", string(targetStatus)),
			slog.String("error", err.Error()),
			slog.String("reason", reason))
		return err
	}

	// 3. Mandatory rich observability for popout + Logloom (flume-go SKILL)
	flumelogger.LogStateTransition(ctx, taskID, string(prevStatus), string(targetStatus), reason)

	flumelogger.LogAgentReasoning(ctx, taskID, workerRole, reason, map[string]any{
		"previous_status": string(prevStatus),
		"target_status":   string(targetStatus),
		"worker_role":     workerRole,
		"reason":          reason,
		"columns_mutated": "status,active_worker,queue_state",
	})

	logger.Info("lease state updated",
		slog.String("task_id", taskID),
		slog.String("from", string(prevStatus)),
		slog.String("to", string(targetStatus)),
		slog.String("role", workerRole),
		slog.String("reason", reason))

	return nil
}

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
		// Python model (v0.1.126): implementer completes → task transitions to
		// "review" status → reviewer worker claims the SAME task (role rotation).
		// No child task spawning. This keeps 1 task as 1 task throughout its lifecycle.
		// result.NextStatus is already "review" from handleImplementer — let it pass through.
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

		// Emit structured reasoning for the crash (feeds popout + Logloom). This was missing
		// on the dominant failure paths (PM parse storms, gateway 120s timeouts) in the field test.
		flumelogger.LogAgentReasoning(ctx, taskID, worker.Role,
			fmt.Sprintf("Worker %s failed: %s. Stale claim will be cleared and task reset for recovery.", worker.Name, err.Error()),
			map[string]any{
				"worker": worker.Name,
				"role":   worker.Role,
				"error":  err.Error(),
			})

		// Detect persistent LLM config errors (404, DNS failures) that won't self-heal.
		// Escalate immediately to "blocked" instead of allowing infinite retry loops
		// (the dominant failure mode in the post-migration field test).
		// Per reliable-go-systems + flume-go SKILLs: use typed sentinel (ErrPersistentConfig)
		// + errors.Is instead of brittle strings (see llm/client.go).
		errStr := err.Error()
		isPersistentConfigError := errors.Is(err, llm.ErrPersistentConfig) ||
			strings.Contains(errStr, "404") ||
			strings.Contains(errStr, "no such host")

		if isPersistentConfigError {
			reason := fmt.Sprintf("Worker %s (role=%s) hit persistent config error (will not self-heal): %s",
				worker.Name, worker.Role, errStr)

			update := map[string]interface{}{
				"status":        "blocked",
				"error_message": reason,
				"active_worker": nil,
				"queue_state":   "available",
				"updated_at":    time.Now().UTC().Format(time.RFC3339),
			}

			// Use the central lease state helper (Phase 3a-2).
			// This removes a bare UpdateDoc on the critical lease columns (status/active_worker/queue_state)
			// and guarantees Enforce + dual Log* emission.
			_ = updateTaskLeaseState(ctx, r.es, r.logger, taskID, update,
				0, 0, // no OCC seq/prim available in this error path yet
				task.Status, ftypes.TaskStatusBlocked,
				worker.Role, reason)

			return err
		}

		// Clear stale claim (now also emits LogStateTransition + LogAgentReasoning via helper)
		r.clearStaleClaim(ctx, taskID, task.Status, worker.Role)
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

	flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
		fmt.Sprintf("Starting implementation of task: %s", task.Title),
		map[string]any{"phase": "start", "worker": worker.Name})

	// Check if task requires code
	if !TaskRequiresCode(task) {
		r.logger.Info("implementer: non-code task, completing",
			slog.String("task_id", task.ID))
		flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
			"Task does not require code changes. Marking as done.",
			map[string]any{"phase": "complete", "reason": "non_code_task"})
		return ftypes.AgentResult{
			Success:    true,
			NextStatus: ftypes.TaskStatusDone,
		}, nil
	}

	// 1. Ensure task branch exists
	repoPath, branch, err := r.EnsureTaskBranch(ctx, task)
	if err != nil {
		flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
			fmt.Sprintf("Failed to set up task branch: %s", err.Error()),
			map[string]any{"phase": "branch_setup", "error": err.Error()})
		return ftypes.AgentResult{
			Success: false,
			Errors:  []string{err.Error()},
		}, err
	}

	flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
		fmt.Sprintf("Branch ready: %s. Executing agent loop against repository.", branch),
		map[string]any{"phase": "branch_ready", "branch": branch, "repo_path": repoPath})

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
		flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
			fmt.Sprintf("Auto-commit failed: %s. Proceeding to review.", err.Error()),
			map[string]any{"phase": "commit", "error": err.Error()})
	}

	// 4. Create PR if branch has new commits
	if commitSHA != "" {
		r.logger.Info("implementer: committed and pushed",
			slog.String("task_id", task.ID),
			slog.String("sha", commitSHA))
		flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
			fmt.Sprintf("Changes committed and pushed (SHA: %s). Transitioning to review.", commitSHA[:min(len(commitSHA), 8)]),
			map[string]any{"phase": "complete", "commit_sha": commitSHA})
	} else {
		flumelogger.LogAgentReasoning(ctx, task.ID, "implementer",
			"Implementation complete. No new commits detected. Transitioning to review.",
			map[string]any{"phase": "complete", "had_commits": false})
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
						if os.Getenv("FLUME_NATIVE_MODE") == "1" {
							// Native mode: use current directory, not Docker path.
							workspace, _ = os.Getwd()
							if workspace == "" {
								workspace = "."
							}
						} else {
							workspace = "/app/workspace"
						}
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

	diffLen := len(diffOut)
	flumelogger.LogAgentReasoning(ctx, task.ID, "reviewer",
		fmt.Sprintf("Reviewing implementation for task: %s. Diff size: %d chars. Sending to LLM for analysis.", parent.Title, diffLen),
		map[string]any{"phase": "llm_review", "diff_size": diffLen, "parent_task": parent.ID})

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
		// Use shared LLM failure handler with retry-cap instead of raw error return
		// (prevents infinite retry loops when gateway/Ollama is misconfigured)
		r.handleRoleLLMFailure(ctx, task.ID, task, worker.Role)
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

	flumelogger.LogAgentReasoning(ctx, task.ID, "reviewer",
		fmt.Sprintf("Code review complete. Verdict: %s. Analyzed diff for task: %s", verdict, parent.Title),
		map[string]any{"phase": "complete", "verdict": verdict, "parent_task": parent.ID})

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

	flumelogger.LogAgentReasoning(ctx, task.ID, "tester",
		"Starting test execution. Checking out branch and running test suite.",
		map[string]any{"phase": "start", "worker": worker.Name})

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

	flumelogger.LogAgentReasoning(ctx, task.ID, "tester",
		fmt.Sprintf("Test execution complete. Verdict: %s.", verdict),
		map[string]any{"phase": "complete", "verdict": verdict, "parent_task": parent.ID,
			"feedback_preview": firstNChars(feedback, 200)})

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

	// === Hard status guard: never re-decompose a task that has already left planned/ready ===
	// This catches race conditions where a done/blocked task ends up back in the claim pool.
	if task.Status == ftypes.TaskStatusDone || task.Status == ftypes.TaskStatusArchived || task.Status == ftypes.TaskStatusBlocked {
		r.logger.Info("pm: skipping — task already in terminal/blocked status",
			slog.String("task_id", task.ID),
			slog.String("status", string(task.Status)))
		return ftypes.AgentResult{Success: true, NextStatus: task.Status}, nil
	}

	// === Anti-explosion guard: skip decomposition for intake-created hierarchy nodes ===
	// Epics, features, and stories are organizational containers created by buildTaskHierarchy().
	// They already have children and should NEVER be sent to the LLM for decomposition.
	// Only items with ItemType "task" (or empty, for legacy compatibility) are valid PM targets.
	if task.ItemType == "epic" || task.ItemType == "feature" || task.ItemType == "story" {
		reason := fmt.Sprintf("PM decomposition skipped: %q is an intake-created hierarchy node (item_type=%s), not a decomposition target", task.Title, task.ItemType)
		flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{"item_type": task.ItemType})
		r.logger.Info("pm: skipping decomposition — intake-created hierarchy node",
			slog.String("task_id", task.ID),
			slog.String("item_type", task.ItemType),
			slog.String("title", task.Title))
		return ftypes.AgentResult{Success: true, NextStatus: ftypes.TaskStatusDone}, nil
	}
	// === HARD emergency stop guard (the missing piece when user hit "halt the swarm") ===
	if r.isWorkPaused(ctx, task.ProjectID) {
		reason := "PM decomposition blocked: project work is paused (emergency halt)"
		flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{"repo": task.ProjectID})
		r.logger.Warn("handlePM blocked by work pause", slog.String("task_id", task.ID), slog.String("repo", task.ProjectID))
		return ftypes.AgentResult{Success: true, NextStatus: ftypes.TaskStatusBlocked}, nil
	}

	// === Anti-re-decomposition guard (core fix for workitem explosion) ===
	// Prefer the cheap denormalized ChildCount / DecomposedAt (populated by previous decompositions
	// and by intake creation). Fall back to a small child search if the denorm fields are not yet set.
	//
	// Hardened (post 74-task incident): also treat a recent failed decomp attempt (even if no children
	// were created because the LLM call failed early) as a reason to back off. This breaks the
	// "gateway down → error → clearStaleClaim → re-claim → repeat decomp attempt" storm.
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

	// New backoff for recent failed attempts (recorded on every early LLM failure path)
	if task.DecompLastAttemptAt != nil {
		age := time.Since(*task.DecompLastAttemptAt)
		if age < 2*time.Minute {  // conservative backoff window while LLM/gateway is unhealthy
			reason := fmt.Sprintf("recent PM decomp attempt failed %s ago (gateway or LLM issue) — backing off to prevent retry storm", age.Round(time.Second))
			flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{"age": age.String()})
			r.logger.Info("pm: skipping — recent failed decomp attempt, backing off",
				slog.String("task_id", task.ID),
				slog.Duration("attempt_age", age))
			return ftypes.AgentResult{Success: true, NextStatus: ftypes.TaskStatusReady}, nil
		}
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

	// === Phase 2 BUDGET ENFORCEMENT in handlePM (before expensive LLM call) ===
	// Load plan session (keyed by task.PlanSessionID). If proposed children (use conservative maxChildrenPerParent or parsed later)
	// would exceed, block the *parent* with rich LogAgentReasoning + audit event + set blocked.
	// This is the primary defense against a bad LLM plan from a PM causing cascade explosions.
	// "fail closed". Also used for depth check in task 2.
	if task.PlanSessionID != "" {
		sessBytes, sessErr := r.es.GetDoc(ctx, "agent-plan-sessions", task.PlanSessionID)
		if sessErr == nil && sessBytes != nil {
			var sess struct {
				ItemBudget   int    `json:"item_budget"`
				CurrentItems int    `json:"current_items"`
				BudgetStatus string `json:"budget_status"`
				ID           string `json:"id"`
			}
			if json.Unmarshal(sessBytes, &sess) == nil {
				// Conservative proposed: we haven't called LLM yet; use the existing hard cap as estimate.
				// After LLM parse (below) we do a precise re-check with actual len(plan.Tasks) before child creation + counter inc.
				proposedEstimate := 15 // matches const maxChildrenPerParent later in this function
				if sess.BudgetStatus == "exceeded" || (sess.ItemBudget > 0 && sess.CurrentItems+proposedEstimate > sess.ItemBudget) {
					reason := fmt.Sprintf("PM decomp blocked by plan budget: session %s current=%d + est=%d > budget=%d", sess.ID, sess.CurrentItems, proposedEstimate, sess.ItemBudget)
					flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{
						"plan_session_id": task.PlanSessionID,
						"current_items":   sess.CurrentItems,
						"item_budget":     sess.ItemBudget,
						"action":          "blocked_pre_llm_budget",
					})
					r.logger.Warn("handlePM: plan budget would be exceeded — blocking parent pre-LLM (fail closed)",
						slog.String("task_id", task.ID), slog.String("plan_session", task.PlanSessionID), slog.String("reason", reason))
					_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
						"status":        "blocked",
						"error_message": reason,
						"updated_at":    time.Now().UTC().Format(time.RFC3339),
					})
					// Return blocked as NextStatus with nil error so RunWorker does NOT call clearStaleClaim.
					// This prevents the PM re-decomposition loop (GAP-3 / Phase 1).
					return ftypes.AgentResult{
						Success:    false,
						Errors:     []string{reason},
						NextStatus: ftypes.TaskStatusBlocked,
					}, nil
				}
			}
		}
	}

	pmSystemPrompt := readSystemPrompt("pm")
	req := llm.ChatRequest{
		Messages: []llm.Message{
			{Role: "system", Content: pmSystemPrompt},
			{Role: "user", Content: fmt.Sprintf("Please decompose the following task:\nTitle: %s\nObjective: %s", task.Title, task.Description)},
		},
		Model:         worker.Model,
		Provider:      worker.Provider,
		AgentRole:     "pm",
		TaskID:        task.ID,
		PlanSessionID: task.PlanSessionID, // Phase 2: enables gateway per-plan-pm rate limiter + budget context
	}

	resp, err := r.llm.Chat(ctx, req)
	if err != nil {
		flumelogger.LogAgentReasoning(ctx, task.ID, "pm", "PM LLM call to gateway/mesh failed during decomposition", map[string]any{
			"error": err.Error(),
			"model": worker.Model,
		})

		// Quick hardening for failure-induced retry storms (the exact pattern seen in the 74-task explosion).
		// Even on early LLM failure (before any children are created), mark the attempt so the
		// anti-re-decomp guard can see "recent failure, back off" on subsequent claims.
		now := time.Now().UTC()
		_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
			"decomp_last_attempt_at": now.Format(time.RFC3339),
			"decomp_failures":        (task.ChildCount + 1), // reuse field or just set a marker; real counter can be added later
			"error_message":          "PM LLM failure (gateway unreachable or similar) - decomp attempt recorded for backoff",
			"updated_at":             now.Format(time.RFC3339),
		})

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
		// === PM JSON resilience (highest-leverage local-LLM fix) ===
		// Local models (even qwen3.5 35b) frequently emit leading text, ".", or markdown
		// before the JSON. This single path was responsible for the entire 40+ minute
		// death-spiral storm in the field test (75-120s calls, gateway collapse, repeated
		// stale-claim resets, shadow violations).
		r.logger.Warn("pm: initial JSON parse failed, attempting one repair LLM call",
			slog.String("task_id", task.ID),
			slog.String("error", err.Error()),
			slog.Int("raw_len", len(resp.Content)))

		flumelogger.LogAgentReasoning(ctx, task.ID, "pm",
			"Initial subtask plan JSON parse failed (LLM output did not start with valid JSON). Attempting automatic repair with strict JSON-only instruction.",
			map[string]any{
				"raw_prefix":     firstNChars(resp.Content, 200),
				"parse_error":    err.Error(),
				"repair_attempt": 1,
			})

		// One repair attempt with a very strict prompt (re-uses same model for simplicity).
		repairReq := llm.ChatRequest{
			Messages: []llm.Message{
				{Role: "system", Content: "You are a JSON repair assistant. Output ONLY a single valid JSON object. No markdown, no explanations, no leading or trailing text."},
				{Role: "user", Content: fmt.Sprintf("The following text is supposed to be a JSON object with a top-level 'tasks' array matching this schema exactly:\n{\n  \"tasks\": [ {\"id\": \"task_1\", \"title\": \"...\", \"objective\": \"...\", \"depends_on\": [] } ]\n}\n\nPrevious model output (first 800 chars):\n%s\n\nRe-emit ONLY the corrected JSON object. Start with '{' and end with '}'.", firstNChars(resp.Content, 800))},
			},
			Model:         worker.Model,
			Provider:      worker.Provider,
			AgentRole:     "pm",
			TaskID:        task.ID,
			PlanSessionID: task.PlanSessionID,
		}
		repairResp, repairErr := r.llm.Chat(ctx, repairReq)
		repairSucceeded := false
		if repairErr == nil {
			repairContent := cleanJSONContent(repairResp.Content)
			if uerr := json.Unmarshal([]byte(repairContent), &plan); uerr == nil && len(plan.Tasks) > 0 {
				repairSucceeded = true
				r.logger.Info("pm: repair LLM call succeeded", slog.String("task_id", task.ID), slog.Int("tasks", len(plan.Tasks)))
				flumelogger.LogAgentReasoning(ctx, task.ID, "pm", "Repair LLM call produced valid subtask plan JSON after initial failure.", map[string]any{"tasks": len(plan.Tasks)})
			}
		}
		if !repairSucceeded {
			// Circuit breaker: hard failure after repair attempt. Block to stop the retry storm.
			reason := "PM failed to produce parseable subtask JSON even after one repair attempt (local LLM output format issue)"
			flumelogger.LogStateTransition(ctx, task.ID, string(task.Status), "blocked", reason)
			flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{
				"raw_prefix":       firstNChars(resp.Content, 300),
				"repair_attempted": true,
				"repair_error":     repairErr,
				"model":            worker.Model,
				"provider":         worker.Provider,
				"action":           "blocked_to_prevent_storm",
			})
			r.logger.Error("pm: JSON parse failed after repair — blocking task to prevent retry storm",
				slog.String("task_id", task.ID), slog.String("raw_prefix", firstNChars(resp.Content, 120)))

			// Persist the block (best effort)
			_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
				"status":        "blocked",
				"error_message": reason,
				"updated_at":    time.Now().UTC().Format(time.RFC3339),
			})
			// Return blocked as NextStatus with nil error (GAP-3 / Phase 1).
			// Prevents clearStaleClaim from resetting a permanently failed PM back to planned.
			return ftypes.AgentResult{
				Success:    false,
				Errors:     []string{reason},
				NextStatus: ftypes.TaskStatusBlocked,
			}, nil
		}
		// If we reach here, plan was populated by the repair response — fall through to creation.
	}

	// === HARD per-parent child cap (prevents one bad decomposition from creating 50+ items) ===
	const maxChildrenPerParent = 15 // conservative; tune via env if needed for very large legitimate epics
	if len(plan.Tasks) > maxChildrenPerParent {
		reason := fmt.Sprintf("PM decomposition refused: plan would create %d children (cap=%d). This is the primary explosion vector.", len(plan.Tasks), maxChildrenPerParent)
		flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{
			"attempted_children": len(plan.Tasks),
			"cap":                maxChildrenPerParent,
			"repo":               task.ProjectID,
		})
		r.logger.Error("pm: child cap exceeded — blocking parent to prevent explosion",
			slog.String("task_id", task.ID), slog.Int("attempted", len(plan.Tasks)), slog.Int("cap", maxChildrenPerParent))

		_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
			"status":        "blocked",
			"error_message": reason,
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		})
		return ftypes.AgentResult{Success: false, Errors: []string{reason}}, fmt.Errorf("%s", reason)
	}

	// === Phase 2 precise plan budget re-check (post-LLM parse, pre-create) ===
	// Now that we have exact len(plan.Tasks), re-validate against session budget.
	// On exceed: rich LogAgentReasoning + block + audit. Atomic inc only on success path.
	if task.PlanSessionID != "" {
		sessBytes, _ := r.es.GetDoc(ctx, "agent-plan-sessions", task.PlanSessionID)
		if sessBytes != nil {
			var sess struct {
				ItemBudget   int    `json:"item_budget"`
				CurrentItems int    `json:"current_items"`
				BudgetStatus string `json:"budget_status"`
				ID           string `json:"id"`
			}
			if json.Unmarshal(sessBytes, &sess) == nil {
				proposed := len(plan.Tasks)
				if sess.BudgetStatus == "exceeded" || (sess.ItemBudget > 0 && sess.CurrentItems+proposed > sess.ItemBudget) {
					reason := fmt.Sprintf("PM decomp refused by plan budget post-parse: %d children would exceed (session %s current=%d cap=%d)", proposed, sess.ID, sess.CurrentItems, sess.ItemBudget)
					flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{
						"plan_session_id":     task.PlanSessionID,
						"attempted_children":  proposed,
						"current_items":       sess.CurrentItems,
						"item_budget":         sess.ItemBudget,
						"action":              "blocked_exact_budget",
					})
					r.logger.Error("pm: exact plan budget exceeded after LLM parse — blocking (fail closed)",
						slog.String("task_id", task.ID), slog.Int("attempted", proposed))
					_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
						"status":        "blocked",
						"error_message": reason,
						"updated_at":    time.Now().UTC().Format(time.RFC3339),
					})
					// Return blocked + nil error (GAP-3 consistency).
					return ftypes.AgentResult{
						Success:    false,
						Errors:     []string{reason},
						NextStatus: ftypes.TaskStatusBlocked,
					}, nil
				}
			}
		}
	}

	// === Phase 2 DEPTH ENFORCEMENT in handlePM (before child creation) ===
	// Block if this decomp would create items at depth > MAX. Rich reasoning + block parent.
	if task.HierarchyDepth+1 > ftypes.MAX_HIERARCHY_DEPTH {
		reason := fmt.Sprintf("PM decomp blocked: would create children at depth %d > MAX_HIERARCHY_DEPTH=%d", task.HierarchyDepth+1, ftypes.MAX_HIERARCHY_DEPTH)
		flumelogger.LogAgentReasoning(ctx, task.ID, "pm", reason, map[string]any{
			"current_depth": task.HierarchyDepth,
			"would_be":      task.HierarchyDepth + 1,
			"max":           ftypes.MAX_HIERARCHY_DEPTH,
			"plan_session_id": task.PlanSessionID,
		})
		r.logger.Error("pm: depth limit exceeded — blocking parent to prevent nesting explosion",
			slog.String("task_id", task.ID), slog.Int("depth", task.HierarchyDepth))
		_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
			"status":        "blocked",
			"error_message": reason,
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		})
		return ftypes.AgentResult{Success: false, Errors: []string{reason}}, fmt.Errorf("%s", reason)
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
			// Phase 2 propagation for budget/depth enforcement (task 1+2)
			PlanSessionID:  task.PlanSessionID,
			HierarchyDepth: task.HierarchyDepth + 1,
			LastUpdate:     now,
		}
		if indexErr := r.es.IndexDoc(ctx, "agent-task-records", subtask.ID, subtask); indexErr != nil {
			r.logger.Error("pm: failed to index subtask", slog.String("id", subtask.ID), slog.String("error", indexErr.Error()))
		}
	}

	r.logger.Info("pm: task decomposed successfully", slog.Int("subtasks", len(plan.Tasks)))

	// Structured success reasoning for the popout / Logloom (symmetric to the failure path above).
	flumelogger.LogAgentReasoning(ctx, task.ID, "pm",
		fmt.Sprintf("Successfully decomposed parent task into %d child subtasks (DAG).", len(plan.Tasks)),
		map[string]any{
			"subtask_count": len(plan.Tasks),
			"parent_title":  task.Title,
		})

	// Set child_count and decomposed_at via direct UpdateDoc first (synchronous, reliable).
	// The inline script below is a secondary atomic counter increment for concurrent PM claims,
	// but the direct set ensures the anti-re-decomp guard always has data even if the
	// script races with updateTaskStatus or fails silently.
	nowISO := time.Now().UTC().Format(time.RFC3339)
	_ = r.es.UpdateDoc(ctx, "agent-task-records", task.ID, map[string]interface{}{
		"child_count":   len(plan.Tasks),
		"decomposed_at": nowISO,
		"updated_at":    nowISO,
	})

	// Authoritative ChildCount via atomic ES inline script (task 3) — secondary reinforcement.
	delta := len(plan.Tasks)
	scriptSrc := `ctx._source.child_count = (ctx._source.child_count != null ? ctx._source.child_count : 0) + params.delta; ctx._source.decomposed_at = params.now; ctx._source.updated_at = params.now;`
	_ = r.es.UpdateDocWithInlineScript(ctx, "agent-task-records", task.ID, scriptSrc, map[string]interface{}{
		"delta": delta,
		"now":   nowISO,
	})

	// Phase 2: atomic plan budget increment on successful decomp (item delta = children created)
	if task.PlanSessionID != "" {
		_ = r.es.UpdateDocWithInlineScript(ctx, "agent-plan-sessions", task.PlanSessionID, `
			if (ctx._source.current_items == null) { ctx._source.current_items = 0; }
			ctx._source.current_items += params.delta;
			if (ctx._source.item_budget != null && ctx._source.item_budget > 0 && ctx._source.current_items > ctx._source.item_budget) {
				ctx._source.budget_status = "exceeded";
			}
			ctx._source.updated_at = params.now;
		`, map[string]interface{}{
			"delta": len(plan.Tasks),
			"now":   nowISO,
		})
	}

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
		if os.Getenv("FLUME_NATIVE_MODE") == "1" {
			// Native mode: use current directory, not Docker path.
			workspace, _ = os.Getwd()
			if workspace == "" {
				workspace = "."
			}
		} else {
			workspace = "/app/workspace"
		}
	}
	repoPath := project.LocalPath
	if repoPath == "" {
		repoPath = filepath.Join(workspace, fmt.Sprintf("flume-reg-%s", project.ID))
		r.logger.Info("EnsureTaskBranch: resolved workspace (project.LocalPath was empty)",
			slog.String("project_id", task.ProjectID),
			slog.String("workspace", workspace),
			slog.String("repo_path", repoPath))
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

		// Execute git clone with explicit long timeout (GAP-1)
		cloneTimeout := 120 * time.Second // clones can be slow for large repos
		cloneCtx, cancel := context.WithTimeout(ctx, cloneTimeout)
		defer cancel()

		cmd := exec.CommandContext(cloneCtx, "git", "clone", "--", cloneURL, repoPath)
		if output, err := cmd.CombinedOutput(); err != nil {
			_ = os.RemoveAll(repoPath)
			return "", "", fmt.Errorf("git clone failed after %v: %s: %w", cloneTimeout, string(output), err)
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
	// Check if there are changes (short timeout)
	out, err := gitCmdWithTimeout(repoPath, 15*time.Second, "status", "--porcelain")
	if err != nil {
		class := classifyGitError(err, out)
		r.logger.Warn("auto_commit: git status failed",
			slog.String("task_id", taskID),
			slog.String("classified_as", class))
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

	// Commit (with classification on failure)
	if out, err := gitCmdWithTimeout(repoPath, 30*time.Second, "commit", "-m", message); err != nil {
		class := classifyGitError(err, out)
		r.logger.Error("auto_commit: commit failed",
			slog.String("task_id", taskID),
			slog.String("classified_as", class),
			slog.String("error", err.Error()))
		return "", fmt.Errorf("git commit: %w", err)
	}

	// Rebase before push (use longer timeout + classification)
	if _, err := gitCmdWithTimeout(repoPath, 60*time.Second, "pull", "--rebase", "origin", branch); err != nil {
		class := classifyGitError(err, "")
		// Check for rebase conflict
		if strings.Contains(err.Error(), "CONFLICT") || strings.Contains(err.Error(), "conflict") || class == "conflict" {
			r.logger.Error("auto_commit: rebase conflict detected",
				slog.String("task_id", taskID),
				slog.String("branch", branch))
			_, _ = gitCmdWithTimeout(repoPath, 15*time.Second, "rebase", "--abort")
			return "", fmt.Errorf("rebase conflict on %s", branch)
		}
		r.logger.Warn("auto_commit: pre-push rebase error",
			slog.String("task_id", taskID),
			slog.String("error", err.Error()),
			slog.String("classified_as", class))
	}

	// Push (explicit timeout)
	if _, err := gitCmdWithTimeout(repoPath, 60*time.Second, "push", "origin", branch); err != nil {
		class := classifyGitError(err, "")
		r.logger.Error("auto_commit: push failed",
			slog.String("task_id", taskID),
			slog.String("branch", branch),
			slog.String("error", err.Error()),
			slog.String("classified_as", class))
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
func (r *Runner) clearStaleClaim(ctx context.Context, taskID string, currentStatus ftypes.TaskStatus, workerRole string) {
	// Determine the correct reset target based on both the worker role and
	// current status. Reviewer/tester tasks should reset to "review" so they
	// can be re-claimed by the same role. Implementers reset to "ready".
	// PM tasks reset to "planned" so the promote sweep can re-evaluate them.
	targetStatus := "ready"
	switch workerRole {
	case "reviewer", "tester":
		targetStatus = "review"
	case "pm":
		targetStatus = "planned"

		// GAP-7 / Phase 1 hardening:
		// If this PM task already successfully decomposed (child_count > 0 or decomposed_at set),
		// do NOT reset it back to "planned". That would cause duplicate decomposition attempts
		// and feed the explosion loop. Instead treat it as completed decomposition.
		if doc, err := r.es.GetDoc(ctx, "agent-task-records", taskID); err == nil && doc != nil {
			var t ftypes.Task
			if json.Unmarshal(doc, &t) == nil {
				if t.ChildCount > 0 || t.DecomposedAt != nil {
					targetStatus = "done"
				}
			}
		}
	default:
		if currentStatus == ftypes.TaskStatusReview {
			targetStatus = "review"
		}
	}

	update := map[string]interface{}{
		"status":        targetStatus,
		"active_worker": nil,
		"queue_state":   "available",
		"updated_at":    time.Now().UTC().Format(time.RFC3339),
	}

	reason := fmt.Sprintf("stale claim cleared after worker crash or LLM/gateway failure (reset to %s for re-claim)", targetStatus)

	// Phase 3a-2: Route through the central lease state helper.
	// This guarantees consistent Enforce + dual Log* + reasoning on all claim/lease column mutations.
	// (Previously this path had its own direct UpdateDoc + duplicated logging.)
	if err := updateTaskLeaseState(ctx, r.es, r.logger, taskID, update,
		0, 0, // no OCC information at reset time in this path
		currentStatus, ftypes.TaskStatus(targetStatus),
		workerRole, reason); err == nil {
		r.logger.Info("cleared stale claim on task after worker crash",
			slog.String("task_id", taskID),
			slog.String("worker_role", workerRole),
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

	// Log state transition — feeds the Agent Reasoning popout + Logloom.
	flumelogger.LogStateTransition(ctx, taskID, "", string(status),
		fmt.Sprintf("Task transitioned to %s", status))

	_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, update)
}

// ─── Git Helpers ────────────────────────────────────────────────────────────

func gitCmd(repoPath string, args ...string) (string, error) {
	// Default safe timeout for most git operations (status, diff, symbolic-ref, etc.)
	return gitCmdWithTimeout(repoPath, 30*time.Second, args...)
}

// gitCmdWithTimeout executes a git command with an explicit timeout.
// This addresses GAP-1 (per-command git timeouts) from the Python→Go migration review.
func gitCmdWithTimeout(repoPath string, timeout time.Duration, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = repoPath
	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return string(out), fmt.Errorf("git %s timed out after %v: %w", strings.Join(args, " "), timeout, err)
	}
	return string(out), err
}

// classifyGitError provides basic classification of common git exit 128 / error patterns.
// This addresses GAP-2 (error classification for exit code 128).
func classifyGitError(err error, output string) string {
	if err == nil {
		return ""
	}
	combined := strings.ToLower(output + " " + err.Error())

	switch {
	case strings.Contains(combined, "authentication failed"),
		strings.Contains(combined, "could not read username"),
		strings.Contains(combined, "permission denied (publickey)"):
		return "auth"
	case strings.Contains(combined, "index.lock"):
		return "lock"
	case strings.Contains(combined, "conflict"):
		return "conflict"
	case strings.Contains(combined, "not a git repository"):
		return "not_repo"
	case strings.Contains(combined, "does not match any"):
		return "branch_not_found"
	case strings.Contains(combined, "network") || strings.Contains(combined, "connection") || strings.Contains(combined, "timeout"):
		return "network"
	default:
		return "unknown"
	}
}

func gitCheckoutBranch(repoPath, branch string) error {
	// Clean stale git locks from crashed processes before any operation.
	// This prevents the "Unable to create index.lock: File exists" error.
	cleanStaleLocks(repoPath)

	// Always try to refresh refs first — this prevents the "main is not a commit" and similar races
	// after crashes, resets, or dynamic clones that left the local state inconsistent.
	_, _ = gitCmdWithTimeout(repoPath, 30*time.Second, "fetch", "--all", "--prune", "--quiet")

	// Try checkout existing branch (fast path)
	if _, err := gitCmd(repoPath, "checkout", branch); err == nil {
		return nil
	} else {
		// Existing branch checkout failed — try to recover the default branch state
		defaultBranch, _ := resolveDefaultBranch(repoPath)

		// Hard reset + clean to get to a known clean state on the default branch
		_, _ = gitCmdWithTimeout(repoPath, 30*time.Second, "checkout", "-B", defaultBranch, "origin/"+defaultBranch) // force track origin
		_, _ = gitCmdWithTimeout(repoPath, 30*time.Second, "reset", "--hard", "origin/"+defaultBranch)
		_, _ = gitCmdWithTimeout(repoPath, 30*time.Second, "clean", "-fd")

		// Now attempt the feature branch creation from a known-good base.
		// Use -B (force-create) so an existing branch from a prior failed attempt
		// is reset to the clean base instead of causing "branch already exists" errors.
		if out2, err2 := gitCmdWithTimeout(repoPath, 30*time.Second, "checkout", "-B", branch, "origin/"+defaultBranch); err2 != nil {
			// Last resort: try creating from local HEAD if origin ref was also bad
			if out3, err3 := gitCmdWithTimeout(repoPath, 30*time.Second, "checkout", "-B", branch); err3 != nil {
				return fmt.Errorf("git checkout branch %s failed after recovery. Existing: %s (%v). From origin/%s: %s (%v). Last resort: %s (%v)",
					branch, strings.TrimSpace(string(err.Error())), err, defaultBranch, strings.TrimSpace(out2), err2, strings.TrimSpace(out3), err3)
			}
		}
	}
	return nil
}

// cleanStaleLocks removes stale .git/index.lock files left by crashed git processes.
// Only removes locks older than 30 seconds to avoid interfering with active operations.
func cleanStaleLocks(repoPath string) {
	lockFile := fmt.Sprintf("%s/.git/index.lock", strings.TrimRight(repoPath, "/"))
	info, err := os.Stat(lockFile)
	if err != nil {
		return // No lock file — nothing to clean
	}

	// Only remove if the lock is stale (older than 30 seconds)
	if time.Since(info.ModTime()) > 30*time.Second {
		if removeErr := os.Remove(lockFile); removeErr == nil {
			slog.Warn("git: removed stale index.lock",
				slog.String("repo", repoPath),
				slog.Duration("age", time.Since(info.ModTime())),
			)
		}
	}
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

var branchSanitizeRe = regexp.MustCompile(`[^a-zA-Z0-9._/-]+`)

// resolveBranchName generates a stable, safe git branch name for a task.
// ID/ParentID primary (guarantees uniqueness and avoids title-derived races).
// Title is used only as a short, sanitized human-readable suffix.
// This directly addresses repeated checkout failures seen when titles contain
// punctuation, spaces, or when the same logical work is retried after resets.
func resolveBranchName(task ftypes.Task) string {
	// Primary key for the branch — always stable even if title changes or on re-claims after failure
	scope := task.ID
	if task.ParentID != "" {
		scope = task.ParentID
	}

	hash := sha256.Sum256([]byte(scope + "|" + task.Title)) // include title for some human readability
	shortHash := hex.EncodeToString(hash[:4])

	// Title as optional suffix only — heavily sanitized and truncated
	segment := branchSanitizeRe.ReplaceAllString(task.Title, "-")
	segment = strings.Trim(segment, "-.")
	if len(segment) > 35 {
		segment = segment[:35]
	}
	segment = strings.Trim(segment, "-.")

	prefix := flumeAutoPRScope()

	if segment != "" {
		return fmt.Sprintf("%s/%s-%s", prefix, segment, shortHash)
	}
	return fmt.Sprintf("%s/task-%s", prefix, shortHash)
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
// Now uses centralized LogStateTransition + LogAgentReasoning so failures and
// retry decisions are visible in the agent reasoning popout + Logloom graphs.
func (r *Runner) ImplementerHandleLLMFailure(ctx context.Context, taskID string, task ftypes.Task) {
	failureCount := task.Attempts + 1
	maxCap := implementerMaxLLMFailuresCap()

	if failureCount >= maxCap {
		reason := fmt.Sprintf("blocked after %d LLM failures (cap=%d)", failureCount, maxCap)
		flumelogger.LogStateTransition(ctx, taskID, string(task.Status), "blocked", reason)
		flumelogger.LogAgentReasoning(ctx, taskID, "implementer", reason, map[string]any{
			"failures": failureCount,
			"cap":      maxCap,
			"action":   "block",
		})
		r.logger.Error("implementer: task blocked after LLM failures",
			slog.String("task_id", taskID),
			slog.Int("failures", failureCount),
			slog.Int("cap", maxCap))
		update := map[string]interface{}{
			// PR2 LLM fail block guarded
			"status":         "blocked",
			"attempts":       failureCount,
			"error_message":  reason,
			"active_worker":  nil,
			"queue_state":    "available",
			"updated_at":     time.Now().UTC().Format(time.RFC3339),
		}
		_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, update)
	} else {
		reason := fmt.Sprintf("LLM failure #%d < cap=%d; re-queued for retry", failureCount, maxCap)
		flumelogger.LogStateTransition(ctx, taskID, string(task.Status), "ready", reason)
		flumelogger.LogAgentReasoning(ctx, taskID, "implementer", reason, map[string]any{
			"attempt": failureCount,
			"max":     maxCap,
			"action":  "requeue",
		})
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

// handleRoleLLMFailure is a shared retry-cap handler for reviewer/tester roles.
// Mirrors ImplementerHandleLLMFailure: after N failures, escalates to "blocked" instead
// of allowing the infinite retry loop that was the dominant post-migration failure mode.
func (r *Runner) handleRoleLLMFailure(ctx context.Context, taskID string, task ftypes.Task, role string) {
	failureCount := task.Attempts + 1
	maxCap := 3 // reviewer/tester are lightweight; 3 retries is generous

	if failureCount >= maxCap {
		reason := fmt.Sprintf("%s blocked after %d LLM failures (cap=%d)", role, failureCount, maxCap)
		flumelogger.LogStateTransition(ctx, taskID, string(task.Status), "blocked", reason)
		flumelogger.LogAgentReasoning(ctx, taskID, role, reason, map[string]any{
			"failures": failureCount,
			"cap":      maxCap,
			"action":   "block",
		})
		r.logger.Error(role+": task blocked after LLM failures",
			slog.String("task_id", taskID),
			slog.Int("failures", failureCount),
			slog.Int("cap", maxCap))
		update := map[string]interface{}{
			"status":        "blocked",
			"attempts":      failureCount,
			"error_message": reason,
			"active_worker": nil,
			"queue_state":   "available",
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		}
		_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, update)
	} else {
		// Increment attempts so the next claim cycle sees the counter
		_ = r.es.UpdateDoc(ctx, "agent-task-records", taskID, map[string]interface{}{
			"attempts":   failureCount,
			"updated_at": time.Now().UTC().Format(time.RFC3339),
		})
	}
}

// spawnReviewTasks creates a reviewer and tester subtask for review-consensus.
// HARDENED against explosion (2026-05):
// - Idempotent: skips if reviewer or tester children already exist for this parent.
// - Respects project-level WorkPaused (the real "halt the swarm" signal).
// - Logs rich reasoning so the popout explains why no more children were created.
func (r *Runner) spawnReviewTasks(ctx context.Context, parent ftypes.Task) error {
	// 1. Emergency stop / pause guard (the control the user needed when 258 items appeared)
	if r.isWorkPaused(ctx, parent.ProjectID) {
		reason := "project work is paused (emergency stop or manual halt); refusing to spawn reviewer/tester children"
		flumelogger.LogAgentReasoning(ctx, parent.ID, "system", reason, map[string]any{
			"parent_id": parent.ID,
			"repo":      parent.ProjectID,
		})
		r.logger.Warn("spawnReviewTasks blocked by work pause",
			slog.String("parent_id", parent.ID), slog.String("repo", parent.ProjectID))
		return nil // do not error the parent task; just refuse to explode
	}

	// 2. Strong idempotency / anti-explosion guard (prevents reviewer/tester multiplication)
	// We now count existing review/test children for this parent and refuse if we already have the expected pair.
	childQuery := map[string]interface{}{
		"term": map[string]string{"parent_id": parent.ID},
	}
	childRes, cerr := r.es.Search(ctx, "agent-task-records", childQuery, 100)
	existingReviewOrTest := 0
	if cerr == nil {
		for _, rawHit := range childRes.RawHits {
			var c ftypes.Task
			if json.Unmarshal(rawHit.Source, &c) == nil {
				if c.WorkerRole == "reviewer" || c.WorkerRole == "tester" ||
					strings.HasPrefix(c.Title, "Review:") || strings.HasPrefix(c.Title, "Test:") ||
					strings.Contains(c.Title, "Review:") || strings.Contains(c.Title, "Test:") {
					existingReviewOrTest++
					if existingReviewOrTest >= 2 {
						r.logger.Info("spawnReviewTasks skipped — sufficient reviewer/tester children already exist (strong anti-explosion guard)",
							slog.String("parent_id", parent.ID),
							slog.Int("existing_review_test_count", existingReviewOrTest))
						flumelogger.LogAgentReasoning(ctx, parent.ID, "system",
							"Refused to spawn additional reviewer/tester children — quota already met for this parent.",
							map[string]any{"parent_id": parent.ID, "existing_count": existingReviewOrTest})
						return errSpawnGuardFired
					}
				}
			}
		}
	}

	// Hardened guard (post live test 17-task dup explosion): Re-check right before creation to close race window.
	// Under LLM failure + reset loops, multiple implementer completions can race. Stricter: refuse if *any* exist.
	if existingReviewOrTest > 0 {
		r.logger.Info("spawnReviewTasks skipped — review/test children already exist for parent (race protection + recheck)",
			slog.String("parent_id", parent.ID),
			slog.Int("existing", existingReviewOrTest))
		flumelogger.LogAgentReasoning(ctx, parent.ID, "system",
			"Refused additional review/test spawn — children already present (concurrent reset/spawn race closed).",
			map[string]any{"parent_id": parent.ID, "existing": existingReviewOrTest})
		return errSpawnGuardFired
	}

	// Final atomic-ish recheck using fresh search before any Index to minimize dup window under high churn.
	recheckRes, recheckErr := r.es.Search(ctx, "agent-task-records", childQuery, 10)
	if recheckErr == nil {
		recheckCount := 0
		for _, h := range recheckRes.RawHits {
			var c ftypes.Task
			if json.Unmarshal(h.Source, &c) == nil {
				if c.WorkerRole == "reviewer" || c.WorkerRole == "tester" || strings.HasPrefix(c.Title, "Review:") || strings.HasPrefix(c.Title, "Test:") {
					recheckCount++
				}
			}
		}
		if recheckCount > 0 {
			r.logger.Info("spawnReviewTasks skipped on final recheck — race detected and prevented",
				slog.String("parent_id", parent.ID), slog.Int("recheck_count", recheckCount))
			flumelogger.LogAgentReasoning(ctx, parent.ID, "system", "Final recheck prevented duplicate review/test spawn under failure loop.", map[string]any{"parent_id": parent.ID, "recheck": recheckCount})
			return errSpawnGuardFired
		}
	}

	// Extra safety: never spawn reviewer/tester for a task whose own title already indicates it is a review or test item
	lowerTitle := strings.ToLower(parent.Title)
	if strings.Contains(lowerTitle, "review") || strings.Contains(lowerTitle, "test") {
		r.logger.Info("spawnReviewTasks skipped — parent task title indicates it is already a review/test item",
			slog.String("parent_id", parent.ID), slog.String("title", parent.Title))
		return nil
	}

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
		// Phase 2: non-commit creation path wiring
		PlanSessionID:  parent.PlanSessionID,
		HierarchyDepth: parent.HierarchyDepth + 1,
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
		// Phase 2: non-commit creation path wiring
		PlanSessionID:  parent.PlanSessionID,
		HierarchyDepth: parent.HierarchyDepth + 1,
	}

	if err := r.es.IndexDoc(ctx, "agent-task-records", revTask.ID, revTask); err != nil {
		return fmt.Errorf("index reviewer task: %w", err)
	}
	// Give the reviewer child its own initial reasoning/evidence at birth (fixes "0 thoughts on spawned review/test" from live rflow test).
	// This ensures every review-consensus child starts with audit trail for Evidence gates + UI.
	flumelogger.LogAgentReasoning(ctx, revTask.ID, "reviewer",
		"Reviewer task spawned for parent review-consensus. Awaiting analysis of implementer output against acceptance criteria.",
		map[string]any{"parent_id": parent.ID, "role": "reviewer", "spawn_reason": "review-consensus"})

	if err := r.es.IndexDoc(ctx, "agent-task-records", testTask.ID, testTask); err != nil {
		return fmt.Errorf("index tester task: %w", err)
	}
	// Symmetric for tester child (ensures full metadata + thoughts on all children from creation).
	flumelogger.LogAgentReasoning(ctx, testTask.ID, "tester",
		"Tester task spawned for parent review-consensus. Will execute tests and evaluate outcomes after reviewer pass.",
		map[string]any{"parent_id": parent.ID, "role": "tester", "spawn_reason": "review-consensus"})

	r.logger.Info("spawned reviewer and tester subtasks",
		slog.String("parent_id", parent.ID),
		slog.String("rev_task_id", revTask.ID),
		slog.String("test_task_id", testTask.ID))

	flumelogger.LogAgentReasoning(ctx, parent.ID, "system",
		"Spawned one reviewer + one tester child for review-consensus (guarded).",
		map[string]any{"parent_id": parent.ID, "rev": revTask.ID, "test": testTask.ID})

	// Authoritative child_count via script (task 3) — +2 for reviewer+tester.
	nowISO := time.Now().UTC().Format(time.RFC3339)
	_ = r.es.UpdateDocWithInlineScript(ctx, "agent-task-records", parent.ID, `
		ctx._source.child_count = (ctx._source.child_count != null ? ctx._source.child_count : 0) + params.delta;
		ctx._source.decomposed_at = params.now;
		ctx._source.updated_at = params.now;
	`, map[string]interface{}{"delta": 2, "now": nowISO})

	return nil
}

// isWorkPaused returns true if the given project (or a global emergency pause) has WorkPaused set.
// This is the central check for the "halt the swarm" feature that actually stops new item creation.
func (r *Runner) isWorkPaused(ctx context.Context, projectID string) bool {
	if projectID == "" {
		return false
	}
	projDoc, err := r.es.GetDoc(ctx, "flume-projects", projectID)
	if err != nil || projDoc == nil {
		return false
	}
	var proj ftypes.Project
	if json.Unmarshal(projDoc, &proj) != nil {
		return false
	}
	return proj.WorkPaused
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

	// Strip <think>...</think> blocks (thinking models like qwen3.5 emit these).
	// Must happen BEFORE any JSON extraction so the JSON finder sees clean text.
	thinkRe := regexp.MustCompile(`(?s)<think>.*?</think>`)
	s = thinkRe.ReplaceAllString(s, "")
	s = strings.TrimSpace(s)

	// Strip markdown code fences
	if strings.HasPrefix(s, "```json") {
		s = strings.TrimPrefix(s, "```json")
		s = strings.TrimSuffix(s, "```")
		s = strings.TrimSpace(s)
	} else if strings.HasPrefix(s, "```") {
		s = strings.TrimPrefix(s, "```")
		s = strings.TrimSuffix(s, "```")
		s = strings.TrimSpace(s)
	}

	// If the result doesn't start with '{', try to find the first JSON object.
	// LLMs frequently emit leading prose like "Here is the plan:" before the JSON.
	if !strings.HasPrefix(s, "{") {
		if idx := strings.Index(s, "{"); idx >= 0 {
			s = s[idx:]
			// Find the matching closing brace
			depth := 0
			for i, ch := range s {
				if ch == '{' {
					depth++
				} else if ch == '}' {
					depth--
					if depth == 0 {
						s = s[:i+1]
						break
					}
				}
			}
		}
	}

	return s
}

func generateShortID() string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x", b)
}

// firstNChars is a tiny safe helper for logging/repair prompts (avoids dumping megabytes of LLM output).
func firstNChars(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
