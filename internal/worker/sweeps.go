package worker

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
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Sweeper handles periodic maintenance sweeps for the task queue.
// Derived from Python: orchestration/sweeps.py (391 LOC, 13 AST nodes).
//
// Sweeps:
//   - Stuck Implementer: Requeue tasks stuck in "running" beyond timeout
//   - Stuck Review: Clear phantom review locks
//   - Promote: Move planned tasks to ready when dependencies are met
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
			"stuck_impl":   {},
			"stuck_review": {},
			"promote":      {},
			"consensus":    {},
			"parent_comp":  {},
		},
		intervals: map[string]time.Duration{
			"stuck_impl":   30 * time.Second,
			"stuck_review": 30 * time.Second,
			"promote":      5 * time.Second,
			"consensus":    5 * time.Second,
			"parent_comp":  5 * time.Second,
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
	promoteInterval := s.getPromoteInterval()
	if now.Sub(s.lastRun["promote"]) >= promoteInterval {
		s.lastRun["promote"] = now
		s.mu.Unlock()
		if count := s.promotePlannedTasks(ctx); count > 0 {
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
				map[string]interface{}{"term": map[string]string{"status": "review"}},
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
		if err := s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update); err == nil {
			cleared++
		}
	}
	return cleared
}

// promotePlannedTasks moves planned tasks to ready when dependencies are met.
// Derived from Python: promote_planned_tasks() (L150-200)
func (s *Sweeper) promotePlannedTasks(ctx context.Context) int {
	query := map[string]interface{}{
		"term": map[string]string{"status": "planned"},
	}

	result, err := s.es.Search(ctx, "agent-task-records", query, 100)
	if err != nil {
		s.logger.Warn("dependency sweep error", slog.String("error", err.Error()))
		return 0
	}

	promoted := 0
	for _, hit := range result.Hits {
		var task struct {
			ID        string   `json:"id"`
			ParentID  string   `json:"parent_id"`
			DependsOn []string `json:"depends_on"`
		}
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		// Check parent status if there is a parent task
		if task.ParentID != "" {
			parentDoc, err := s.es.GetDoc(ctx, "agent-task-records", task.ParentID)
			if err != nil || parentDoc == nil {
				continue
			}
			var parent struct {
				Status string `json:"status"`
			}
			if json.Unmarshal(parentDoc, &parent) != nil {
				continue
			}
			if parent.Status == "planned" || parent.Status == "blocked" || parent.Status == "archived" {
				continue // parent not active or terminal
			}
		}

		// Check DependsOn sibling dependencies
		dependsOnMet := true
		for _, depID := range task.DependsOn {
			if depID == "" {
				continue
			}
			depDoc, err := s.es.GetDoc(ctx, "agent-task-records", depID)
			if err != nil || depDoc == nil {
				dependsOnMet = false
				break
			}
			var dep struct {
				Status string `json:"status"`
			}
			if json.Unmarshal(depDoc, &dep) != nil {
				dependsOnMet = false
				break
			}
			if dep.Status != "done" && dep.Status != "archived" {
				dependsOnMet = false
				break
			}
		}

		if !dependsOnMet {
			continue
		}

		update := map[string]interface{}{
			"status":     "ready",
			"updated_at": time.Now().UTC().Format(time.RFC3339),
		}
		if err := s.es.UpdateDoc(ctx, "agent-task-records", task.ID, update); err == nil {
			promoted++
			s.logger.Info("promoted task to ready",
				slog.String("task_id", task.ID))
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
				Model:     task.Model,
				Provider:  task.Provider,
				AgentRole: "critic",
				TaskID:    task.ID,
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
	if err != nil {
		return
	}

	for _, hit := range result.Hits {
		var parent ftypes.Task
		if json.Unmarshal(hit, &parent) != nil {
			continue
		}

		// Find all child tasks
		childQuery := map[string]interface{}{
			"term": map[string]string{"parent_id": parent.ID},
		}

		childRes, err := s.es.Search(ctx, "agent-task-records", childQuery, 100)
		if err != nil {
			continue
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
				slog.String("title", parent.Title))

			update := map[string]interface{}{
				"status":       string(ftypes.TaskStatusDone),
				"completed_at": time.Now().UTC().Format(time.RFC3339),
				"updated_at":   time.Now().UTC().Format(time.RFC3339),
			}
			_ = s.es.UpdateDoc(ctx, "agent-task-records", parent.ID, update)
		}
	}
}
