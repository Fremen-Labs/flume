package worker

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
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
	logger *slog.Logger

	// Throttle state — derived from Python: SWEEP_LAST_RUN, SWEEP_INTERVALS
	mu           sync.Mutex
	lastRun      map[string]time.Time
	intervals    map[string]time.Duration
	plannedCount int
}

// NewSweeper creates a new sweep orchestrator.
func NewSweeper(esClient *es.Client, logger *slog.Logger) *Sweeper {
	return &Sweeper{
		es:     esClient,
		logger: logger.With(slog.String("component", "orchestration.sweeps")),
		lastRun: map[string]time.Time{
			"stuck_impl":   {},
			"stuck_review": {},
			"promote":      {},
		},
		intervals: map[string]time.Duration{
			"stuck_impl":   30 * time.Second,
			"stuck_review": 30 * time.Second,
			"promote":      5 * time.Second,
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
		return
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
			ID       string `json:"id"`
			ParentID string `json:"parent_id"`
		}
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		// Check if dependencies are met (parent complete or no parent)
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
			if parent.Status != "done" && parent.Status != "archived" {
				continue // parent not yet complete
			}
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
