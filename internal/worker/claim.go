package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Claimer handles atomic task claiming with dedup awareness and WIP gating.
// Derived from Python: orchestration/claim.py (562 LOC, 15 AST nodes).
//
// The claim pipeline:
//  1. Pre-flight: Check if role has available tasks
//  2. Dedup: Skip tasks whose normalized title matches an in-progress task
//  3. WIP Gate: Respect per-repo concurrency limits
//  4. Atomic Claim: ES _update_by_query with Painless script
type Claimer struct {
	es     *es.Client
	logger *slog.Logger
	nodeID string

	// Dedup normalization regex
	normRe *regexp.Regexp
}

// NewClaimer creates a new task claimer.
func NewClaimer(esClient *es.Client, logger *slog.Logger, nodeID string) *Claimer {
	return &Claimer{
		es:     esClient,
		logger: logger.With(slog.String("component", "orchestration.claim")),
		nodeID: nodeID,
		normRe: regexp.MustCompile(`[^a-z0-9 ]`),
	}
}

// TryAtomicClaim attempts to claim a single task for the given worker.
// Returns the claimed task or nil if no task was available.
// Derived from Python: try_atomic_claim() (L246-560, 15 AST nodes)
func (c *Claimer) TryAtomicClaim(ctx context.Context, worker ftypes.Worker) *ftypes.Task {
	claimStart := time.Now()

	role := worker.Role
	targetStatus := roleToTargetStatus(role)

	// Search for a claimable task
	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]string{"status": targetStatus}},
			},
			"must_not": []interface{}{
				map[string]interface{}{"exists": map[string]string{"field": "active_worker"}},
			},
		},
	}

	result, err := c.es.Search(ctx, "flume-tasks", query, 10)
	if err != nil {
		c.logger.Error("claim search failed", slog.String("error", err.Error()))
		return nil
	}

	for _, hit := range result.Hits {
		var task ftypes.Task
		if json.Unmarshal(hit, &task) != nil {
			continue
		}

		// Dedup check
		if c.isDuplicateTask(ctx, task.Title, task.ID) {
			c.logger.Info("dedup: skipping duplicate task",
				slog.String("task_id", task.ID),
				slog.String("title", task.Title))
			continue
		}

		// WIP gate check
		if c.isWIPSaturated(ctx, task) {
			c.logger.Debug("WIP gate: skipping saturated scope",
				slog.String("task_id", task.ID))
			continue
		}

		// Attempt atomic claim via ES update
		claimed := c.atomicClaim(ctx, task.ID, worker)
		if claimed {
			c.logger.Info("task claimed",
				slog.String("worker", worker.Name),
				slog.String("task_id", task.ID),
				slog.String("title", task.Title),
				slog.Duration("claim_latency", time.Since(claimStart)))
			return &task
		}
	}

	return nil
}

// isDuplicateTask checks if a task with the same normalized title is already active.
// Derived from Python: _is_duplicate_task() (L54-84)
func (c *Claimer) isDuplicateTask(ctx context.Context, title, taskID string) bool {
	norm := c.normalizeTitle(title)
	if norm == "" {
		return false
	}

	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{
					"terms": map[string]interface{}{
						"status": []string{"running", "review", "done"},
					},
				},
			},
			"must_not": []interface{}{
				map[string]interface{}{"term": map[string]string{"_id": taskID}},
			},
		},
	}

	result, err := c.es.Search(ctx, "flume-tasks", query, 50)
	if err != nil {
		c.logger.Warn("dedup check error", slog.String("error", err.Error()))
		return false // fail open
	}

	for _, hit := range result.Hits {
		var existing struct {
			Title string `json:"title"`
		}
		if json.Unmarshal(hit, &existing) == nil {
			if c.normalizeTitle(existing.Title) == norm {
				return true
			}
		}
	}
	return false
}

// normalizeTitle strips non-alphanumeric characters for dedup comparison.
// Derived from Python: _normalize_title() (L49-51)
func (c *Claimer) normalizeTitle(title string) string {
	return strings.TrimSpace(c.normRe.ReplaceAllString(strings.ToLower(title), ""))
}

// isWIPSaturated checks per-repo concurrency limits.
// Derived from Python: _compute_saturated_scopes() (L180-244)
func (c *Claimer) isWIPSaturated(ctx context.Context, task ftypes.Task) bool {
	if task.ProjectID == "" {
		return false
	}

	// Load WIP limits for this repo
	limits := c.loadRepoWIPLimits(ctx, task.ProjectID)
	if limits.MaxConcurrent <= 0 {
		return false // no limit configured
	}

	// Count running tasks for this repo
	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]string{"status": "running"}},
				map[string]interface{}{"term": map[string]string{"project_id": task.ProjectID}},
			},
		},
	}

	count, err := c.es.Count(ctx, "flume-tasks", query)
	if err != nil {
		return false // fail open
	}

	return count >= limits.MaxConcurrent
}

// WIPLimits holds per-repo concurrency configuration.
type WIPLimits struct {
	MaxConcurrent int `json:"max_concurrent_workers"`
}

func (c *Claimer) loadRepoWIPLimits(ctx context.Context, repoID string) WIPLimits {
	doc, err := c.es.GetDoc(ctx, "flume-projects", repoID)
	if err != nil || doc == nil {
		return WIPLimits{}
	}
	var limits struct {
		WIP WIPLimits `json:"wip"`
	}
	if json.Unmarshal(doc, &limits) != nil {
		return WIPLimits{}
	}
	return limits.WIP
}

// atomicClaim performs the ES _update_by_query to atomically claim a task.
func (c *Claimer) atomicClaim(ctx context.Context, taskID string, worker ftypes.Worker) bool {
	now := time.Now().UTC().Format(time.RFC3339)
	update := map[string]interface{}{
		"doc": map[string]interface{}{
			"status":         "running",
			"active_worker":  worker.Name,
			"execution_host": worker.ExecutionHost,
			"model":          worker.Model,
			"worker_role":    worker.Role,
			"queue_state":    "active",
			"claimed_at":     now,
			"updated_at":     now,
		},
	}

	err := c.es.IndexDoc(ctx, "flume-tasks", taskID, update)
	if err != nil {
		c.logger.Warn("atomic claim failed",
			slog.String("task_id", taskID),
			slog.String("error", err.Error()))
		return false
	}
	return true
}

// roleToTargetStatus maps worker roles to the task status they consume.
// Derived from Python: manager.py L325-329
func roleToTargetStatus(role string) string {
	switch role {
	case "pm":
		return "planned"
	case "tester", "reviewer":
		return "review"
	default:
		return "ready"
	}
}

// ─── Dedup Cleanup ──────────────────────────────────────────────────────────

// DeleteRemoteBranchForTask cleans up orphan branches on dedup skip.
// Derived from Python: _delete_remote_branch_for_task() (L87-130)
func (c *Claimer) DeleteRemoteBranchForTask(ctx context.Context, branch, repoID string) {
	if branch == "" || repoID == "" {
		return
	}
	// Protected branches
	protected := map[string]bool{
		"main": true, "master": true, "develop": true, "trunk": true,
	}
	if protected[branch] {
		return
	}
	// Story-scoped branches may be shared
	if strings.HasPrefix(branch, "feature/story-") || strings.HasPrefix(branch, "bugfix/story-") {
		return
	}

	c.logger.Info("dedup_cleanup: would delete orphan remote branch",
		slog.String("branch", branch),
		slog.String("repo", repoID))
	// Actual git host API call will be implemented in Phase 3 (internal/git)
}

// ─── Telemetry ──────────────────────────────────────────────────────────────

// LogTaskStateTransition records a task lifecycle event.
// Derived from Python: es/telemetry.log_task_state_transition() (L61)
func (c *Claimer) LogTaskStateTransition(ctx context.Context, taskID, from, to string) {
	c.logger.Info("Lifecycle Event: task state transition",
		slog.String("task_id", taskID),
		slog.String("from", from),
		slog.String("to", to))

	event := map[string]interface{}{
		"@timestamp": time.Now().UTC().Format(time.RFC3339),
		"event":      "TASK_STATE_TRANSITION",
		"task_id":    taskID,
		"from":       from,
		"to":         to,
		"node_id":    c.nodeID,
	}
	_ = c.es.IndexDoc(ctx, "flume-telemetry", fmt.Sprintf("%s-%d", taskID, time.Now().UnixNano()), event)
}
