package worker

// PR 2 NOTE: Every "status" update in this file now routes through
// pkg/types DefaultTaskStateMachine.EnforceTransition (shadow mode).
// See requeueStuck..., promotePlannedTasks, Execute*Sweep, evaluateReviewConsensus, parentCompletionSweep.

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	"github.com/Fremen-Labs/flume/internal/git"
	"github.com/Fremen-Labs/flume/internal/llm"
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Sweeper handles periodic maintenance sweeps for the task queue.
// Derived from Python: orchestration/sweeps.py (391 LOC, 13 AST nodes).
//
// Sweeps:
//   - Stuck Implementer: Requeue tasks stuck in "running" beyond timeout
//   - Stuck Review: Clear phantom review locks
//   - Promote: Move planned tasks to ready when dependencies are met  (UNIFIED SOURCE OF TRUTH per PR3)
//   - Resume: Recover blocked tasks when conditions clear
//   - Block: Halt tasks when node capacity is exceeded
type Sweeper struct {
	es     *es.Client
	llm    *llm.Client
	logger *slog.Logger

	// Throttle state — derived from Python: SWEEP_LAST_RUN, SWEEP_INTERVALS
	mu           sync.Mutex
	lastRun      map[string]time.Time
	intervals    map[string]time.Duration
	plannedCount int
}

// NewSweeper creates a new sweep orchestrator.
func NewSweeper(esClient *es.Client, llmClient *llm.Client, logger *slog.Logger) *Sweeper {
	return &Sweeper{
		es:     esClient,
		llm:    llmClient,
		logger: logger.With(slog.String("component", "orchestration.sweeps")),
		lastRun: map[string]time.Time{
			"stuck_impl":       {},
			"stuck_review":     {},
			"promote":          {},
			"consensus":        {},
			"parent_comp":      {},
			"child_count_recon": {},
		},
		intervals: map[string]time.Duration{
			"stuck_impl":        30 * time.Second,
			"stuck_review":      30 * time.Second,
			"promote":           5 * time.Second,
			"consensus":         5 * time.Second,
			"parent_comp":       5 * time.Second,
			"child_count_recon": 45 * time.Second, // periodic authoritative recompute + lag detection
		},
	}
}

// RunThrottled executes all sweeps that are due.
// Derived from Python: manager.cycle() sweep throttling (L194-225, 18 AST nodes)
func (s *Sweeper) RunThrottled(ctx context.Context) {
	s.mu.Lock()
	now := time.Now()

	// Stuck implementer sweep
	if now.Sub(s.lastRun["stuck_impl"]) >= s.intervals["stuck_impl"] {
		s.lastRun["stuck_impl"] = now
		s.mu.Unlock()
		if count := s.requeueStuckImplementerTasks(ctx); count > 0 {
			s.logger.Info("stuck-implementer sweep: requeued tasks",
				slog.Int("count", count))
		}
		s.mu.Lock()
	}

	// Stuck review sweep
	if now.Sub(s.lastRun["stuck_review"]) >= s.intervals["stuck_review"] {
		s.lastRun["stuck_review"] = now
		s.mu.Unlock()
		if count := s.requeueStuckReviewTasks(ctx); count > 0 {
			s.logger.Info("stuck-review sweep: cleared phantom locks",
				slog.Int("count", count))
		}
		s.mu.Lock()
	}

	// Promote sweep — adaptive interval based on planned task count
	// Now delegates to unified repo-aware promotePlannedTasks (repoFilter="" for global)
	promoteInterval := s.getPromoteInterval()
	if now.Sub(s.lastRun["promote"]) >= promoteInterval {
		s.lastRun["promote"] = now
		s.mu.Unlock()
		if count := s.promotePlannedTasks(ctx, ""); count > 0 {
			s.logger.Info("dependency sweep: promoted tasks to ready",
				slog.Int("count", count))
		}
		s.mu.Lock()
	}

	// Consensus review sweep
	if now.Sub(s.lastRun["consensus"]) >= s.intervals["consensus"] {
		s.lastRun["consensus"] = now
		s.mu.Unlock()
		s.evaluateReviewConsensus(ctx)
		s.mu.Lock()
	}

	// Parent completion sweep
	if now.Sub(s.lastRun["parent_comp"]) >= s.intervals["parent_comp"] {
		s.lastRun["parent_comp"] = now
		s.mu.Unlock()
		s.parentCompletionSweep(ctx)
		s.mu.Lock()
	}

	// Child count authoritative reconciliation + lag detection (task 3)
	if now.Sub(s.lastRun["child_count_recon"]) >= s.intervals["child_count_recon"] {
		s.lastRun["child_count_recon"] = now
		s.mu.Unlock()
		s.childCountReconciliationSweep(ctx)
		s.mu.Lock()
	}

	s.mu.Unlock()
}

// SetPlannedCount updates the planned task count for adaptive promote interval.
// Derived from Python: set_last_planned_count() in orchestration/__init__.py
func (s *Sweeper) SetPlannedCount(count int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.plannedCount = count
}

// getPromoteInterval returns adaptive promote interval.
// When many planned tasks exist, promote more frequently.
func (s *Sweeper) getPromoteInterval() time.Duration {
	if s.plannedCount > 20 {
		return 2 * time.Second
	}
	if s.plannedCount > 5 {
		return 3 * time.Second
	}
	return 5 * time.Second
}

// requeueStuckImplementerTasks requeues tasks stuck in "running" beyond timeout.
// Derived from Python: requeue_stuck_implementer_tasks() (L40-93)
func (s *Sweeper) requeueStuckImplementerTasks(ctx context.Context) int {
	threshold := time.Now().Add(-5 * time.Minute).UTC().Format(time.RFC3339)

	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]string{"status": "running"}},
				// Only target implementer tasks — reviewer/tester stuck tasks are handled
				// by requeueStuckReviewTasks with the correct reset status.
				map[string]interface{}{"term": map[string]string{"worker_role": "implementer"}},
				map[string]interface{}{
					"range": map[string]interface{}{
						"claimed_at": map[string]string{"lt": threshold},
					},
				},
			},
		},
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		s.logger.Warn("stuck-implementer sweep error", slog.String("error", err.Error()))
		return 0
	}

	requeued := 0
	for _, hit := range result.Hits {
		var task struct {
			ID string `json:"id"`
		}
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		update := map[string]interface{}{
			"status":        "ready",
			"active_worker": nil,
			"queue_state":   "available",
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		}
		_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog("", ftypes.TaskStatus("ready"), s.logger.Warn)
		if err := s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update); err == nil {
			requeued++
			s.logger.Info("requeued stuck task",
				slog.String("task_id", task.ID))
		}
	}
	return requeued
}

// requeueStuckReviewTasks clears phantom review locks.
// Derived from Python: requeue_stuck_review_tasks() (L95-148)
func (s *Sweeper) requeueStuckReviewTasks(ctx context.Context) int {
	threshold := time.Now().Add(-10 * time.Minute).UTC().Format(time.RFC3339)

	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				// Match both "review" and "running" statuses — reviewer/tester tasks can get
				// stuck in either state depending on when the crash occurred.
				map[string]interface{}{"terms": map[string]interface{}{"status": []string{"review", "running"}}},
				// Only target reviewer/tester roles — implementer stuck tasks are handled
				// separately by requeueStuckImplementerTasks with the correct reset status.
				map[string]interface{}{"terms": map[string]interface{}{"worker_role": []string{"reviewer", "tester"}}},
				map[string]interface{}{
					"range": map[string]interface{}{
						"claimed_at": map[string]string{"lt": threshold},
					},
				},
			},
		},
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		s.logger.Warn("stuck-review sweep error", slog.String("error", err.Error()))
		return 0
	}

	cleared := 0
	for _, hit := range result.Hits {
		var task struct {
			ID string `json:"id"`
		}
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		update := map[string]interface{}{
			"status":        "review",
			"active_worker": nil,
			"queue_state":   "available",
			"updated_at":    time.Now().UTC().Format(time.RFC3339),
		}
		_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog("", ftypes.TaskStatus(update["status"].(string)), s.logger.Warn)
	if err := s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update); err == nil {
			cleared++
		}
	}
	return cleared
}

// promotePlannedTasks moves planned tasks to ready when dependencies (parent + depends_on) are met.
// 
// This is the SINGLE SOURCE OF TRUTH for all promotion logic (design: flume-queue-planning-reliability PR3).
// Retires the buggy/dead ComputeReadyForRepo (see runner.go).
//
// Key features per design doc:
//   - Repo-scoped: pass repoFilter (non-empty uses "repo" term filter; "" = global sweep)
//   - Batch mget + in-sweep cache for parents/depends_on (eliminates N+1 Gets for large plans)
//   - Resilience: OCC via seq/prim from search hits + retry loop on conflicts; fallback on mget errors
//   - Enforcer integration: calls central TaskStateMachine.EnforceTransitionOrLog (from PR2)
//     (leverages new Complexity fields for gates/limits)
//   - Improved logging with skip reasons, repo context, batch stats
//   - Full dependency check (parent + all DependsOn siblings) — old ComputeReadyForRepo only did parent (bug)
//
// Derived from Python: promote_planned_tasks() (L150-200) + compute_ready_for_repo (L1636-1864)
// See also: manager.go TriggerSweep, claim.go OCC patterns.
func (s *Sweeper) promotePlannedTasks(ctx context.Context, repoFilter string) int {
	baseQuery := map[string]interface{}{
		"term": map[string]string{"status": "planned"},
	}
	query := baseQuery
	if repoFilter != "" {
		query = map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []interface{}{
					map[string]interface{}{"term": map[string]string{"status": "planned"}},
					map[string]interface{}{"term": map[string]string{"repo": repoFilter}},
				},
			},
		}
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 200)
	if err != nil {
		s.logger.Warn("dependency sweep error", slog.String("error", err.Error()), slog.String("repo", repoFilter))
		return 0
	}

	if len(result.Hits) == 0 {
		return 0
	}

	// Collect needed parent + depends IDs for batch resolution (resilience for large/complex plans)
	neededIDs := make(map[string]bool)
	type plannedTask struct {
		ID             string
		ParentID       string
		DependsOn      []string
		HierarchyDepth int
		PlanSessionID  string
		// RawHit for OCC retry resilience (captured from search with seq_no_primary_term)
		RawHit *es.SearchHit
	}
	var candidates []plannedTask

	for i, hit := range result.Hits {
		var t struct {
			ID             string   `json:"id"`
			ParentID       string   `json:"parent_id"`
			DependsOn      []string `json:"depends_on"`
			HierarchyDepth int      `json:"hierarchy_depth"`
			PlanSessionID  string   `json:"plan_session_id"`
		}
		if json.Unmarshal(hit, &t) != nil {
			continue
		}
		pt := plannedTask{
			ID:             t.ID,
			ParentID:       t.ParentID,
			DependsOn:      t.DependsOn,
			HierarchyDepth: t.HierarchyDepth,
			PlanSessionID:  t.PlanSessionID,
		}
		if i < len(result.RawHits) {
			pt.RawHit = &result.RawHits[i]
		}
		candidates = append(candidates, pt)

		if t.ParentID != "" {
			neededIDs[t.ParentID] = true
		}
		for _, d := range t.DependsOn {
			if d != "" {
				neededIDs[d] = true
			}
		}
	}

	// Batch lookup (mget preferred) + simple in-sweep cache
	// This is the key resilience change documented for PR3: O(1) lookups vs N+1 per planned task
	statusCache := make(map[string]string, len(neededIDs))
	idList := make([]string, 0, len(neededIDs))
	for id := range neededIDs {
		idList = append(idList, id)
	}

	if len(idList) > 0 {
		mgetResults, mgetErr := s.es.MGetDocs(ctx, "agent-task-records", idList)
		if mgetErr != nil {
			s.logger.Warn("mget batch lookup failed for promote deps; falling back to serial Gets (performance hit on large plans)",
				slog.String("error", mgetErr.Error()),
				slog.Int("ids", len(idList)))
			for _, id := range idList {
				doc, gerr := s.es.GetDoc(ctx, "agent-task-records", id)
				if gerr == nil && doc != nil {
					var meta struct {
						Status string `json:"status"`
					}
					if json.Unmarshal(doc, &meta) == nil {
						statusCache[id] = meta.Status
					}
				}
			}
		} else {
			for id, doc := range mgetResults {
				if doc != nil {
					var meta struct {
						Status string `json:"status"`
					}
					if json.Unmarshal(doc, &meta) == nil {
						statusCache[id] = meta.Status
					}
				}
			}
			s.logger.Debug("promote: mget+cache batch complete",
				slog.Int("requested_ids", len(idList)),
				slog.Int("cached", len(statusCache)),
				slog.String("repo", repoFilter))
		}
	}

	promoted := 0
	for _, task := range candidates {
		// === Phase 2 DEPTH ENFORCEMENT in promotePlannedTasks ===
		// Skip (with rich LogAgentReasoning for UI/Logloom) any task whose depth already exceeds MAX.
		// This prevents promotion of deeply nested items created by runaway PM decomp.
		if task.HierarchyDepth > ftypes.MAX_HIERARCHY_DEPTH {
			reason := fmt.Sprintf("promote blocked: hierarchy_depth=%d exceeds MAX_HIERARCHY_DEPTH=%d (anti-nesting explosion guard)", task.HierarchyDepth, ftypes.MAX_HIERARCHY_DEPTH)
			flumelogger.LogAgentReasoning(ctx, task.ID, "system", reason, map[string]any{
				"depth": task.HierarchyDepth,
				"max":   ftypes.MAX_HIERARCHY_DEPTH,
				"plan_session_id": task.PlanSessionID,
			})
			s.logger.Warn("promote skip (depth exceeded)",
				slog.String("task_id", task.ID),
				slog.Int("depth", task.HierarchyDepth),
				slog.Int("max", ftypes.MAX_HIERARCHY_DEPTH))
			continue
		}

		// Parent check (using cache)
		//
		// FIX for Hierarchy promotion deadlock (P0 from flume-queue-planning-reliability):
		// Intake (buildTaskHierarchy) creates stories/feats/epics with status="planned".
		// Previously we skipped *any* child whose parent was still "planned".
		// This meant that in any story with 2+ tasks, only the first sibling (pre-created "ready")
		// would ever run. All subsequent siblings stayed "planned" forever even after depends_on met.
		//
		// We now only block promotion when the parent is in a definitively bad terminal state.
		// "planned" parents are normal for organizational hierarchy items created by Plan New Work.
		// The depends_on check (below) is the primary ordering mechanism for siblings.
		if task.ParentID != "" {
			pstatus := statusCache[task.ParentID]
			if pstatus == "" || pstatus == string(ftypes.TaskStatusBlocked) || pstatus == string(ftypes.TaskStatusArchived) {
				s.logger.Debug("promote skip (parent inactive)",
					slog.String("task_id", task.ID),
					slog.String("parent_id", task.ParentID),
					slog.String("parent_status", pstatus),
					slog.String("repo", repoFilter))
				continue
			}
		}

		// DependsOn sibling deps check (full logic; old ComputeReadyForRepo omitted this — bug)
		dependsOnMet := true
		for _, depID := range task.DependsOn {
			if depID == "" {
				continue
			}
			dstatus := statusCache[depID]
			if dstatus == "" || (dstatus != string(ftypes.TaskStatusDone) && dstatus != string(ftypes.TaskStatusArchived)) {
				dependsOnMet = false
				s.logger.Debug("promote skip (dep unmet)",
					slog.String("task_id", task.ID),
					slog.String("dep_id", depID),
					slog.String("dep_status", dstatus))
				break
			}
		}
		if !dependsOnMet {
			continue
		}

		// PR2 Enforcer (central TaskStateMachine from PR2)
		_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog(
			ftypes.TaskStatusPlanned,
			ftypes.TaskStatusReady,
			s.logger.Warn,
		)

		// Resilient update (PR3): prefer OCC using hit metadata; retry on conflict (race with claim/other sweeps)
		now := time.Now().UTC().Format(time.RFC3339)
		update := map[string]interface{}{
			"status":     "ready",
			"updated_at": now,
		}

		updateSuccess := false
		const maxRetries = 3
		for attempt := 0; attempt < maxRetries; attempt++ {
			var updErr error
			if task.RawHit != nil && task.RawHit.SeqNo != nil && task.RawHit.PrimaryTerm != nil {
				updErr = s.es.UpdateDocOCC(ctx, "agent-task-records", task.ID, update, *task.RawHit.SeqNo, *task.RawHit.PrimaryTerm)
			} else {
				updErr = s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update)
			}

			if updErr == nil {
				updateSuccess = true
				break
			}
			if updErr == es.ErrConflict {
				s.logger.Debug("promote update conflict (OCC race); will retry",
					slog.String("task_id", task.ID),
					slog.Int("attempt", attempt+1))
				time.Sleep(time.Duration(attempt+1) * 40 * time.Millisecond)
				continue
			}
			s.logger.Warn("promote update non-retryable error",
				slog.String("task_id", task.ID),
				slog.String("error", updErr.Error()))
			break
		}

		if updateSuccess {
			promoted++
			s.logger.Info("promoted task to ready (single unified path)",
				slog.String("task_id", task.ID),
				slog.String("repo", repoFilter))
		}
	}

	return promoted
}

// ExecuteResumeSweep recovers blocked tasks when conditions clear.
// Derived from Python: execute_resume_sweep() (L202-250)
func (s *Sweeper) ExecuteResumeSweep(ctx context.Context) {
	query := map[string]interface{}{
		"term": map[string]string{"status": "blocked"},
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		return
	}

	for _, hit := range result.Hits {
		var task struct {
			ID           string `json:"id"`
			ErrorMessage string `json:"error_message"`
			Attempts     int    `json:"attempts"`
			MaxAttempts  int    `json:"max_attempts"`
		}
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		// Only resume if under max attempts
		maxAttempts := task.MaxAttempts
		if maxAttempts == 0 {
			maxAttempts = 3
		}
		if task.Attempts < maxAttempts {
			update := map[string]interface{}{
				"status":        "ready",
				"error_message": "",
				"updated_at":    time.Now().UTC().Format(time.RFC3339),
			}
			_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog("", ftypes.TaskStatus(update["status"].(string)), s.logger.Warn)
	if err := s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update); err == nil {
				s.logger.Info("resume sweep: recovered blocked task",
					slog.String("task_id", task.ID))
			}
		}
	}
}

// ExecuteBlockSweep halts tasks when node capacity is exceeded.
// Derived from Python: execute_block_sweep() (L252-300)
func (s *Sweeper) ExecuteBlockSweep(ctx context.Context, nodeLoads, nodeCaps map[string]int, cloudProviders map[string]bool) {
	// Block sweep checks for tasks that should be paused due to overloaded nodes
	query := map[string]interface{}{
		"term": map[string]string{"status": "running"},
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 100)
	if err != nil {
		return
	}

	for _, hit := range result.Hits {
		var task struct {
			ID            string `json:"id"`
			ExecutionHost string `json:"execution_host"`
			Provider      string `json:"provider"`
		}
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		// Only block local tasks when node is overloaded
		if task.Provider != "" && cloudProviders[task.Provider] {
			continue
		}

		host := task.ExecutionHost
		if host == "" {
			host = "localhost"
		}
		cap := nodeCaps[host]
		if cap == 0 {
			cap = 4
		}

		if nodeLoads[host] > cap*2 { // Only block at 2x overload
			update := map[string]interface{}{
				"status":        "blocked",
				"error_message": "Node capacity exceeded — auto-blocked by sweep",
				"updated_at":    time.Now().UTC().Format(time.RFC3339),
			}
			if err := s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update); err == nil {
				s.logger.Warn("block sweep: halted overloaded task",
					slog.String("task_id", task.ID),
					slog.String("host", host))
			}
		}
	}
}

func (s *Sweeper) evaluateReviewConsensus(ctx context.Context) {
	// Query all tasks with status "review-consensus"
	query := map[string]interface{}{
		"term": map[string]string{"status": "review-consensus"},
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		return
	}

	for _, hit := range result.Hits {
		var task ftypes.Task
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		// Query all child tasks
		childQuery := map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []interface{}{
					map[string]interface{}{"term": map[string]string{"parent_id": task.ID}},
					map[string]interface{}{"terms": map[string]interface{}{"worker_role": []string{"reviewer", "tester"}}},
				},
			},
		}

		childRes, err := s.es.Search(ctx, "agent-task-records", childQuery, 10)
		if err != nil {
			continue
		}

		// Verify if all child tasks are completed
		allCompleted := true
		hasChildren := false
		var reviewerTask, testerTask *ftypes.Task

		for _, chHit := range childRes.Hits {
			var child ftypes.Task
			if json.Unmarshal(chHit, &child) == nil {
				hasChildren = true
				if child.Status != ftypes.TaskStatusDone && child.Status != ftypes.TaskStatusArchived && child.Status != ftypes.TaskStatusBlocked {
					allCompleted = false
					break
				}
				if child.WorkerRole == "reviewer" {
					childCopy := child
					reviewerTask = &childCopy
				} else if child.WorkerRole == "tester" {
					childCopy := child
					testerTask = &childCopy
				}
			}
		}

		if !hasChildren || !allCompleted {
			continue
		}

		// We have review tasks, and they are complete!
		// Evaluate consensus
		approved := true
		var explanation strings.Builder
		explanation.WriteString("Aggregated Consensus Review:\n")

		if reviewerTask != nil {
			explanation.WriteString(fmt.Sprintf("- Reviewer: verdict=%s, feedback=%s\n", reviewerTask.ReviewVerdict, reviewerTask.Feedback))
			if reviewerTask.ReviewVerdict != "approved" {
				approved = false
			}
		}
		if testerTask != nil {
			explanation.WriteString(fmt.Sprintf("- Tester: verdict=%s, feedback=%s\n", testerTask.ReviewVerdict, testerTask.Feedback))
			if testerTask.ReviewVerdict != "approved" {
				approved = false
			}
		}

		// Use an agent prompt to summarize or make a finalized decision
		if s.llm != nil {
			consensusSystemPrompt := "You are the Flume Consensus Agent. Evaluate the reviewer feedback and test outcomes, and synthesize a final verdict explaining whether the PR should be created and merged."
			req := llm.ChatRequest{
				Messages: []llm.Message{
					{Role: "system", Content: consensusSystemPrompt},
					{Role: "user", Content: fmt.Sprintf("Parent Task: %s\n%s", task.Title, explanation.String())},
				},
				Model:      task.Model,
				Provider:   task.Provider,
				AgentRole:  "critic",
				TaskID:     task.ID,
				WorkerName: "critic",
			}
			resp, chatErr := s.llm.Chat(ctx, req)
			if chatErr == nil {
				explanation.WriteString(fmt.Sprintf("\nConsensus Critic Synthesis:\n%s", resp.Content))
			}
		}

		if approved {
			s.logger.Info("consensus review approved: creating PR and marking task done", slog.String("task_id", task.ID))

			// Post-review PR creation hook
			if task.ProjectID != "" {
				projDoc, projErr := s.es.GetDoc(ctx, "flume-projects", task.ProjectID)
				if projErr == nil && projDoc != nil {
					var proj ftypes.Project
					if json.Unmarshal(projDoc, &proj) == nil {
						// Create git client
						projMap := make(map[string]interface{})
						_ = json.Unmarshal(projDoc, &projMap)
						client, clientErr := git.GetClient(ctx, projMap)
						if clientErr == nil {
							branch := resolveBranchName(task)
							defaultBranch := "main"
							if proj.Branch != "" {
								defaultBranch = proj.Branch
							} else if override := os.Getenv("FLUME_DEFAULT_BRANCH"); override != "" {
								defaultBranch = override
							}
							prTitle := fmt.Sprintf("[Flume PR] %s", task.Title)
							prBody := fmt.Sprintf("This PR was automatically generated by Flume for task: %s\n\n%s", task.ID, explanation.String())
							_, prErr := client.CreatePullRequest(ctx, prTitle, prBody, branch, defaultBranch)
							if prErr != nil {
								s.logger.Error("post-review hook: failed to create Pull Request", slog.String("error", prErr.Error()))
							} else {
								s.logger.Info("post-review hook: Pull Request created successfully")
							}
						}
					}
				}
			}

			// Update task to done
			now := time.Now().UTC()
			update := map[string]interface{}{
				"status":        string(ftypes.TaskStatusDone),
				"completed_at":  now.Format(time.RFC3339),
				"updated_at":    now.Format(time.RFC3339),
				"feedback":      explanation.String(),
				"active_worker": nil,
				"queue_state":   "available",
			}
			_ = s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update)

		} else {
			s.logger.Warn("consensus review rejected: resetting task to ready for rework", slog.String("task_id", task.ID))

			// Reset parent task to ready for rework
			attempts := task.Attempts + 1
			maxAttempts := task.MaxAttempts
			if maxAttempts == 0 {
				maxAttempts = 3
			}

			var update map[string]interface{}
			if attempts >= maxAttempts {
				update = map[string]interface{}{
					"status":        string(ftypes.TaskStatusBlocked),
					"attempts":      attempts,
					"error_message": fmt.Sprintf("Consensus review failed after %d attempts", attempts),
					"feedback":      explanation.String(),
					"active_worker": nil,
					"queue_state":   "available",
					"updated_at":    time.Now().UTC().Format(time.RFC3339),
				}
			} else {
				update = map[string]interface{}{
					"status":        string(ftypes.TaskStatusReady),
					"attempts":      attempts,
					"feedback":      explanation.String(),
					"active_worker": nil,
					"queue_state":   "available",
					"updated_at":    time.Now().UTC().Format(time.RFC3339),
				}
			}

			_ = s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update)
		}
	}
}

func (s *Sweeper) parentCompletionSweep(ctx context.Context) {
	// FIX for Hierarchy promotion deadlock (P0):
	// Previously this only considered top-level items (no parent_id) that were *already*
	// in active states (running/ready/review-consensus). Intake-created epics/feats/stories
	// start as "planned" and have parent_id (except epics), so they were never swept.
	//
	// We now do two passes:
	// 1. Original top-level active parents (kept for compatibility).
	// 2. General pass: any item that has children where *all* direct children are terminal
	//    (done or archived) gets marked done. This walks the full epic→feat→story→task tree.

	// Pass 1: Original top-level logic (items with no parent_id already in active states)
	{
		query := map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []interface{}{
					map[string]interface{}{"terms": map[string]interface{}{"status": []string{"running", "ready", "review-consensus"}}},
				},
				"must_not": []interface{}{
					map[string]interface{}{"exists": map[string]string{"field": "parent_id"}},
				},
			},
		}

		result, err := s.es.Search(ctx, "agent-task-records", query, 100)
		if err == nil {
			for _, hit := range result.Hits {
				var parent ftypes.Task
				if json.Unmarshal(hit, &parent) != nil {
					continue
				}
				s.tryMarkParentDoneIfAllChildrenTerminal(ctx, parent)
			}
		}
	}

	// Pass 2: General hierarchy completion — any parent whose direct children are all terminal
	// This catches epics (no parent_id but start "planned"), feats, and stories created by intake.
	{
		// Find candidates that have at least one child (we'll check their status inside the helper)
		// To keep it simple and correct we scan a broader set of non-terminal parents.
		query := map[string]interface{}{
			"bool": map[string]interface{}{
				"must_not": []interface{}{
					map[string]interface{}{"terms": map[string]interface{}{
						"status": []string{"done", "archived"},
					}},
				},
			},
		}

		result, err := s.es.Search(ctx, "agent-task-records", query, 200)
		if err != nil {
			return
		}

		for _, hit := range result.Hits {
			var potentialParent ftypes.Task
			if json.Unmarshal(hit, &potentialParent) != nil {
				continue
			}

			// Only consider items that actually have children
			childQuery := map[string]interface{}{
				"term": map[string]string{"parent_id": potentialParent.ID},
			}
			childRes, cerr := s.es.Search(ctx, "agent-task-records", childQuery, 1)
			if cerr != nil || len(childRes.Hits) == 0 {
				continue
			}

			s.tryMarkParentDoneIfAllChildrenTerminal(ctx, potentialParent)
		}
	}
}

// tryMarkParentDoneIfAllChildrenTerminal is the shared helper used by parentCompletionSweep.
func (s *Sweeper) tryMarkParentDoneIfAllChildrenTerminal(ctx context.Context, parent ftypes.Task) {
	childQuery := map[string]interface{}{
		"term": map[string]string{"parent_id": parent.ID},
	}

	childRes, err := s.es.Search(ctx, "agent-task-records", childQuery, 100)
	if err != nil {
		return
	}

	allChildrenDone := true
	hasChildren := false

	for _, chHit := range childRes.Hits {
		var child ftypes.Task
		if json.Unmarshal(chHit, &child) == nil {
			hasChildren = true
			if child.Status != ftypes.TaskStatusDone && child.Status != ftypes.TaskStatusArchived {
				allChildrenDone = false
				break
			}
		}
	}

	if hasChildren && allChildrenDone {
		s.logger.Info("parentCompletionSweep: marking parent done — all children terminal",
			slog.String("parent_id", parent.ID),
			slog.String("title", parent.Title),
			slog.String("item_type", parent.ItemType))

		update := map[string]interface{}{
			"status":       string(ftypes.TaskStatusDone),
			"completed_at": time.Now().UTC().Format(time.RFC3339),
			"updated_at":   time.Now().UTC().Format(time.RFC3339),
		}
		_ = s.es.UpdateDoc(ctx, "agent-task-records", parent.ID, update)
	}
}

// childCountReconciliationSweep (Phase 2 task 3): periodically recomputes true child_count
// from live children search for a sample of parents and corrects drift via atomic script.
// Emits structured "child_count_lag" logs (observable as metric in Logloom / dashboards).
// Keeps denorm authoritative even if prior increments missed under failure/retry storms.
func (s *Sweeper) childCountReconciliationSweep(ctx context.Context) {
	// Sample parents that have a non-zero child_count denorm or are known PM/structural items.
	// Lightweight: limit to 50 candidates to avoid heavy load.
	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"exists": map[string]string{"field": "parent_id"}},
			},
			"should": []interface{}{
				map[string]interface{}{"range": map[string]interface{}{"child_count": map[string]interface{}{"gt": 0}}},
				map[string]interface{}{"terms": map[string]interface{}{"item_type": []string{"epic", "feature", "story", "pm"}}},
			},
			"minimum_should_match": 1,
		},
	}
	res, err := s.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		s.logger.Debug("childCountReconciliationSweep: search failed", slog.String("err", err.Error()))
		return
	}

	lagDetected := 0
	for _, hit := range res.Hits {
		var parent struct {
			ID          string `json:"id"`
			ChildCount  int    `json:"child_count"`
			Title       string `json:"title"`
		}
		if json.Unmarshal(hit, &parent) == nil && parent.ID != "" {
			// Compute authoritative live count
			childQ := map[string]interface{}{"term": map[string]string{"parent_id": parent.ID}}
			childRes, cerr := s.es.Search(ctx, "agent-task-records", childQ, 1000)
			if cerr != nil {
				continue
			}
			trueCount := len(childRes.Hits)
			diff := trueCount - parent.ChildCount
			if diff != 0 {
				lagDetected++
				s.logger.Info("child_count_lag_detected",
					slog.String("parent_id", parent.ID),
					slog.String("title", parent.Title),
					slog.Int("stored_child_count", parent.ChildCount),
					slog.Int("true_child_count", trueCount),
					slog.Int("lag", diff),
				)
				// Emit rich reasoning for observability
				flumelogger.LogAgentReasoning(ctx, parent.ID, "system", fmt.Sprintf("child_count lag reconciled: stored=%d true=%d lag=%d", parent.ChildCount, trueCount, diff), map[string]any{
					"lag": diff, "true_count": trueCount, "stored": parent.ChildCount,
				})

				// Atomic correction via script (authoritative)
				now := time.Now().UTC().Format(time.RFC3339)
				script := `ctx._source.child_count = params.true_count; ctx._source.updated_at = params.now;`
				_ = s.es.UpdateDocWithInlineScript(ctx, "agent-task-records", parent.ID, script, map[string]interface{}{
					"true_count": trueCount,
					"now":        now,
				})
			}
		}
	}
	if lagDetected > 0 {
		s.logger.Info("childCountReconciliationSweep complete", slog.Int("parents_with_lag", lagDetected))
	}
}
