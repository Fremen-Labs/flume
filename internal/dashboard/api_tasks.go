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
	"strings"
	"time"
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
// Derived from Python: _async_append_task_agent_log_note() in api/tasks.py.
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

	// TODO: Implement git diff via exec.Command (local) or GitHostClient (remote).
	// Phase 2 stub — the diff endpoint requires git subprocess integration
	// which will be wired during Phase 3 (git client port).
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"diff":   "",
		"branch": branch,
		"error":  "diff not yet implemented in Go dashboard — use Python endpoint",
	})
}

// ─── GET /api/tasks/{task_id}/commits ───────────────────────────────────────

func (s *Server) handleTaskCommits(w http.ResponseWriter, r *http.Request) {
	// TODO: Implement via GitHostClient (Phase 3).
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
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	allowedStatuses := map[string]bool{"ready": true, "planned": true, "inbox": true}
	status := strings.TrimSpace(strings.ToLower(req.Status))
	if !allowedStatuses[status] {
		writeError(w, http.StatusBadRequest,
			fmt.Sprintf("status must be one of [inbox, planned, ready], got %q", status))
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
	autoRecovery := true
	if req.AutoRecoveryPrompt != nil {
		autoRecovery = *req.AutoRecoveryPrompt
	}

	if instruction != "" {
		_ = s.appendTaskAgentLogNote(ctx, esID, fmt.Sprintf("[Human guidance] %s", instruction))
	} else if status == "ready" && prevStatus == "blocked" && autoRecovery {
		_ = s.appendTaskAgentLogNote(ctx, esID,
			"[Recovery] Re-queued after blocked. Read prior agent_log and execution_thoughts; "+
				"fix the root cause, run or add tests, and iterate until acceptance criteria are met.")
	}

	now := nowISO()
	owner := orStr(str(src["owner"]), str(src["assigned_agent_role"]))

	doc := map[string]interface{}{
		"status":       status,
		"queue_state":  "queued",
		"active_worker": nil,
		"needs_human":  false,
		"updated_at":   now,
		"last_update":  now,
		"implementer_consecutive_llm_failures": 0,
	}
	if owner != "" {
		doc["owner"] = owner
		doc["assigned_agent_role"] = owner
	}

	if err := s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", esID), map[string]interface{}{"doc": doc}); err != nil {
		s.logger.Error("task transition: ES update failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to update task")
		return
	}

	s.logger.Info("task transition",
		slog.String("task_id", taskID),
		slog.String("status", status),
		slog.String("owner", owner),
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
		writeError(w, http.StatusBadRequest, "invalid request body")
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
			doc["status"] = "planned"
		case "tester", "reviewer":
			doc["status"] = "review"
		default:
			doc["status"] = "ready"
		}
		doc["owner"] = role
		doc["assigned_agent_role"] = role

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
		writeError(w, http.StatusBadRequest, "invalid request body")
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
				"status":        "archived",
				"active_worker": nil,
				"needs_human":   false,
				"updated_at":    now,
				"last_update":   now,
			}
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
		writeError(w, http.StatusBadRequest, "invalid request body")
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
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	esID, _, err := s.findTaskByLogicalID(ctx, req.TaskID)
	if err != nil || esID == "" {
		writeError(w, http.StatusNotFound, "task not found")
		return
	}

	now := nowISO()
	doc := map[string]interface{}{
		"status":        "done",
		"queue_state":   "completed",
		"active_worker": nil,
		"completed_at":  now,
		"updated_at":    now,
		"last_update":   now,
	}

	if err := s.es.Post(ctx, fmt.Sprintf("agent-task-records/_update/%s", esID), map[string]interface{}{"doc": doc}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to complete task")
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
