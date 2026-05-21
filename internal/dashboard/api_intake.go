// api_intake.go — Intake session management: create, get, message, commit.
//
// Direct port of Python: src/dashboard/api/intake.py (4 AST nodes).
// The intake system is an LLM-powered conversational interface for
// breaking down user requests into structured tasks.
package dashboard

import (
	"fmt"
	"log/slog"
	"net/http"
)

const (
	planSessionsIndex = "agent-plan-sessions"
	taskRecordsIndex  = "agent-task-records"
)

// ─── POST /api/intake/session ───────────────────────────────────────────────

// handleIntakeStartSession creates a new plan session.
// Derived from Python: api/intake.py api_intake_start_session().
func (s *Server) handleIntakeStartSession(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req struct {
		ProjectID string `json:"project_id"`
		Title     string `json:"title,omitempty"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.ProjectID == "" {
		writeError(w, http.StatusBadRequest, "project_id is required")
		return
	}

	sessionID := fmt.Sprintf("session-%d", timeNowUnixMilli())
	now := nowISO()

	doc := map[string]interface{}{
		"id":         sessionID,
		"project_id": req.ProjectID,
		"title":      orStr(req.Title, "New Intake Session"),
		"messages":   []interface{}{},
		"status":     "active",
		"created_at": now,
		"updated_at": now,
	}

	if err := s.es.IndexDoc(ctx, planSessionsIndex, sessionID, doc); err != nil {
		s.logger.Error("intake session create failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to create session")
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":    true,
		"session_id": sessionID,
		"session":    doc,
	})
}

// ─── GET /api/intake/session/{session_id} ───────────────────────────────────

func (s *Server) handleIntakeGetSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	ctx := r.Context()

	src, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || src == nil {
		writeError(w, http.StatusNotFound, "session not found")
		return
	}

	var session map[string]interface{}
	_ = unmarshalRaw(src, &session)
	writeJSON(w, http.StatusOK, session)
}

// ─── POST /api/intake/session/{session_id}/message ──────────────────────────

// handleIntakeMessage appends a user message and generates an LLM response.
// Derived from Python: api/intake.py api_intake_message().
func (s *Server) handleIntakeMessage(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	ctx := r.Context()

	var req struct {
		Message string `json:"message"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.Message == "" {
		writeError(w, http.StatusBadRequest, "message is required")
		return
	}

	now := nowISO()

	// Append the user message to the session via Painless script
	if err := s.es.Post(ctx, fmt.Sprintf("%s/_update/%s", planSessionsIndex, sessionID), map[string]interface{}{
		"script": map[string]interface{}{
			"source": "ctx._source.messages.add(params.msg); ctx._source.updated_at = params.ts;",
			"lang":   "painless",
			"params": map[string]interface{}{
				"msg": map[string]interface{}{
					"role":      "user",
					"content":   req.Message,
					"timestamp": now,
				},
				"ts": now,
			},
		},
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to append message")
		return
	}

	// TODO: Generate LLM response via internal/llm client (Phase 3).
	// For now, return acknowledgment.
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":    true,
		"session_id": sessionID,
		"message":    "Message received. LLM response generation pending Go LLM client implementation.",
	})
}

// ─── POST /api/intake/session/{session_id}/commit ───────────────────────────

// handleIntakeCommit commits the session's planned tasks to the task queue.
// Derived from Python: api/intake.py api_intake_commit().
func (s *Server) handleIntakeCommit(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	ctx := r.Context()

	var req struct {
		Tasks []map[string]interface{} `json:"tasks"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if len(req.Tasks) == 0 {
		writeError(w, http.StatusBadRequest, "tasks list is required")
		return
	}

	now := nowISO()
	var created []map[string]interface{}

	for _, task := range req.Tasks {
		taskID, _ := task["id"].(string)
		if taskID == "" {
			taskID = fmt.Sprintf("task-%d", timeNowUnixMilli())
		}

		doc := map[string]interface{}{
			"id":          taskID,
			"title":       task["title"],
			"description": task["description"],
			"status":      "inbox",
			"queue_state": "queued",
			"repo":        task["project_id"],
			"created_at":  now,
			"updated_at":  now,
		}
		if priority, ok := task["priority"].(string); ok {
			doc["priority"] = priority
		}

		if err := s.es.IndexDoc(ctx, taskRecordsIndex, taskID, doc); err != nil {
			s.logger.Error("intake commit: task create failed",
				slog.String("task_id", taskID),
				slog.String("error", err.Error()),
			)
			continue
		}
		created = append(created, map[string]interface{}{"id": taskID, "title": task["title"]})
	}

	// Mark session as committed
	_ = s.es.Post(ctx, fmt.Sprintf("%s/_update/%s", planSessionsIndex, sessionID), map[string]interface{}{
		"doc": map[string]interface{}{"status": "committed", "updated_at": now},
	})

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"created": created,
		"count":   len(created),
	})
}
