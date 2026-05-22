// api_intake.go — Intake session management: create, get, message, commit.
//
// Direct port of Python: src/dashboard/api/intake.py (4 AST nodes).
// The intake system is an LLM-powered conversational interface for
// breaking down user requests into structured tasks.
package dashboard

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"

	"github.com/Fremen-Labs/flume/internal/llm"
)

const (
	planSessionsIndex = "agent-plan-sessions"
	taskRecordsIndex  = "agent-task-records"
)

const plannerSystemPrompt = `You are a senior technical planner. The user describes what they want built and you break it down into a structured hierarchy of Epics, Features, Stories, and Tasks.

RULES:
- Always respond with valid JSON containing exactly two keys: "message" and "plan".
- "message" is your conversational reply to the user (markdown is fine).
- "plan" is the current complete work breakdown with this exact structure:
  {
    "complexityScore": <1-10>,
    "epics": [
      {
        "id": "epic-<n>",
        "title": "...",
        "description": "...",
        "features": [
          {
            "id": "feat-<n>",
            "title": "...",
            "stories": [
              {
                "id": "story-<n>",
                "title": "...",
                "acceptanceCriteria": ["..."],
                "tasks": [
                  { "id": "task-<n>", "title": "..." }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
- When the user asks to add, remove, or modify items, return the full updated plan.
- Use short, descriptive IDs (epic-1, feat-1, story-1, task-1, etc.).
- Only output the JSON object, nothing before or after it.

COMPLEXITY-PROPORTIONAL PLANNING (critical):
- Match task granularity to ACTUAL complexity. Do NOT over-decompose simple work.
- TRIVIAL changes (update a URL, fix a typo, change a config value, swap a constant):
  produce 1-2 tasks MAXIMUM. One task for the change, optionally one for verification.
- SINGLE-COMPONENT changes (add a feature to one module, update one API endpoint):
  produce 3-5 tasks.
- CROSS-CUTTING changes (new API + UI + database + tests): use full decomposition.
- NEVER create separate tasks for "locate the file" and "make the change" — the
  implementer agent has AST search and file-read tools built in.
- NEVER create a task that assumes an artifact exists without evidence (e.g.,
  "replace the SVG icon" when no SVG was mentioned by the user).
- Combine all verification steps (lint, test, visual check) into ONE task unless
  the project has distinct test suites requiring separate execution.
- A single-file edit should NEVER produce more than 3 tasks total.`

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

	// Fetch updated session
	src, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || src == nil {
		writeError(w, http.StatusNotFound, "session not found")
		return
	}
	var session map[string]interface{}
	_ = unmarshalRaw(src, &session)

	// Build message history
	var messages []llm.Message
	messages = append(messages, llm.Message{
		Role:    "system",
		Content: plannerSystemPrompt,
	})

	sessMsgs, _ := session["messages"].([]interface{})
	for _, mObj := range sessMsgs {
		m, ok := mObj.(map[string]interface{})
		if !ok {
			continue
		}
		role, _ := m["role"].(string)
		content, _ := m["content"].(string)
		messages = append(messages, llm.Message{
			Role:    role,
			Content: content,
		})
	}

	// Generate LLM response
	resp, err := s.llmClient.Chat(ctx, llm.ChatRequest{
		Messages:    messages,
		Temperature: 0.3,
		MaxTokens:   8192,
		AgentRole:   "intake",
	})

	assistantMsg := "Failed to generate plan."
	var plan interface{}

	if err == nil && resp != nil {
		assistantMsg, plan = parseLLMResponse(resp.Content)
	} else {
		// Fallback placeholder plan if LLM failed
		var title string
		if len(req.Message) > 80 {
			title = req.Message[:77] + "..."
		} else {
			title = req.Message
		}
		plan = map[string]interface{}{
			"complexityScore": 1,
			"epics": []interface{}{
				map[string]interface{}{
					"id":          "epic-1",
					"title":       title,
					"description": req.Message,
					"features": []interface{}{
						map[string]interface{}{
							"id":    "feat-1",
							"title": "[Placeholder] Rename this feature",
							"stories": []interface{}{
								map[string]interface{}{
									"id": "story-1",
									"title": "[Placeholder] Rename this story",
									"acceptanceCriteria": []interface{}{
										"[Placeholder] Add acceptance criteria",
									},
									"tasks": []interface{}{
										map[string]interface{}{
											"id":    "task-1",
											"title": "[Placeholder] Add a concrete task",
										},
									},
								},
							},
						},
					},
				},
			},
		}
		assistantMsg = "I encountered an issue processing your request via the LLM. Below is an editable placeholder template."
		if err != nil {
			s.logger.Warn("Intake LLM chat generation failed, using placeholder", slog.String("error", err.Error()))
		}
	}

	// Save assistant response and plan back to session
	assistantTime := nowISO()
	if err := s.es.Post(ctx, fmt.Sprintf("%s/_update/%s", planSessionsIndex, sessionID), map[string]interface{}{
		"script": map[string]interface{}{
			"source": "ctx._source.messages.add(params.msg); ctx._source.draftPlan = params.plan; ctx._source.updated_at = params.ts;",
			"lang":   "painless",
			"params": map[string]interface{}{
				"msg": map[string]interface{}{
					"role":      "assistant",
					"content":   assistantMsg,
					"timestamp": assistantTime,
				},
				"plan": plan,
				"ts":   assistantTime,
			},
		},
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to save assistant response")
		return
	}

	// Fetch final session state and return to client
	finalSrc, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || finalSrc == nil {
		writeError(w, http.StatusInternalServerError, "failed to load final session state")
		return
	}
	var finalSession map[string]interface{}
	_ = unmarshalRaw(finalSrc, &finalSession)
	writeJSON(w, http.StatusOK, finalSession)
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

// ─── Helpers ────────────────────────────────────────────────────────────────

var (
	thinkRegexp = regexp.MustCompile(`(?s)<think>.*?</think>`)
	fenceRegexp = regexp.MustCompile(`(?s)^\x60\x60\x60(?:json)?\s*(.*?)\s*\x60\x60\x60$`)
	jsonRegexp  = regexp.MustCompile(`(?s)\{.*?\}`)
)

func parseLLMResponse(raw string) (string, interface{}) {
	cleaned := strings.TrimSpace(raw)
	// Strip <think> reasoning blocks
	cleaned = thinkRegexp.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(cleaned)

	// Unwrap outer markdown fence
	if strings.HasPrefix(cleaned, "```") {
		subMatches := fenceRegexp.FindStringSubmatch(cleaned)
		if len(subMatches) > 1 {
			cleaned = strings.TrimSpace(subMatches[1])
		} else {
			cleaned = strings.TrimPrefix(cleaned, "```")
			cleaned = strings.TrimSuffix(cleaned, "```")
			cleaned = strings.TrimSpace(cleaned)
		}
	}

	// Try parsing direct JSON
	var obj map[string]interface{}
	if err := json.Unmarshal([]byte(cleaned), &obj); err == nil {
		if msg, ok := obj["message"].(string); ok {
			plan := obj["plan"]
			return msg, plan
		}
	}

	// Try regex extraction of JSON object
	match := jsonRegexp.FindString(cleaned)
	if match != "" {
		var obj map[string]interface{}
		if err := json.Unmarshal([]byte(match), &obj); err == nil {
			if msg, ok := obj["message"].(string); ok {
				plan := obj["plan"]
				return msg, plan
			}
		}
	}

	return cleaned, nil
}
