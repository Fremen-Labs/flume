package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	"github.com/Fremen-Labs/flume/internal/llm"
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Claimer handles atomic task claiming with dedup awareness and WIP gating.
// Derived from Python: orchestration/claim.py (562 LOC, 15 AST nodes).
//
// The claim pipeline:
//  1. Pre-flight: Check if role has available tasks
//  2. Dedup: Skip tasks whose normalized title matches an in-progress task
//  3. WIP Gate: Respect per-repo concurrency limits
//  4. Git Overlap & Lock Checks: Ensure branch and repository are safe and don't overlap on modified files
//  5. Atomic OCC Claim: ES _update with if_seq_no and if_primary_term parameters
type Claimer struct {
	es        *es.Client
	llmClient *llm.Client
	logger    *slog.Logger
	nodeID    string

	// Dedup normalization regex
	normRe *regexp.Regexp
}

// NewClaimer creates a new task claimer.
func NewClaimer(esClient *es.Client, llmClient *llm.Client, logger *slog.Logger, nodeID string) *Claimer {
	return &Claimer{
		es:        esClient,
		llmClient: llmClient,
		logger:    logger.With(slog.String("component", "orchestration.claim")),
		nodeID:    nodeID,
		normRe:    regexp.MustCompile(`[^a-z0-9 ]`),
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
	// Fix 6 extension: exclude any task that carries decomposition markers (decomposed_at).
	// Such tasks were already processed by handlePM (or equivalent); claiming them again
	// would feed the death spiral even if their status was left in a claimable bucket by a race.
	// This is the ES-level filter; the post-fetch raw/struct check in the loop is belt-and-suspenders.
	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]string{"status": targetStatus}},
				map[string]interface{}{
					"bool": map[string]interface{}{
						"should": []interface{}{
							map[string]interface{}{"term": map[string]string{"worker_role": role}},
							map[string]interface{}{"term": map[string]string{"owner": role}},
							map[string]interface{}{"term": map[string]string{"assigned_agent_role": role}},
						},
						"minimum_should_match": 1,
					},
				},
			},
			"must_not": []interface{}{
				map[string]interface{}{"exists": map[string]string{"field": "active_worker"}},
				map[string]interface{}{"exists": map[string]string{"field": "decomposed_at"}},
			},
		},
	}

	result, err := c.es.Search(ctx, "agent-task-records", query, 10)
	if err != nil {
		c.logger.Error("claim search failed", slog.String("error", err.Error()))
		return nil
	}

	for _, rawHit := range result.RawHits {
		var task ftypes.Task
		if json.Unmarshal(rawHit.Source, &task) != nil {
			continue
		}

		// Safeguard: verify task has a valid ID
		if task.ID == "" {
			task.ID = rawHit.ID
		}
		if task.ID == "" {
			c.logger.Warn("claim: task document has no ID, skipping", slog.String("hit", string(rawHit.Source)))
			continue
		}

		// === Fix 6 (PM decomp death spiral): reject tasks carrying decomposition markers ===
		// The claimer snapshot may see a "planned" (or ready) task whose handlePM already wrote
		// decomposed_at/child_count (via the atomic combined update) but whose status write raced
		// or was left claimable. Checking BOTH the populated struct AND the raw source JSON
		// catches deserialization edge cases for *time.Time and any schema drift.
		// This is defense-in-depth alongside the primary ES-child-search guard inside handlePM.
		if task.DecomposedAt != nil || task.ChildCount > 0 || hasDecompMarkers(rawHit.Source) {
			c.logger.Info("claim: skipping already-decomposed task (Fix 6 denorm/raw guard)",
				slog.String("task_id", task.ID),
				slog.String("status", string(task.Status)),
				slog.Int("child_count", task.ChildCount),
				slog.String("title", task.Title))
			continue
		}

		// === Emergency work pause guard (prevents claiming anything for a halted project) ===
		// This is what makes "POST /api/workflow/agents/stop?repo=xxx" actually stop the bleeding.
		if task.ProjectID != "" {
			projDoc, _ := c.es.GetDoc(ctx, "flume-projects", task.ProjectID)
			if projDoc != nil {
				var proj ftypes.Project
				if json.Unmarshal(projDoc, &proj) == nil && proj.WorkPaused {
					c.logger.Warn("claim: skipping — project is emergency-paused (WorkPaused)",
						slog.String("task_id", task.ID), slog.String("repo", task.ProjectID))
					continue
				}
			}
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

		// Pre-claim Git lock and overlap check
		if c.checkGitOverlap(ctx, task) {
			continue
		}

		// Attempt atomic claim via ES update with Optimistic Concurrency Control
		var seq int64
		var prim int64
		if rawHit.SeqNo != nil {
			seq = *rawHit.SeqNo
		}
		if rawHit.PrimaryTerm != nil {
			prim = *rawHit.PrimaryTerm
		}
		claimed := c.atomicClaim(ctx, task.ID, worker, task.Status, seq, prim)
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

	result, err := c.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		c.logger.Warn("dedup check error", slog.String("error", err.Error()))
		return false // fail open
	}

	// 1. Text-based normalized match (fast path)
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

	// 2. Semantic similarity using embeddings (slow path fallback)
	if c.llmClient != nil {
		candidateVector, err := c.llmClient.Embed(ctx, title, "", "")
		if err == nil && len(candidateVector) > 0 {
			for _, hit := range result.Hits {
				var existing struct {
					Title string `json:"title"`
				}
				if json.Unmarshal(hit, &existing) == nil && existing.Title != "" {
					existingVector, err := c.llmClient.Embed(ctx, existing.Title, "", "")
					if err == nil && len(existingVector) > 0 {
						sim := cosineSimilarity(candidateVector, existingVector)
						if sim > 0.85 {
							c.logger.Info("semantic duplicate detected",
								slog.String("title1", title),
								slog.String("title2", existing.Title),
								slog.Float64("similarity", sim))
							return true
						}
					}
				}
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
				map[string]interface{}{"term": map[string]string{"repo": task.ProjectID}},
			},
		},
	}

	count, err := c.es.Count(ctx, "agent-task-records", query)
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

// atomicClaim performs the ES _update to atomically claim a task.
func (c *Claimer) atomicClaim(ctx context.Context, taskID string, worker ftypes.Worker, prevStatus ftypes.TaskStatus, seqNo, primaryTerm int64) bool {
	now := time.Now().UTC().Format(time.RFC3339)
	update := map[string]interface{}{
		"status":         "running",
		"active_worker":  worker.Name,
		"execution_host": worker.ExecutionHost,
		"model":          worker.Model,
		"worker_role":    worker.Role,
		"queue_state":    "active",
		"claimed_at":     now,
		"updated_at":     now,
	}

	// PR 2 + Phase 0: atomic claim status (running) MUST go through EnforceTransition (OCC preserved in update layer)
	// In strict mode (default), abort the claim on violation instead of proceeding.
	if enforceErr := ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog(prevStatus, "running", c.logger.Warn); enforceErr != nil {
		flumelogger.LogTaskStateViolation(ctx, taskID, string(prevStatus), "running", enforceErr, ftypes.DefaultTaskStateMachine.ShadowMode)
		if !ftypes.DefaultTaskStateMachine.ShadowMode {
			c.logger.Error("claim aborted due to TaskStateMachine violation (strict mode)",
				slog.String("task_id", taskID), slog.String("error", enforceErr.Error()))
			return false
		}
	}
	err := c.es.UpdateDocOCC(ctx, "agent-task-records", taskID, update, seqNo, primaryTerm)
	if err != nil {
		if err == es.ErrConflict {
			c.logger.Warn("atomic claim conflict: task already claimed by another worker",
				slog.String("task_id", taskID))
		} else {
			c.logger.Warn("atomic claim failed",
				slog.String("task_id", taskID),
				slog.String("error", err.Error()))
		}
		return false
	}
	return true
}

// checkGitOverlap checks if the task modifies any files that are currently being modified by other running tasks in the same project.
func (c *Claimer) checkGitOverlap(ctx context.Context, task ftypes.Task) bool {
	if task.ProjectID == "" {
		return false
	}

	// Get project local path
	projDoc, err := c.es.GetDoc(ctx, "flume-projects", task.ProjectID)
	if err != nil || projDoc == nil {
		return false
	}
	var project ftypes.Project
	if json.Unmarshal(projDoc, &project) != nil || project.LocalPath == "" {
		return false
	}

	repoPath := project.LocalPath
	branch := c.branchName(task)

	// Check branch and repo locks
	if c.isRepoLocked(repoPath, branch) {
		c.logger.Warn("git lock detected: skipping task due to lock in repository",
			slog.String("task_id", task.ID),
			slog.String("repo_path", repoPath))
		return true
	}

	// Get modified files for the candidate task
	candidateFiles := c.getModifiedFiles(repoPath, branch)
	if len(candidateFiles) == 0 {
		return false // No files modified or branch doesn't exist yet
	}

	// Search for other running tasks in the same project
	query := map[string]interface{}{
		"bool": map[string]interface{}{
			"must": []interface{}{
				map[string]interface{}{"term": map[string]string{"status": "running"}},
				map[string]interface{}{"term": map[string]string{"repo": task.ProjectID}},
			},
			"must_not": []interface{}{
				map[string]interface{}{"term": map[string]string{"_id": task.ID}},
			},
		},
	}
	result, err := c.es.Search(ctx, "agent-task-records", query, 50)
	if err != nil {
		return false
	}

	for _, hit := range result.Hits {
		var runningTask ftypes.Task
		if json.Unmarshal(hit, &runningTask) == nil {
			runningBranch := c.branchName(runningTask)
			runningFiles := c.getModifiedFiles(repoPath, runningBranch)
			for _, cf := range candidateFiles {
				for _, rf := range runningFiles {
					if cf == rf {
						c.logger.Info("git overlap: skipping task due to file overlap with running task",
							slog.String("task_id", task.ID),
							slog.String("running_task_id", runningTask.ID),
							slog.String("conflicting_file", cf))
						return true // Overlap detected
					}
				}
			}
		}
	}

	return false
}

func (c *Claimer) isRepoLocked(repoPath, branch string) bool {
	// Check git index lock
	indexLock := fmt.Sprintf("%s/.git/index.lock", strings.TrimRight(repoPath, "/"))
	if _, err := os.Stat(indexLock); err == nil {
		return true
	}
	// Check branch ref lock
	branchLock := fmt.Sprintf("%s/.git/refs/heads/%s.lock", strings.TrimRight(repoPath, "/"), branch)
	if _, err := os.Stat(branchLock); err == nil {
		return true
	}
	return false
}

func (c *Claimer) getModifiedFiles(repoPath, branch string) []string {
	// Check if branch exists
	_, err := exec.Command("git", "-C", repoPath, "rev-parse", "--verify", branch).Output()
	if err != nil {
		return nil // Branch doesn't exist yet
	}

	defaultBranch := "main"
	if override := os.Getenv("FLUME_DEFAULT_BRANCH"); override != "" {
		defaultBranch = override
	}

	// Try comparing origin/defaultBranch
	out, err := exec.Command("git", "-C", repoPath, "diff", "--name-only", "origin/"+defaultBranch+"..."+branch).Output()
	if err != nil {
		// Fallback to local defaultBranch
		out, err = exec.Command("git", "-C", repoPath, "diff", "--name-only", defaultBranch+"..."+branch).Output()
		if err != nil {
			return nil
		}
	}

	lines := strings.Split(string(out), "\n")
	var files []string
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed != "" {
			files = append(files, trimmed)
		}
	}
	return files
}

// roleToTargetStatus maps worker roles to the task status they consume.
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

// hasDecompMarkers inspects the raw ES source JSON for decomposition markers.
// Used by Fix 6 guard to catch cases where the struct unmarshal may not have
// populated DecomposedAt (e.g. parse edge on *time.Time, bad timestamp format,
// or future field casing changes) even though the document in ES has the data.
func hasDecompMarkers(source []byte) bool {
	if len(source) == 0 {
		return false
	}
	var m map[string]json.RawMessage
	if json.Unmarshal(source, &m) != nil {
		return false
	}
	// decomposed_at present with a non-null value
	if v, ok := m["decomposed_at"]; ok && string(v) != "null" && len(v) > 2 {
		return true
	}
	// child_count present and > 0
	if v, ok := m["child_count"]; ok {
		var cnt int
		if json.Unmarshal(v, &cnt) == nil && cnt > 0 {
			return true
		}
	}
	return false
}

func (c *Claimer) branchName(task ftypes.Task) string {
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

	prefix := "feature"
	if scope := os.Getenv("FLUME_AUTO_PR_SCOPE"); scope != "" {
		prefix = scope
	}
	return fmt.Sprintf("%s/%s-%s", prefix, segment, shortHash)
}

func cosineSimilarity(a, b []float64) float64 {
	if len(a) != len(b) || len(a) == 0 {
		return 0
	}
	var dotProduct, normA, normB float64
	for i := range a {
		dotProduct += a[i] * b[i]
		normA += a[i] * a[i]
		normB += b[i] * b[i]
	}
	if normA == 0 || normB == 0 {
		return 0
	}
	return dotProduct / (math.Sqrt(normA) * math.Sqrt(normB))
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
// Now delegates to the centralized structured helper (Logloom-friendly) while
// preserving the legacy telemetry ES index write for backward compat.
func (c *Claimer) LogTaskStateTransition(ctx context.Context, taskID, from, to string) {
	// Use the new FAANG-grade helper — this ensures consistent attrs + Logloom correlation
	flumelogger.LogStateTransition(ctx, taskID, from, to, "lifecycle event via claimer", slog.String("node_id", c.nodeID))

	// Legacy telemetry (kept for existing dashboards / Python consumers)
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
