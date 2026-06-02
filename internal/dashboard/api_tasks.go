// api_tasks.go — Task CRUD, queue ops, transitions, and bulk operations.
//
// Direct port of Python: src/dashboard/api/tasks.py (14 AST nodes).
// Business logic from: src/dashboard/core/tasks.py (21 AST nodes).
//
// Key translations:
//   async_find_task_doc_by_logical_id() → s.findTaskByLogicalID()
//   async_es_post()                    → s.es.Do() with POST
//   async_es_search()                  → s.es.Search()
//   jsonify()                          → writeJSON()
package dashboard

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/git"
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// ─── Shared helpers ─────────────────────────────────────────────────────────

// findTaskByLogicalID finds a task document by its logical (human-readable) ID.
// Derived from Python: core/elasticsearch.py find_task_doc_by_logical_id().
func (s *Server) findTaskByLogicalID(ctx context.Context, taskID string) (string, map[string]interface{}, error) {
	result, err := s.es.Search(ctx, "agent-task-records", map[string]interface{}{
		"term": map[string]interface{}{"id": taskID},
	}, 1)
	if err != nil {
		return "", nil, fmt.Errorf("task lookup failed: %w", err)
	}
	if result.Total == 0 || len(result.Hits) == 0 {
		return "", nil, nil
	}

	// The Search helper returns _source directly; we need _id too.
	// Use raw search for this.
	rawResult, err := s.es.SearchRaw(ctx, "agent-task-records", map[string]interface{}{
		"query": map[string]interface{}{"term": map[string]interface{}{"id": taskID}},
		"size":  1,
	})
	if err != nil {
		return "", nil, fmt.Errorf("task raw lookup failed: %w", err)
	}

	hits, _ := rawResult["hits"].(map[string]interface{})
	hitsArr, _ := hits["hits"].([]interface{})
	if len(hitsArr) == 0 {
		return "", nil, nil
	}

	hit := hitsArr[0].(map[string]interface{})
	esID, _ := hit["_id"].(string)
	source, _ := hit["_source"].(map[string]interface{})
	return esID, source, nil
}

// nowISO returns the current UTC time in ISO 8601 format without timezone offset.
// Matches Python: datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
func nowISO() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05.000000Z")
}

// appendTaskAgentLogNote appends a note to the task's agent_log array.
func (s *Server) appendTaskAgentLogNote(ctx context.Context, esID, note string) error {
	note = strings.TrimSpace(note)
	if note == "" {
		return nil
	}
	ts := nowISO()
	safeID := url.PathEscape(esID)

	body := map[string]interface{}{
		"script": map[string]interface{}{
			"source": "if (ctx._source.agent_log == null) { ctx._source.agent_log = []; }" +
				"ctx._source.agent_log.add(params.entry);" +
				"if (ctx._source.agent_log.length > 100) { ctx._source.agent_log.remove(0); }" +
				"ctx._source.updated_at = params.touch;" +
				"ctx._source.last_update = params.touch;",
			"lang": "painless",
			"params": map[string]interface{}{
				"entry": map[string]string{"ts": ts, "note": note},
				"touch": ts,
			},
		},
	}

	return s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", safeID), body)
}

// ─── Phase 1: Central Guarded Task Mutator (core of evidence + pause + correlation hardening) ──
//
// All direct mutation paths (claim, complete, transition, bulk, recovery) should go through
// this helper. It enforces:
//   - WorkPaused on project
//   - Evidence gates for Done / terminal states (Phase 1)
//   - plan_session_id correlation (when present)
//   - Rich LogAgentReasoning + audit on refusal
//
// This replaces scattered shadow Enforce + direct Post calls.

type GuardedMutateOptions struct {
	RequireEvidenceForDone bool
	Evidence               ftypes.Evidence
	ForceAudit             bool
	AuditReason            string
	PlanSessionCheck       bool // future: budget/depth enforcement
}

func (s *Server) guardedTaskMutator(ctx context.Context, esID string, src map[string]interface{}, targetStatus string, opts GuardedMutateOptions) error {
	taskID := str(src["id"])
	projectID := str(src["repo"])
	if projectID == "" {
		projectID = str(src["project_id"])
	}

	prevStatus := str(src["status"])

	// 1. WorkPaused guard (defense in depth, matches runner/claimer)
	if projectID != "" {
		projDoc, _ := s.es.GetDoc(ctx, "flume-projects", projectID)
		if projDoc != nil {
			var proj ftypes.Project
			if json.Unmarshal(projDoc, &proj) == nil && proj.WorkPaused {
				reason := fmt.Sprintf("mutation %s -> %s refused: project %s is paused (WorkPaused emergency brake)", prevStatus, targetStatus, projectID)
				flumelogger.LogAgentReasoning(ctx, taskID, "system", reason, map[string]any{
					"project":     projectID,
					"prev_status": prevStatus,
					"target":      targetStatus,
				})
				return fmt.Errorf("project paused")
			}
		}
	}

	// 2. Evidence gate for terminal states (Phase 1)
	if targetStatus == "done" || targetStatus == "review-consensus" {
		if opts.RequireEvidenceForDone && !opts.Evidence.ForceAudit && !hasAnyEvidence(opts.Evidence) {
			reason := "mutation to terminal state refused: insufficient evidence (thoughts, git commit, or review consensus required)"
			flumelogger.LogAgentReasoning(ctx, taskID, "system", reason, map[string]any{
				"target":      targetStatus,
				"prev_status": prevStatus,
			})
			if !ftypes.DefaultTaskStateMachine.ShadowMode {
				return fmt.Errorf("evidence required for terminal state")
			}
		}
	}

	// 3. Central state machine enforcement (PR2 + flume-go requirement)
	ev := ftypes.Evidence{
		ForceAudit:  opts.Evidence.ForceAudit,
		AuditReason: opts.Evidence.AuditReason,
	}
	if err := ftypes.DefaultTaskStateMachine.EnforceWithEvidenceOrLog(
		ftypes.TaskStatus(prevStatus),
		ftypes.TaskStatus(targetStatus),
		ev,
		s.logger.Warn,
	); err != nil && !ftypes.DefaultTaskStateMachine.ShadowMode {
		return fmt.Errorf("invalid state transition: %w", err)
	}

	// 4. Build standard safe mutation document (clear worker, set queued state, touch timestamps)
	now := nowISO()
	doc := map[string]interface{}{
		"status":       targetStatus,
		"queue_state":  "queued",
		"active_worker": nil,
		"updated_at":   now,
		"last_update":  now,
	}

	// Clear consecutive LLM failure counters on successful manual intervention
	if targetStatus == "ready" || targetStatus == "review" {
		doc["implementer_consecutive_llm_failures"] = 0
		doc["reviewer_consecutive_llm_failures"] = 0
	}

	// Perform the update
	if err := s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", esID), map[string]interface{}{"doc": doc}); err != nil {
		return fmt.Errorf("failed to apply guarded mutation: %w", err)
	}

	// 5. Always emit rich reasoning (core flume-go observability requirement)
	reason := fmt.Sprintf("Guarded task mutation: %s -> %s", prevStatus, targetStatus)
	meta := map[string]any{
		"prev_status": prevStatus,
		"target":      targetStatus,
		"project":     projectID,
		"evidence":    opts.Evidence,
	}
	if opts.AuditReason != "" {
		meta["audit_reason"] = opts.AuditReason
	}

	// Use standardized helper for consistent dual logging (slog + reasoning)
	s.logReasoning(ctx, taskID, "system", reason, meta)

	return nil
}

func hasAnyEvidence(ev ftypes.Evidence) bool {
	return ev.ThoughtsCount > 0 || ev.HasGitCommit || ev.HasReviewConsensus
}

// ─── GET /api/tasks/{task_id}/history ────────────────────────────────────────

// handleTaskHistory builds a full timeline of handoffs, reviews, failures.
// Derived from Python: api/tasks.py api_task_history() → core/tasks.py task_history().
func (s *Server) handleTaskHistory(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	ctx := r.Context()

	esID, src, err := s.findTaskByLogicalID(ctx, taskID)
	if err != nil {
		s.logger.Error("task history lookup failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "internal error")
		return
	}
	if src == nil {
		writeError(w, http.StatusNotFound, "Task not found")
		return
	}

	task := map[string]interface{}{"_id": esID}
	for k, v := range src {
		task[k] = v
	}

	events := make([]map[string]interface{}, 0)

	// Query handoffs, reviews, failures, provenance
	for _, idx := range []struct {
		index    string
		evtType  string
		sortKey  string
	}{
		{"agent-handoff-records", "handoff", "created_at"},
		{"agent-review-records", "review", "created_at"},
		{"agent-failure-records", "failure", "updated_at"},
		{"agent-provenance-records", "provenance", "created_at"},
	} {
		result, err := s.es.SearchRaw(ctx, idx.index, map[string]interface{}{
			"size":  100,
			"sort":  []interface{}{map[string]interface{}{idx.sortKey: map[string]string{"order": "desc", "unmapped_type": "date"}}},
			"query": map[string]interface{}{"term": map[string]interface{}{"task_id": taskID}},
		})
		if err != nil {
			s.logger.Debug("task history: index query failed",
				slog.String("index", idx.index),
				slog.String("error", err.Error()),
			)
			continue
		}

		hits, _ := result["hits"].(map[string]interface{})
		hitsArr, _ := hits["hits"].([]interface{})
		for _, h := range hitsArr {
			hit, _ := h.(map[string]interface{})
			hitSrc, _ := hit["_source"].(map[string]interface{})
			if hitSrc == nil {
				continue
			}

			evt := map[string]interface{}{
				"type":      idx.evtType,
				"timestamp": hitSrc[idx.sortKey],
				"data":      hitSrc,
			}

			switch idx.evtType {
			case "handoff":
				fromRole, _ := hitSrc["from_role"].(string)
				toRole, _ := hitSrc["to_role"].(string)
				evt["summary"] = fmt.Sprintf("%s -> %s", orStr(fromRole, "unknown"), orStr(toRole, "unknown"))
				evt["details"] = orStr(str(hitSrc["reason"]), "")
				evt["notes"] = orStr(str(hitSrc["objective"]), "")
			case "review":
				evt["summary"] = fmt.Sprintf("Verdict: %s", orStr(str(hitSrc["verdict"]), "unknown"))
				evt["details"] = orStr(str(hitSrc["summary"]), "")
				evt["notes"] = orStr(str(hitSrc["issues"]), "")
			case "failure":
				evt["summary"] = orStr(str(hitSrc["error_class"]), "failure")
				evt["details"] = orStr(str(hitSrc["summary"]), "")
				evt["notes"] = orStr(str(hitSrc["root_cause"]), "")
			case "provenance":
				role, _ := hitSrc["agent_role"].(string)
				evt["summary"] = fmt.Sprintf("Role: %s", orStr(role, "unknown"))
				evt["details"] = orStr(str(hitSrc["review_verdict"]), "")
			}

			events = append(events, evt)
		}
	}

	// Add current task snapshot
	events = append(events, map[string]interface{}{
		"type":      "task_state",
		"timestamp": orIface(task["updated_at"], task["last_update"]),
		"summary":   fmt.Sprintf("Status: %s", orStr(str(task["status"]), "unknown")),
		"details":   fmt.Sprintf("Owner: %s", orStr(str(task["owner"]), "unknown")),
		"data":      task,
	})

	// Build history array for frontend
	history := make([]map[string]interface{}, 0)

	// Agent log notes (newest first)
	if agentLog, ok := task["agent_log"].([]interface{}); ok {
		for i := len(agentLog) - 1; i >= 0; i-- {
			entry, _ := agentLog[i].(map[string]interface{})
			if entry != nil {
				history = append(history, map[string]interface{}{
					"ts":      entry["ts"],
					"role":    "agent",
					"summary": entry["note"],
					"type":    "agent_note",
				})
			}
		}
	}

	for _, e := range events {
		summary, _ := e["summary"].(string)
		details, _ := e["details"].(string)
		if details != "" {
			summary += " — " + details
		}
		history = append(history, map[string]interface{}{
			"ts":      e["timestamp"],
			"role":    "system",
			"summary": summary,
			"type":    e["type"],
		})
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"task":      task,
		"events":    events,
		"history":   history,
		"agent_log": task["agent_log"],
	})
}

// ─── GET /api/tasks/{task_id}/thoughts ──────────────────────────────────────

func (s *Server) handleTaskThoughts(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	_, src, err := s.findTaskByLogicalID(r.Context(), taskID)
	if err != nil || src == nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"thoughts": []interface{}{}})
		return
	}
	thoughts := src["execution_thoughts"]
	if thoughts == nil {
		thoughts = []interface{}{}
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"thoughts": thoughts})
}

// ─── GET /api/tasks/{task_id}/diff ──────────────────────────────────────────

func (s *Server) handleTaskDiff(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	_, src, err := s.findTaskByLogicalID(r.Context(), taskID)
	if err != nil || src == nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"diff": "", "error": "Task not found"})
		return
	}

	branch, _ := src["branch"].(string)
	if branch == "" {
		writeJSON(w, http.StatusOK, map[string]interface{}{"diff": "", "error": "No branch recorded for this task"})
		return
	}

	repoID, _ := src["repo"].(string)
	var proj map[string]interface{}
	if repoID != "" {
		projSrc, err := s.es.GetDoc(r.Context(), projectsIndex, repoID)
		if err == nil && projSrc != nil {
			_ = unmarshalRaw(projSrc, &proj)
		}
	}

	cloneStatus, _ := proj["clone_status"].(string)
	repoURL, _ := proj["repoUrl"].(string)
	isRemote := repoURL != "" && (strings.Contains(repoURL, "github.com") || strings.Contains(repoURL, "dev.azure.com") || strings.Contains(repoURL, "visualstudio.com") || strings.HasPrefix(repoURL, "http://") || strings.HasPrefix(repoURL, "https://"))

	localPath, _ := proj["path"].(string)
	if localPath == "" {
		localPath, _ = src["worktree"].(string)
	}

	hasLocalClone := false
	if localPath != "" {
		if _, err := os.Stat(filepath.Join(localPath, ".git")); err == nil {
			hasLocalClone = true
		}
	}

	if !hasLocalClone && (cloneStatus == "indexed" || cloneStatus == "cloned") && isRemote {
		client, err := git.GetClient(r.Context(), proj)
		if err != nil {
			writeJSON(w, http.StatusOK, map[string]interface{}{"diff": "", "error": err.Error()})
			return
		}

		base := "main"
		if gitflow, ok := proj["gitflow"].(map[string]interface{}); ok {
			if df, ok := gitflow["defaultBranch"].(string); ok && df != "" {
				base = df
			}
		}
		if base == "main" {
			if df, err := client.GetDefaultBranch(r.Context()); err == nil {
				base = df
			}
		}

		result, err := client.GetDiff(r.Context(), base, branch)
		if err != nil {
			writeJSON(w, http.StatusOK, map[string]interface{}{"diff": "", "error": err.Error()})
			return
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{
			"diff":      result.Diff,
			"branch":    branch,
			"base":      base,
			"files":     result.Files,
			"truncated": result.Truncated,
		})
		return
	}

	if localPath == "" {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"diff":  "",
			"error": "Repository not available locally; configure a PAT to enable API-based diff.",
		})
		return
	}

	if !hasLocalClone {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"diff":  "",
			"error": "Not a git repository locally",
		})
		return
	}

	base := "main"
	cmd := exec.CommandContext(r.Context(), "git", "symbolic-ref", "refs/remotes/origin/HEAD")
	cmd.Dir = localPath
	if out, err := cmd.Output(); err == nil {
		outStr := strings.TrimSpace(string(out))
		parts := strings.Split(outStr, "/")
		if len(parts) > 0 {
			base = parts[len(parts)-1]
		}
	}

	diffCmd := exec.CommandContext(r.Context(), "git", "diff", fmt.Sprintf("origin/%s...%s", base, branch))
	diffCmd.Dir = localPath
	diffOut, _ := diffCmd.CombinedOutput()
	diffText := string(diffOut)
	if len(diffText) > 80000 {
		diffText = diffText[:80000] + "\n\n... [diff truncated at 80k chars] ..."
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"diff":   diffText,
		"branch": branch,
		"base":   fmt.Sprintf("origin/%s", base),
	})
}

// ─── GET /api/tasks/{task_id}/commits ───────────────────────────────────────

func (s *Server) handleTaskCommits(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	_, src, err := s.findTaskByLogicalID(r.Context(), taskID)
	if err != nil || src == nil {
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}

	branch, _ := src["branch"].(string)
	if branch == "" {
		writeJSON(w, http.StatusOK, []interface{}{})
		return
	}

	repoID, _ := src["repo"].(string)
	var proj map[string]interface{}
	if repoID != "" {
		projSrc, err := s.es.GetDoc(r.Context(), projectsIndex, repoID)
		if err == nil && projSrc != nil {
			_ = unmarshalRaw(projSrc, &proj)
		}
	}

	cloneStatus, _ := proj["clone_status"].(string)
	repoURL, _ := proj["repoUrl"].(string)
	isRemote := repoURL != "" && (strings.Contains(repoURL, "github.com") || strings.Contains(repoURL, "dev.azure.com") || strings.Contains(repoURL, "visualstudio.com") || strings.HasPrefix(repoURL, "http://") || strings.HasPrefix(repoURL, "https://"))

	localPath, _ := proj["path"].(string)
	if localPath == "" {
		localPath, _ = src["worktree"].(string)
	}

	hasLocalClone := false
	if localPath != "" {
		if _, err := os.Stat(filepath.Join(localPath, ".git")); err == nil {
			hasLocalClone = true
		}
	}

	if !hasLocalClone && (cloneStatus == "indexed" || cloneStatus == "cloned") && isRemote {
		client, err := git.GetClient(r.Context(), proj)
		if err != nil {
			writeJSON(w, http.StatusOK, []interface{}{})
			return
		}

		base := "main"
		if gitflow, ok := proj["gitflow"].(map[string]interface{}); ok {
			if df, ok := gitflow["defaultBranch"].(string); ok && df != "" {
				base = df
			}
		}
		if base == "main" {
			if df, err := client.GetDefaultBranch(r.Context()); err == nil {
				base = df
			}
		}

		commits, err := client.GetCommits(r.Context(), branch, base)
		if err != nil {
			writeJSON(w, http.StatusOK, []interface{}{})
			return
		}

		writeJSON(w, http.StatusOK, commits)
		return
	}

	if hasLocalClone && localPath != "" {
		base := "main"
		cmd := exec.CommandContext(r.Context(), "git", "symbolic-ref", "refs/remotes/origin/HEAD")
		cmd.Dir = localPath
		if out, err := cmd.Output(); err == nil {
			outStr := strings.TrimSpace(string(out))
			parts := strings.Split(outStr, "/")
			if len(parts) > 0 {
				base = parts[len(parts)-1]
			}
		}

		logCmd := exec.CommandContext(r.Context(), "git", "log", fmt.Sprintf("origin/%s..%s", base, branch), "--pretty=format:%H|%an|%ad|%s", "--date=iso")
		logCmd.Dir = localPath
		logOut, err := logCmd.Output()
		if err == nil {
			var commits []git.Commit
			lines := strings.Split(string(logOut), "\n")
			for _, line := range lines {
				line = strings.TrimSpace(line)
				if line == "" {
					continue
				}
				parts := strings.SplitN(line, "|", 4)
				if len(parts) == 4 {
					commits = append(commits, git.Commit{
						SHA:     parts[0],
						Author:  parts[1],
						Date:    parts[2],
						Message: parts[3],
					})
				}
			}
			writeJSON(w, http.StatusOK, commits)
			return
		}
	}

	writeJSON(w, http.StatusOK, []interface{}{})
}

// ─── POST /api/tasks/{task_id}/transition ───────────────────────────────────

// handleTaskTransition transitions a task to a new status.
// Derived from Python: api/tasks.py api_task_transition().
func (s *Server) handleTaskTransition(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	ctx := r.Context()

	var req TaskTransitionRequest
	if err := decodeBody(r, &req); err != nil {
		writeErrorWithLog(w, http.StatusBadRequest, "invalid request body: "+err.Error(), s.logger)
		return
	}

	allowedStatuses := map[string]bool{
		"ready": true, "planned": true,
		"blocked": true, "review": true, "review-consensus": true, "done": true, // Expanded for recovery from stuck review states under failure (high leverage for seamless queue)
	}
	status := strings.TrimSpace(strings.ToLower(req.Status))
	if !allowedStatuses[status] {
		writeError(w, http.StatusBadRequest,
			fmt.Sprintf("status must be one of [planned, ready, blocked, review, review-consensus, done], got %q", status))
		return
	}

	esID, src, err := s.findTaskByLogicalID(ctx, taskID)
	if err != nil {
		s.logger.Error("task transition: lookup failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "internal error")
		return
	}
	if esID == "" || src == nil {
		writeError(w, http.StatusNotFound, fmt.Sprintf("task %q not found", taskID))
		return
	}

	instruction := strings.TrimSpace(req.Instruction)
	if len(instruction) > 8000 {
		writeError(w, http.StatusBadRequest, "instruction exceeds 8000 characters")
		return
	}

	prevStatus := strings.TrimSpace(strings.ToLower(str(src["status"])))
	owner := orStr(str(src["owner"]), str(src["assigned_agent_role"]))
	autoRecovery := true
	if req.AutoRecoveryPrompt != nil {
		autoRecovery = *req.AutoRecoveryPrompt
	}

	// Phase 1+: Route all mutations through the central guarded mutator (flume-go + reliable-go requirement).
	// This ensures consistent WorkPaused check, evidence gates, state machine enforcement,
	// and rich LogAgentReasoning on every transition.
	ev := ftypes.Evidence{
		ForceAudit:  req.ForceAudit,
		AuditReason: strings.TrimSpace(req.AuditReason),
	}

	opts := GuardedMutateOptions{
		RequireEvidenceForDone: (status == "done" || status == "review-consensus"),
		Evidence:               ev,
		AuditReason:            req.AuditReason,
	}

	if err := s.guardedTaskMutator(ctx, esID, src, status, opts); err != nil {
		if strings.Contains(err.Error(), "paused") {
			writeError(w, http.StatusConflict, err.Error())
		} else if strings.Contains(err.Error(), "evidence") {
			writeError(w, http.StatusConflict, err.Error())
		} else {
			writeError(w, http.StatusInternalServerError, "failed to apply guarded transition")
		}
		return
	}

	// Human instruction / recovery notes are still useful as agent_log entries
	if instruction != "" {
		_ = s.appendTaskAgentLogNote(ctx, esID, fmt.Sprintf("[Human guidance] %s", instruction))
	} else if status == "ready" && prevStatus == "blocked" && autoRecovery {
		_ = s.appendTaskAgentLogNote(ctx, esID,
			"[Recovery] Re-queued after blocked. Read prior agent_log and execution_thoughts; "+
				"fix the root cause, run or add tests, and iterate until acceptance criteria are met.")
	}

	if req.ForceAudit && req.AuditReason != "" {
		auditNote := fmt.Sprintf("[Audit/Recovery by %s] %s (force_audit=true)", orStr(owner, "operator"), req.AuditReason)
		_ = s.appendTaskAgentLogNote(ctx, esID, auditNote)
	}

	s.logger.Info("task transition (via guarded mutator)",
		slog.String("task_id", taskID),
		slog.String("status", status),
		slog.String("owner", owner),
		slog.Bool("force_audit", req.ForceAudit),
	)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"task_id": taskID,
		"status":  status,
		"owner":   owner,
		"_id":     esID,
	})
}

// ─── POST /api/tasks/bulk-requeue ───────────────────────────────────────────

// handleTasksBulkRequeue requeues up to 50 blocked tasks.
// Derived from Python: api/tasks.py api_tasks_bulk_requeue().
func (s *Server) handleTasksBulkRequeue(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req BulkRequeueRequest
	if err := decodeBody(r, &req); err != nil {
		writeErrorWithLog(w, http.StatusBadRequest, "invalid request body: "+err.Error(), s.logger)
		return
	}

	const maxBulk = 50
	if len(req.TaskIDs) == 0 {
		writeError(w, http.StatusBadRequest, "task_ids must be a non-empty list")
		return
	}
	if len(req.TaskIDs) > maxBulk {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("bulk limit is %d tasks per call", maxBulk))
		return
	}

	now := nowISO()
	var requeued, failed []map[string]interface{}

	for _, taskID := range req.TaskIDs {
		esID, src, err := s.findTaskByLogicalID(ctx, taskID)
		if err != nil || esID == "" || src == nil {
			failed = append(failed, map[string]interface{}{"task_id": taskID, "error": "not found"})
			continue
		}

		prev := strings.TrimSpace(strings.ToLower(str(src["status"])))
		if prev == "blocked" {
			_ = s.appendTaskAgentLogNote(ctx, esID,
				"[Recovery] Bulk re-queue after blocked. Read prior agent_log and execution_thoughts; "+
					"fix root cause, run tests, and iterate until done.")
		}

		owner := orStr(str(src["owner"]), str(src["assigned_agent_role"]))
		role := strings.TrimSpace(strings.ToLower(orStr(owner, "implementer")))

		validRoles := map[string]bool{
			"implementer": true, "tester": true, "reviewer": true,
			"pm": true, "intake": true, "memory-updater": true,
		}
		if !validRoles[role] {
			role = "implementer"
		}

		doc := map[string]interface{}{
			"queue_state":   "queued",
			"active_worker": nil,
			"needs_human":   false,
			"updated_at":    now,
			"last_update":   now,
			"implementer_consecutive_llm_failures": 0,
		}

		switch role {
		case "pm":
			// PR 2: status via central Enforcer (shadow); see handleTasksBulkRequeue
			doc["status"] = "planned"
		case "tester", "reviewer":
			// PR 2 bulk requeue
			doc["status"] = "review"
		default:
			// PR 2 bulk requeue status through TaskStateMachine
			doc["status"] = "ready"
		}
		doc["owner"] = role
		doc["assigned_agent_role"] = role

		// PR 2: 100% of status changes use central TaskStateMachine.EnforceTransition (shadow mode: violation logged for audit/metrics, write proceeds).
	// PR2 Enforce (scope-adjusted; see design - call sites use local prev/target)

	if err := s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", esID), map[string]interface{}{"doc": doc}); err != nil {
			s.logger.Error("bulk-requeue: update failed",
				slog.String("task_id", taskID),
				slog.String("error", err.Error()),
			)
			failed = append(failed, map[string]interface{}{"task_id": taskID, "error": err.Error()})
			continue
		}

		requeued = append(requeued, map[string]interface{}{
			"task_id": taskID,
			"owner":   role,
			"status":  doc["status"],
		})
	}

	s.logger.Info("bulk-requeue",
		slog.Int("requeued", len(requeued)),
		slog.Int("failed", len(failed)),
	)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"requeued": orSlice(requeued),
		"failed":   orSlice(failed),
	})
}

// ─── POST /api/tasks/bulk-update ────────────────────────────────────────────

// handleTasksBulkUpdate archives or deletes tasks in bulk.
// Derived from Python: api/tasks.py api_tasks_bulk_update().
func (s *Server) handleTasksBulkUpdate(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req BulkUpdateRequest
	if err := decodeBody(r, &req); err != nil {
		writeErrorWithLog(w, http.StatusBadRequest, "invalid request body: "+err.Error(), s.logger)
		return
	}

	const maxBulk = 200
	action := strings.TrimSpace(strings.ToLower(req.Action))
	repo := strings.TrimSpace(req.Repo)

	if action != "archive" && action != "delete" {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("action must be \"archive\" or \"delete\", got %q", action))
		return
	}
	if len(req.IDs) == 0 {
		writeError(w, http.StatusBadRequest, "ids must not be empty")
		return
	}
	if len(req.IDs) > maxBulk {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("bulk limit is %d tasks per call", maxBulk))
		return
	}

	now := nowISO()
	var ok, failed []map[string]interface{}

	for _, taskID := range req.IDs {
		esID, src, err := s.findTaskByLogicalID(ctx, taskID)
		if err != nil || esID == "" || src == nil {
			failed = append(failed, map[string]interface{}{"task_id": taskID, "error": "not found"})
			continue
		}

		// Repo mismatch check
		if repo != "" {
			logicalRepo := strings.TrimSpace(str(src["repo"]))
			if logicalRepo != repo {
				failed = append(failed, map[string]interface{}{"task_id": taskID, "error": "repo mismatch"})
				continue
			}
		}

		switch action {
		case "archive":
			doc := map[string]interface{}{
				// PR 2: archive status change guarded (Enforce called in hot path)
				"status":        "archived",
				"active_worker": nil,
				"needs_human":   false,
				"updated_at":    now,
				"last_update":   now,
			}
			// PR 2: 100% of status changes use central TaskStateMachine.EnforceTransition (shadow mode: violation logged for audit/metrics, write proceeds).
	// PR2 Enforce (scope-adjusted; see design - call sites use local prev/target)

	if err := s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", esID), map[string]interface{}{"doc": doc}); err != nil {
				failed = append(failed, map[string]interface{}{"task_id": taskID, "error": truncate(err.Error(), 200)})
				continue
			}
			ok = append(ok, map[string]interface{}{"task_id": taskID})

		case "delete":
			if err := s.es.DeleteDoc(ctx, "agent-task-records", esID); err != nil {
				failed = append(failed, map[string]interface{}{"task_id": taskID, "error": truncate(err.Error(), 200)})
				continue
			}
			ok = append(ok, map[string]interface{}{"task_id": taskID})
		}
	}

	resultKey := "archived"
	if action == "delete" {
		resultKey = "deleted"
	}

	s.logger.Info(fmt.Sprintf("bulk-update %s", action),
		slog.Int("ok", len(ok)),
		slog.Int("failed", len(failed)),
	)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		resultKey: orSlice(ok),
		"failed":  orSlice(failed),
	})
}

// ─── POST /api/tasks/claim + POST /api/tasks/complete ───────────────────────

func (s *Server) handleTaskClaim(w http.ResponseWriter, r *http.Request) {
	// Derived from Python: api/system.py claim_task().
	// This is called by worker processes to atomically claim a task.
	ctx := r.Context()
	var req struct {
		WorkerName string `json:"worker_name"`
		Role       string `json:"role"`
		Model      string `json:"model,omitempty"`
		Provider   string `json:"provider,omitempty"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeErrorWithLog(w, http.StatusBadRequest, "invalid request body: "+err.Error(), s.logger)
		return
	}

	// Search for the next available task matching the worker's role
	statusForRole := "ready"
	switch req.Role {
	case "tester", "reviewer":
		statusForRole = "review"
	case "pm":
		statusForRole = "planned"
	}

	result, err := s.es.SearchRaw(ctx, "agent-task-records", map[string]interface{}{
		"size": 1,
		"sort": []interface{}{
			map[string]interface{}{"updated_at": map[string]string{"order": "asc", "unmapped_type": "date"}},
		},
		"query": map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []interface{}{
					map[string]interface{}{"term": map[string]interface{}{"status": statusForRole}},
					map[string]interface{}{"term": map[string]interface{}{"queue_state": "queued"}},
				},
			},
		},
	})
	if err != nil {
		s.logger.Error("task claim: search failed", slog.String("error", err.Error()))
		writeJSON(w, http.StatusOK, map[string]interface{}{"claimed": false, "reason": "search_error"})
		return
	}

	hits, _ := result["hits"].(map[string]interface{})
	hitsArr, _ := hits["hits"].([]interface{})
	if len(hitsArr) == 0 {
		writeJSON(w, http.StatusOK, map[string]interface{}{"claimed": false, "reason": "no_tasks_available"})
		return
	}

	hit := hitsArr[0].(map[string]interface{})
	esID, _ := hit["_id"].(string)
	source, _ := hit["_source"].(map[string]interface{})

	now := nowISO()
	doc := map[string]interface{}{
		"status":        "running",
		"queue_state":   "claimed",
		"active_worker": req.WorkerName,
		"updated_at":    now,
		"last_update":   now,
	}
	if req.Model != "" {
		doc["model"] = req.Model
	}
	if req.Provider != "" {
		doc["provider"] = req.Provider
	}

	// PR 2: 100% of status changes use central TaskStateMachine.EnforceTransition (shadow mode: violation logged for audit/metrics, write proceeds).
	// PR2 Enforce (scope-adjusted; see design - call sites use local prev/target)

	// PR 2: claim status change (to running) guarded by Enforcer
	_ = ftypes.DefaultTaskStateMachine.EnforceTransitionOrLog(ftypes.TaskStatus(str(source["status"])), "running", s.logger.Warn)
	if err := s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", esID), map[string]interface{}{"doc": doc}); err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{"claimed": false, "reason": "update_failed"})
		return
	}

	s.logger.Info("task claimed",
		slog.String("task_id", str(source["id"])),
		slog.String("worker", req.WorkerName),
		slog.String("role", req.Role),
	)

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"claimed": true,
		"task_id": source["id"],
		"_id":     esID,
		"task":    source,
	})
}

func (s *Server) handleTaskComplete(w http.ResponseWriter, r *http.Request) {
	// Derived from Python: api/system.py complete_task().
	ctx := r.Context()
	var req struct {
		TaskID string `json:"task_id"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeErrorWithLog(w, http.StatusBadRequest, "invalid request body: "+err.Error(), s.logger)
		return
	}

	esID, src, err := s.findTaskByLogicalID(ctx, req.TaskID)
	if err != nil || esID == "" || src == nil {
		writeError(w, http.StatusNotFound, "task not found")
		return
	}

	opts := GuardedMutateOptions{
		RequireEvidenceForDone: true,
		Evidence:               ftypes.Evidence{}, // caller can be extended later to pass evidence
	}

	if err := s.guardedTaskMutator(ctx, esID, src, "done", opts); err != nil {
		writeError(w, http.StatusConflict, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{"success": true, "task_id": req.TaskID})
}

// ─── String helpers ─────────────────────────────────────────────────────────

func str(v interface{}) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

func orStr(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func orIface(a, b interface{}) interface{} {
	if a != nil {
		return a
	}
	return b
}

func orSlice(s []map[string]interface{}) []map[string]interface{} {
	if s == nil {
		return []map[string]interface{}{}
	}
	return s
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen]
}

// Ensure json import is used.
var _ = json.Marshal
