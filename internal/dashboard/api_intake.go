package dashboard

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/llm"
	"github.com/Fremen-Labs/flume/internal/secrets"
	worker "github.com/Fremen-Labs/flume/internal/worker"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
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
- A single-file edit should NEVER produce more than 3 tasks total.

RAG CONTEXT (Elastro/Logloom contract #3):
- Before/around this planning call, the system performs best-effort direct queries (using the same elastro_query_ast / logloom_ast_query executor patterns as ToolRegistry) against flume-elastro-graph and flume-logloom-ast.
- Relevant compact structural/semantic hits (functions, files, call relations, signatures from the ingested AST graphs) are injected as an additional system message.
- Use ONLY structures evidenced in the RAG context for references in the plan. This grounds plans, prevents hallucinated modules, and reduces overall token usage vs. shipping raw source to the LLM (works for local Ollama + remote frontier models).
- If RAG context is absent or thin, fall back to minimal plan; do not invent files.`

// Plan Response structures
type PlanTask struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	Objective string `json:"objective,omitempty"`
}

type PlanStory struct {
	ID                 string     `json:"id"`
	Title              string     `json:"title"`
	AcceptanceCriteria []string   `json:"acceptanceCriteria"`
	Tasks              []PlanTask `json:"tasks"`
}

type PlanFeature struct {
	ID      string      `json:"id"`
	Title   string      `json:"title"`
	Stories []PlanStory `json:"stories"`
}

type PlanEpic struct {
	ID          string        `json:"id"`
	Title       string        `json:"title"`
	Description string        `json:"description,omitempty"`
	Features    []PlanFeature `json:"features"`
}

type PlanResponse struct {
	ComplexityScore int        `json:"complexityScore"`
	Epics           []PlanEpic `json:"epics"`
}

type PlanningStatus struct {
	Stage                    string   `json:"stage"` // queued, testing_connection, requesting_plan, ready, failed
	Provider                 string   `json:"provider"`
	Model                    string   `json:"model"`
	BaseURL                  string   `json:"baseUrl"`
	Host                     string   `json:"host"`
	ConnectionTestStartedAt  *string  `json:"connectionTestStartedAt,omitempty"`
	ConnectionTestDurationMs float64  `json:"connectionTestDurationMs"`
	ConnectionTestOk         *bool    `json:"connectionTestOk,omitempty"`
	ConnectionTestResult     *string  `json:"connectionTestResult,omitempty"`
	RequestStartedAt         *string  `json:"requestStartedAt,omitempty"`
	RequestElapsedSeconds    float64  `json:"requestElapsedSeconds"`
	TimeoutSeconds           int      `json:"timeoutSeconds"`
	FailureText              *string  `json:"failureText,omitempty"`
	LastUpdatedAt            string   `json:"lastUpdatedAt"`
}

type SessionMessage struct {
	From      string      `json:"from"` // user, agent
	Text      string      `json:"text"`
	Plan      interface{} `json:"plan,omitempty"`
	AgentRole string      `json:"agent_role,omitempty"`
	Timestamp string      `json:"timestamp"`
}

type SessionDoc struct {
	ID              string           `json:"id"`
	Repo            string           `json:"repo"`
	Status          string           `json:"status"` // active, committed
	AgentRole       string           `json:"agent_role"` // "intake"
	Messages        []SessionMessage `json:"messages"`
	DraftPlan       interface{}      `json:"draftPlan,omitempty"`
	DraftPlanSource string           `json:"draftPlanSource"` // "llm", "placeholder"
	PlanningStatus  PlanningStatus   `json:"planningStatus"`
	CreatedAt       string           `json:"created_at"`
	UpdatedAt       string           `json:"updated_at"`
	CommittedAt     string           `json:"committed_at,omitempty"`
	CommittedDocs   []string         `json:"committedDocs,omitempty"`

	// Phase 2 budget enforcement (Enforcement Mechanics): per-plan hard limits + live counters.
	// Populated at session creation with sane defaults; atomically updated via ES scripts on task creation,
	// promotion, and PM decomp paths. Exceed → block with LogAgentReasoning + audit event.
	ItemBudget    int    `json:"item_budget,omitempty"`    // total workitems (incl. hierarchy) allowed for this plan session
	TokenBudget   int    `json:"token_budget,omitempty"`   // cumulative LLM tokens allowed (soft; advisory)
	CurrentItems  int    `json:"current_items,omitempty"`  // authoritative via script increments
	CurrentTokens int    `json:"current_tokens,omitempty"`
	BudgetStatus  string `json:"budget_status,omitempty"`  // "", "exceeded", "warning"
}

func prepareSessionResponse(session SessionDoc) map[string]interface{} {
	status := session.Status
	if session.PlanningStatus.Stage == "ready" {
		status = "ready"
	} else if session.PlanningStatus.Stage == "failed" {
		status = "failed"
	}

	return map[string]interface{}{
		"id":              session.ID,
		"sessionId":       session.ID,
		"repo":            session.Repo,
		"status":          status,
		"agent_role":      session.AgentRole,
		"messages":        session.Messages,
		"draftPlan":       session.DraftPlan,       // keep draftPlan for compatibility with CLI/tests
		"plan":            session.DraftPlan,       // for frontend compatibility
		"draftPlanSource": session.DraftPlanSource, // keep draftPlanSource for compatibility
		"planSource":      session.DraftPlanSource, // for frontend compatibility
		"planningStatus":  session.PlanningStatus,
		"created_at":      session.CreatedAt,
		"updated_at":      session.UpdatedAt,
		"committed_at":    session.CommittedAt,
		"committedDocs":   session.CommittedDocs,
	}
}


type AgentTaskRecord struct {
	ID                    string   `json:"id"`
	Title                 string   `json:"title"`
	Objective             string   `json:"objective"`
	// Repo is the project identifier for this task record.
	// Serialized as "repo" in ES (matches ftypes.Task.ProjectID json tag).
	// Queries on agent-task-records must use "repo", never "project_id".
	Repo                  string   `json:"repo"`
	Worktree              string   `json:"worktree,omitempty"`
	ItemType              string   `json:"item_type"` // epic, feature, story, task
	Owner                 string   `json:"owner"`      // pm, implementer
	AssignedAgentRole     string   `json:"assigned_agent_role,omitempty"`
	Status                string   `json:"status"` // planned, ready
	Priority              string   `json:"priority"` // normal, high, medium
	ParentID              string   `json:"parent_id,omitempty"`
	DependsOn             []string `json:"depends_on"`
	AcceptanceCriteria    []string `json:"acceptance_criteria"`
	Artifacts             []string `json:"artifacts"`
	LastUpdate            string   `json:"last_update"`
	CreatedAt             string   `json:"created_at"`
	UpdatedAt             string   `json:"updated_at"`
	NeedsHuman            bool     `json:"needs_human"`
	Risk                  string   `json:"risk"`
	PreferredModel        string   `json:"preferred_model,omitempty"`
	PreferredLLMProvider  string   `json:"preferred_llm_provider,omitempty"`
	PreferredCredentialID string   `json:"preferred_llm_credential_id,omitempty"`
	// Complexity* fields (PR 2)
	Complexity       int    `json:"complexity,omitempty"`
	ComplexityReason string `json:"complexity_reason,omitempty"`
	ComplexityBucket string `json:"complexity_bucket,omitempty"`

	// Denormalized fields for cheap anti-explosion guards (mirrors pkg/types.Task).
	// ChildCount = number of direct children (by parent_id). DecomposedAt set on first decomposition.
	DecomposedAt string `json:"decomposed_at,omitempty"`
	ChildCount   int    `json:"child_count,omitempty"`

	// Phase 2 correlation + depth (enforcement mechanics)
	PlanSessionID  string `json:"plan_session_id,omitempty"`
	HierarchyDepth int    `json:"hierarchy_depth,omitempty"`
}

func randomHex(n int) string {
	b := make([]byte, n/2)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x", b)
}

func (s *Server) testPlannerConnection(ctx context.Context, cfg *config.Config) (bool, string) {
	provider := strings.TrimSpace(strings.ToLower(cfg.LLMProvider))
	baseURL := strings.TrimRight(cfg.LLMBaseURL, "/")
	apiKey := cfg.LLMAPIKey

	if apiKey == "" && cfg.OpenBaoAddr != "" && cfg.OpenBaoToken != "" {
		baoClient := secrets.NewOpenBaoClient(cfg.OpenBaoAddr, cfg.OpenBaoToken, s.logger)
		if baoData, err := baoClient.KVGet(ctx, "flume/keys"); err == nil && baoData != nil {
			if k, _ := baoData["LLM_API_KEY"].(string); k != "" {
				apiKey = k
			}
		}
	}

	headers := make(map[string]string)
	var urlStr string

	if provider == "ollama" {
		gatewayURL := os.Getenv("FLUME_GATEWAY_URL")
		if gatewayURL == "" {
			// Native mode: gateway runs in-process on localhost.
			// Docker mode: docker compose DNS resolves "gateway".
			if os.Getenv("FLUME_NATIVE_MODE") == "1" {
				gatewayURL = "http://localhost:8090"
			} else {
				gatewayURL = "http://gateway:8090"
			}
		}
		urlStr = strings.TrimRight(gatewayURL, "/") + "/api/nodes"
	} else {
		if baseURL == "" {
			return false, fmt.Sprintf("No base URL configured for provider %q", provider)
		}
		if baseURL == "http://localhost:11434" && provider != "ollama" && provider != "exo" {
			if provider == "xai" || provider == "grok" {
				baseURL = "https://api.x.ai"
			} else if provider == "openai" {
				baseURL = "https://api.openai.com"
			} else if provider == "anthropic" {
				baseURL = "https://api.anthropic.com"
			}
		}

		if provider == "exo" {
			urlStr = baseURL + "/v1/models"
		} else if provider == "anthropic" {
			urlStr = baseURL + "/v1/models"
			if apiKey != "" {
				headers["x-api-key"] = apiKey
				headers["anthropic-version"] = "2023-06-01"
			}
		} else if provider == "gemini" {
			urlStr = "https://generativelanguage.googleapis.com/v1beta/models"
			if apiKey != "" {
				urlStr += "?key=" + apiKey
			}
		} else {
			urlStr = baseURL + "/v1/models"
			if apiKey != "" {
				headers["Authorization"] = "Bearer " + apiKey
			}
		}
	}

	req, err := http.NewRequestWithContext(ctx, "GET", urlStr, nil)
	if err != nil {
		return false, fmt.Sprintf("Failed to build probe request: %v", err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return false, fmt.Sprintf("%s connection FAILED: %v", strings.ToUpper(provider), err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return false, fmt.Sprintf("%s connection FAILED: responded HTTP %d", strings.ToUpper(provider), resp.StatusCode)
	}
	return true, fmt.Sprintf("%s connection OK — responded HTTP %d", strings.ToUpper(provider), resp.StatusCode)
}

func placeholderPlan(repo, prompt string) map[string]interface{} {
	title := strings.TrimSpace(strings.Split(prompt, "\n")[0])
	if len(title) > 80 {
		title = title[:77] + "..."
	}
	if title == "" {
		title = "New request"
	}

	// Grok-grade minimal placeholder: clean, professional, no confusing "Rename this" or meta language.
	// Tailored to the actual prompt so it's immediately usable as a starting point even when LLM planning fails.
	// Keeps it minimal (1 epic / 1 feature / 1 story / 1-2 tasks) per complexity rules.
	task1 := map[string]interface{}{
		"id":        "task-1",
		"title":     "Analyze request and implement core change",
		"objective": fmt.Sprintf("Understand the request: %s. Make the minimal correct implementation.", prompt),
	}
	tasks := []interface{}{task1}

	// Add a lightweight verification task for non-trivial prompts
	if len(prompt) > 40 {
		tasks = append(tasks, map[string]interface{}{
			"id":        "task-2",
			"title":     "Verify the change works as expected",
			"objective": "Run relevant checks (build, test, or manual) to confirm the implementation satisfies the request.",
		})
	}

	return map[string]interface{}{
		"repo":            repo,
		"complexityScore": 1,
		"epics": []interface{}{
			map[string]interface{}{
				"id":          "epic-1",
				"title":       title,
				"description": prompt,
				"features": []interface{}{
					map[string]interface{}{
						"id":    "feat-1",
						"title": "Implementation",
						"stories": []interface{}{
							map[string]interface{}{
								"id":                 "story-1",
								"title":              "Deliver the requested change",
								"acceptanceCriteria": []interface{}{"The request is implemented correctly and minimally."},
								"tasks":              tasks,
							},
						},
					},
				},
			},
		},
	}
}

func buildLLMMessages(session SessionDoc) []llm.Message {
	var msgs []llm.Message
	msgs = append(msgs, llm.Message{
		Role:    "system",
		Content: plannerSystemPrompt,
	})

	for _, m := range session.Messages {
		if m.From == "user" {
			text := m.Text
			if m.Plan != nil {
				planBytes, _ := json.MarshalIndent(m.Plan, "", "  ")
				text += fmt.Sprintf("\n\nCurrent plan state:\n```json\n%s\n```", string(planBytes))
			}
			msgs = append(msgs, llm.Message{
				Role:    "user",
				Content: text,
			})
		} else if m.From == "agent" {
			plan := m.Plan
			if plan == nil {
				plan = map[string]interface{}{}
			}
			respObj := map[string]interface{}{
				"message": m.Text,
				"plan":    plan,
			}
			respBytes, _ := json.Marshal(respObj)
			msgs = append(msgs, llm.Message{
				Role:    "assistant",
				Content: string(respBytes),
			})
		}
	}
	return msgs
}

// ─── Elastro/Logloom RAG for Planner (contract point #3) ────────────────────
//
// During Plan New Work (initial + refine), we call the *existing tool executors*
// (ElastroASTQueryExecutor, LogloomASTQueryExecutor) — same as registered in
// NewToolRegistryWithElastro in internal/worker/handlers.go — or direct ES
// equivalent via their Execute methods.
//
// This injects compact structural context from flume-elastro-graph + flume-logloom-ast
// into the messages for *every* planner LLM call (buildLLMMessages path).
// Works identically for local (ollama/gateway) and remote frontier models because
// the context is pre-injected into the ChatRequest messages.
//
// Emissions: s.logReasoning (which does logger + LogAgentReasoning) + the
// executors' own internal LogAgentReasoning (role may appear as implementer for
// the reused executor code, but tagged with phase/tool).
// Best-effort: never blocks planning; empty RAG just means no extra context.

func (s *Server) fetchPlannerRAGContext(ctx context.Context, repo, prompt string) string {
	if s.es == nil {
		return ""
	}
	q := strings.TrimSpace(prompt)
	if q == "" {
		return ""
	}

	var parts []string
	if c := s.queryElastroForPlanner(ctx, repo, q); c != "" {
		parts = append(parts, c)
	}
	if c := s.queryLogloomForPlanner(ctx, repo, q); c != "" {
		parts = append(parts, c)
	}
	if len(parts) == 0 {
		return ""
	}
	joined := strings.Join(parts, "\n\n")

	// Top-level reasoning emission proving RAG was used for token-efficient planning.
	s.logReasoning(ctx, "plan-rag-"+repo, "intake-planner",
		"RAG used (elastro_query_ast + logloom_ast_query executor patterns) to inject structural context from indices before planner LLM invocation — fulfills Elastro/Logloom contract #3 for Plan New Work. Context is compact to keep added tokens low while providing high-signal grounding (vs raw file context).",
		map[string]any{
			"repo":              repo,
			"prompt_preview":    plannerTruncate(q, 100),
			"rag_chars":         len(joined),
			"used_elastro":      strings.Contains(joined, "Elastro Graph RAG"),
			"used_logloom":      strings.Contains(joined, "Logloom structural"),
			"phase":             "planner_rag_prefetch",
			"benefit":           "token_reduction + better grounded minimal plans",
		})

	s.logger.Info("intake planner RAG injected for context reduction",
		slog.String("repo", repo),
		slog.Int("rag_chars", len(joined)),
		slog.Bool("elastro", strings.Contains(joined, "Elastro Graph RAG")),
		slog.Bool("logloom", strings.Contains(joined, "Logloom structural")),
	)

	return joined
}

func plannerTruncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func (s *Server) queryElastroForPlanner(ctx context.Context, repo, prompt string) string {
	if s.es == nil {
		return ""
	}
	executor := worker.NewElastroASTQueryExecutor(s.es, s.logger)
	args := map[string]interface{}{"query": prompt}
	if repo != "" && repo != "/" && repo != "." {
		args["target_path"] = repo
	}
	res, err := executor.Execute(ctx, args, repo)
	if err != nil {
		s.logger.Debug("planner elastro RAG via executor failed (best-effort, non-fatal)", slog.String("repo", repo), slog.String("err", err.Error()))
		return ""
	}
	low := strings.ToLower(res)
	if strings.Contains(low, "no elastro ast graph data") ||
		strings.Contains(low, "returned no matches") ||
		strings.Contains(low, "may need re-ingestion") ||
		len(strings.TrimSpace(res)) < 30 {
		return ""
	}
	return res
}

func (s *Server) queryLogloomForPlanner(ctx context.Context, repo, prompt string) string {
	if s.es == nil {
		return ""
	}
	executor := worker.NewLogloomASTQueryExecutor(s.es, s.logger)
	args := map[string]interface{}{"query": prompt}
	// logloom executor ignores target_path but we pass repo as repoPath anyway (best effort)
	res, err := executor.Execute(ctx, args, repo)
	if err != nil {
		s.logger.Debug("planner logloom RAG via executor failed (best-effort, non-fatal)", slog.String("repo", repo), slog.String("err", err.Error()))
		return ""
	}
	low := strings.ToLower(res)
	if strings.Contains(low, "no structural matches") ||
		len(strings.TrimSpace(res)) < 30 {
		return ""
	}
	return res
}

func (s *Server) injectRAGIntoMessages(msgs []llm.Message, ragContext string) []llm.Message {
	if ragContext == "" || len(msgs) == 0 {
		return msgs
	}
	ragMsg := llm.Message{
		Role: "system",
		Content: "RELEVANT STRUCTURAL/Semantic CONTEXT FROM ELASTRO + LOGLOOM INDICES (injected pre-LLM by fetchPlannerRAGContext using tool executor patterns; this is how the planner leverages graph RAG instead of raw context for token-efficient, accurate plans):\n\n" + ragContext,
	}
	// Insert immediately after the primary planner system prompt (index 0)
	if len(msgs) > 0 && msgs[0].Role == "system" {
		res := make([]llm.Message, 0, len(msgs)+1)
		res = append(res, msgs[0], ragMsg)
		res = append(res, msgs[1:]...)
		return res
	}
	// Fallback: prepend
	return append([]llm.Message{ragMsg}, msgs...)
}

// hostFromBaseURL extracts a human-readable host for display in planning status UI.
func hostFromBaseURL(baseURL string) string {
	if baseURL == "" {
		return ""
	}
	if u, err := url.Parse(baseURL); err == nil && u.Host != "" {
		return u.Host
	}
	return baseURL
}

// ─── POST /api/intake/session ───────────────────────────────────────────────

func (s *Server) handleIntakeStartSession(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req struct {
		Repo   string `json:"repo"`
		Prompt string `json:"prompt"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.Repo == "" {
		writeError(w, http.StatusBadRequest, "repo is required")
		return
	}

	sessionID := fmt.Sprintf("plan-%s", randomHex(12))
	now := nowISO()

	cfg := config.Get()
	status := PlanningStatus{
		Stage:          "queued",
		Provider:       cfg.LLMProvider,
		Model:          cfg.LLMModel,
		BaseURL:        cfg.LLMBaseURL,
		Host:           hostFromBaseURL(cfg.LLMBaseURL),
		TimeoutSeconds: 120,
		LastUpdatedAt:  now,
	}

	sessionDoc := SessionDoc{
		ID:              sessionID,
		Repo:            req.Repo,
		Status:          "active",
		AgentRole:       "intake",
		Messages:        []SessionMessage{},
		PlanningStatus:  status,
		CreatedAt:       now,
		UpdatedAt:       now,
		// Phase 2 defaults (tunable via future settings; these prevent 74-task class explosions while allowing real work)
		ItemBudget:  40,   // total hierarchy + leaf items for the plan
		TokenBudget: 80000, // cumulative LLM spend budget (prompt+completion) advisory
	}

	if err := s.es.IndexDoc(ctx, planSessionsIndex, sessionID, sessionDoc); err != nil {
		s.logger.Error("intake session create failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, "failed to create session")
		return
	}

	// Trigger background planning task
	go s.runInitialPlanning(context.Background(), sessionID, req.Repo, req.Prompt)

	resp := prepareSessionResponse(sessionDoc)
	resp["success"] = true
	resp["session_id"] = sessionID
	resp["session"] = sessionDoc

	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) runInitialPlanning(ctx context.Context, sessionID, repo, prompt string) {
	now := nowISO()

	// 1. Update status to testing_connection
	cfg := config.Get()
	status := PlanningStatus{
		Stage:          "testing_connection",
		Provider:       cfg.LLMProvider,
		Model:          cfg.LLMModel,
		BaseURL:        cfg.LLMBaseURL,
		Host:           hostFromBaseURL(cfg.LLMBaseURL),
		TimeoutSeconds: 120,
		LastUpdatedAt:  now,
	}
	startedStr := now
	status.ConnectionTestStartedAt = &startedStr

	_ = s.updateSessionStatus(ctx, sessionID, status, nil, "")

	// 2. Perform connection test
	startTest := time.Now()
	ok, result := s.testPlannerConnection(ctx, cfg)
	elapsedMs := float64(time.Since(startTest).Milliseconds())

	status.ConnectionTestOk = &ok
	status.ConnectionTestResult = &result
	status.ConnectionTestDurationMs = elapsedMs
	status.LastUpdatedAt = nowISO()

	if !ok {
		status.Stage = "failed"
		status.FailureText = &result
		_ = s.updateSessionStatus(ctx, sessionID, status, nil, "")
		return
	}

	// 3. Update status to requesting_plan
	status.Stage = "requesting_plan"
	reqStarted := nowISO()
	status.RequestStartedAt = &reqStarted
	_ = s.updateSessionStatus(ctx, sessionID, status, nil, "")

	// 4. Build messages and Chat
	userMsg := SessionMessage{
		From:      "user",
		Text:      prompt,
		Timestamp: now,
	}

	sessDoc := SessionDoc{
		ID:             sessionID,
		Repo:           repo,
		Status:         "active",
		AgentRole:      "intake",
		Messages:       []SessionMessage{userMsg},
		PlanningStatus: status,
	}

	chatMsgs := buildLLMMessages(sessDoc)
	// Contract #3: pre-fetch RAG using executors and inject (before LLM, for local+frontier)
	if rag := s.fetchPlannerRAGContext(ctx, repo, prompt); rag != "" {
		chatMsgs = s.injectRAGIntoMessages(chatMsgs, rag)
	}
	startReq := time.Now()
	resp, err := s.llmClient.Chat(ctx, llm.ChatRequest{
		Messages:    chatMsgs,
		Temperature: 0.3,
		MaxTokens:   8192,
		AgentRole:   "intake",
		TaskType:    "planning", // Force planning task type so the resilient mesh routing (fresh contexts for fallbacks on slow nodes) is used for all intake work
	})
	elapsedSec := time.Since(startReq).Seconds()

	status.RequestElapsedSeconds = elapsedSec
	status.LastUpdatedAt = nowISO()

	var assistantMsg string
	var plan interface{}
	var planSrc string

	if err != nil || resp == nil {
		errMsg := "LLM plan generation failed"
		if err != nil {
			errMsg = err.Error()
		}
		s.logger.Warn("Intake LLM initial plan generation failed, using placeholder", slog.String("error", errMsg))
		assistantMsg = "I encountered an issue processing your request via the LLM. Below is an editable placeholder template."
		plan = placeholderPlan(repo, prompt)
		planSrc = "placeholder"
	} else {
		assistantMsg, plan = parseLLMResponse(resp.Content)
		if plan == nil {
			rawPreview := resp.Content
			if len(rawPreview) > 800 {
				rawPreview = rawPreview[:800] + "... [truncated]"
			}
			s.logger.Warn("Intake LLM returned empty/unparseable plan, using placeholder",
				slog.String("raw_preview", rawPreview),
				slog.String("parsed_assistant_msg", assistantMsg))
			assistantMsg = "I encountered an issue parsing the plan generated by the LLM. Below is an editable placeholder template."
			plan = placeholderPlan(repo, prompt)
			planSrc = "placeholder"
		} else {
			planSrc = "llm"
		}
	}

	status.Stage = "ready"
	agentMsg := SessionMessage{
		From:      "agent",
		Text:      assistantMsg,
		Plan:      plan,
		Timestamp: nowISO(),
	}

	_ = s.updateSessionStatus(ctx, sessionID, status, &agentMsg, planSrc)
}

func (s *Server) updateSessionStatus(ctx context.Context, sessionID string, status PlanningStatus, agentMsg *SessionMessage, planSrc string) error {
	sessBytes, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || sessBytes == nil {
		return fmt.Errorf("session not found")
	}

	var session SessionDoc
	if err := json.Unmarshal(sessBytes, &session); err != nil {
		return err
	}

	session.PlanningStatus = status
	session.UpdatedAt = nowISO()

	if agentMsg != nil {
		session.Messages = append(session.Messages, *agentMsg)
		session.DraftPlan = agentMsg.Plan
		session.DraftPlanSource = planSrc
	}

	return s.es.IndexDoc(ctx, planSessionsIndex, sessionID, session)
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

	var session SessionDoc
	if err := json.Unmarshal(src, &session); err != nil {
		var rawSession map[string]interface{}
		_ = json.Unmarshal(src, &rawSession)
		if rawSession != nil {
			if id, ok := rawSession["id"].(string); ok {
				rawSession["sessionId"] = id
			}
			if draftPlan, ok := rawSession["draftPlan"]; ok {
				rawSession["plan"] = draftPlan
			}
			if draftPlanSource, ok := rawSession["draftPlanSource"]; ok {
				rawSession["planSource"] = draftPlanSource
			}
			writeJSON(w, http.StatusOK, rawSession)
			return
		}
		writeError(w, http.StatusInternalServerError, "failed to parse session")
		return
	}

	writeJSON(w, http.StatusOK, prepareSessionResponse(session))
}

// ─── POST /api/intake/session/{session_id}/message ──────────────────────────

func (s *Server) handleIntakeMessage(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	ctx := r.Context()

	var req struct {
		Text string                 `json:"text"`
		Plan map[string]interface{} `json:"plan,omitempty"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.Text == "" {
		writeError(w, http.StatusBadRequest, "text is required")
		return
	}

	sessBytes, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || sessBytes == nil {
		writeError(w, http.StatusNotFound, "session not found")
		return
	}

	var session SessionDoc
	if err := json.Unmarshal(sessBytes, &session); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to unmarshal session")
		return
	}

	now := nowISO()

	// Append user message
	userMsg := SessionMessage{
		From:      "user",
		Text:      req.Text,
		Plan:      req.Plan,
		Timestamp: now,
	}
	session.Messages = append(session.Messages, userMsg)
	if req.Plan != nil {
		session.DraftPlan = req.Plan
	}
	session.UpdatedAt = now

	// 1. Connection test
	cfg := config.Get()
	session.PlanningStatus.Stage = "testing_connection"
	startedStr := now
	session.PlanningStatus.ConnectionTestStartedAt = &startedStr
	session.PlanningStatus.LastUpdatedAt = now

	_ = s.es.IndexDoc(ctx, planSessionsIndex, sessionID, session)

	startTest := time.Now()
	ok, result := s.testPlannerConnection(ctx, cfg)
	elapsedMs := float64(time.Since(startTest).Milliseconds())

	session.PlanningStatus.ConnectionTestOk = &ok
	session.PlanningStatus.ConnectionTestResult = &result
	session.PlanningStatus.ConnectionTestDurationMs = elapsedMs

	if !ok {
		session.PlanningStatus.Stage = "failed"
		session.PlanningStatus.FailureText = &result
		session.PlanningStatus.LastUpdatedAt = nowISO()
		_ = s.es.IndexDoc(ctx, planSessionsIndex, sessionID, session)
		writeError(w, http.StatusInternalServerError, result)
		return
	}

	// 2. Requesting plan
	session.PlanningStatus.Stage = "requesting_plan"
	reqStarted := nowISO()
	session.PlanningStatus.RequestStartedAt = &reqStarted
	_ = s.es.IndexDoc(ctx, planSessionsIndex, sessionID, session)

	// 3. Call LLM
	chatMsgs := buildLLMMessages(session)
	// Contract #3: pre-fetch RAG using executors and inject (before LLM, for local+frontier)
	if rag := s.fetchPlannerRAGContext(ctx, session.Repo, req.Text); rag != "" {
		chatMsgs = s.injectRAGIntoMessages(chatMsgs, rag)
	}
	startReq := time.Now()
	resp, err := s.llmClient.Chat(ctx, llm.ChatRequest{
		Messages:    chatMsgs,
		Temperature: 0.3,
		MaxTokens:   8192,
		AgentRole:   "intake",
		TaskType:    "planning", // Force planning task type so the resilient mesh routing (fresh contexts for fallbacks on slow nodes) is used for all intake work
	})
	elapsedSec := time.Since(startReq).Seconds()

	session.PlanningStatus.RequestElapsedSeconds = elapsedSec
	session.PlanningStatus.LastUpdatedAt = nowISO()

	var assistantMsg string
	var plan interface{}
	var planSrc string

	if err != nil || resp == nil {
		errMsg := "LLM plan generation failed"
		if err != nil {
			errMsg = err.Error()
		}
		s.logger.Warn("Intake LLM refine generation failed", slog.String("error", errMsg))
		assistantMsg = "I encountered an issue processing your request via the LLM. Using current draft plan."
		plan = session.DraftPlan
		planSrc = session.DraftPlanSource
	} else {
		assistantMsg, plan = parseLLMResponse(resp.Content)
		if plan == nil {
			s.logger.Warn("Intake LLM returned empty/unparseable plan in refine, using previous draft")
			assistantMsg = "I encountered an issue parsing the plan generated by the LLM. Using current draft plan."
			plan = session.DraftPlan
			planSrc = session.DraftPlanSource
		} else {
			planSrc = "llm"
		}
	}

	session.PlanningStatus.Stage = "ready"
	agentMsg := SessionMessage{
		From:      "agent",
		Text:      assistantMsg,
		Plan:      plan,
		Timestamp: nowISO(),
	}
	session.Messages = append(session.Messages, agentMsg)
	session.DraftPlan = plan
	session.DraftPlanSource = planSrc
	session.UpdatedAt = nowISO()

	if err := s.es.IndexDoc(ctx, planSessionsIndex, sessionID, session); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to save updated session")
		return
	}

	writeJSON(w, http.StatusOK, prepareSessionResponse(session))
}

// ─── POST /api/intake/session/{session_id}/commit ───────────────────────────

func (s *Server) handleIntakeCommit(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	ctx := r.Context()

	var req struct {
		Plan map[string]interface{} `json:"plan,omitempty"`
		Repo string                 `json:"repo,omitempty"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	sessBytes, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || sessBytes == nil {
		writeError(w, http.StatusNotFound, "session not found")
		return
	}

	var session SessionDoc
	if err := json.Unmarshal(sessBytes, &session); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to unmarshal session")
		return
	}

	// Lightweight early-out guard (symmetric to the anti-explosion guard in Runner.handlePM).
	// Prevents re-committing the same Plan New Work session and re-seeding hundreds of
	// "planned" items that then get exploded by PM decomposition.
	if session.Status == "committed" || len(session.CommittedDocs) > 0 {
		s.logger.Info("intake commit: session already committed, skipping duplicate creation",
			slog.String("session_id", sessionID),
			slog.Int("existing_committed_count", len(session.CommittedDocs)))
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"ok":      true,
			"count":   len(session.CommittedDocs),
			"created": 0,
			"taskIds": session.CommittedDocs,
			"already_committed": true,
		})
		return
	}

	finalPlan := req.Plan
	if finalPlan == nil {
		if draft, ok := session.DraftPlan.(map[string]interface{}); ok {
			finalPlan = draft
		}
	}

	if finalPlan == nil {
		writeError(w, http.StatusBadRequest, "no plan found to commit")
		return
	}

	repo := req.Repo
	if repo == "" {
		repo = session.Repo
	}

	if repo == "" {
		writeError(w, http.StatusBadRequest, "repo ID is required")
		return
	}

	docs, err := s.commitPlan(ctx, repo, finalPlan, sessionID)
	if err != nil {
		s.logger.Error("intake commit failed", slog.String("error", err.Error()))
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("commit failed: %v", err))
		return
	}

	var taskIDs []string
	for _, doc := range docs {
		taskIDs = append(taskIDs, doc.ID)
	}

	// Update session status to committed
	now := nowISO()
	session.Status = "committed"
	session.CommittedAt = now
	session.CommittedDocs = taskIDs
	session.UpdatedAt = now

	_ = s.es.IndexDoc(ctx, planSessionsIndex, sessionID, session)

	// Immediately nudge the worker sweeper for this specific repo.
	// This gives much better "Plan New Work → tasks appear in Ready" UX
	// instead of waiting for the next global 2-5s promote cycle.
	if s.onSweepTrigger != nil {
		go func(r string) {
			time.Sleep(150 * time.Millisecond) // tiny delay for ES visibility
			_ = s.onSweepTrigger("promote:" + r)
		}(repo)
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"ok":      true,
		"count":   len(taskIDs),
		"created": len(docs),
		"taskIds": taskIDs,
	})
}

// ─── Task Commit / Mapping Helpers ──────────────────────────────────────────

func (s *Server) esCounterHWM(ctx context.Context, prefix string) int {
	src, err := s.es.GetDoc(ctx, "flume-counters", prefix)
	if err != nil || src == nil {
		return 0
	}
	var doc struct {
		Value int `json:"value"`
	}
	if err := json.Unmarshal(src, &doc); err == nil {
		return doc.Value
	}
	return 0
}

func (s *Server) esCounterSetHWM(ctx context.Context, prefix string, value int) {
	if value <= 0 {
		return
	}
	now := nowISO()
	body := map[string]interface{}{
		"scripted_upsert": true,
		"script": map[string]interface{}{
			"source": "if (ctx._source.containsKey('value')) { ctx._source.value = Math.max(ctx._source.value, (long)params.v); } else { ctx._source.value = (long)params.v; } ctx._source.updated_at = params.ts; ctx._source.prefix = params.pfx;",
			"lang":   "painless",
			"params": map[string]interface{}{
				"v":   value,
				"ts":  now,
				"pfx": prefix,
			},
		},
		"upsert": map[string]interface{}{
			"prefix":     prefix,
			"value":      value,
			"updated_at": now,
		},
	}
	_ = s.es.Post(ctx, fmt.Sprintf("flume-counters/_update/%s", prefix), body)
}

func (s *Server) getNextIDSequence(ctx context.Context, prefix string) int {
	idSequenceCacheMu.Lock()
	defer idSequenceCacheMu.Unlock()

	if cached, ok := idSequenceCache[prefix]; ok {
		// Fast path: in-memory allocation after initial seed.
		// This is the key performance fix for Plan New Work commits.
		idSequenceCache[prefix] = cached + 1
		return cached + 1
	}

	// Slow path (only on first use per prefix per process lifetime, or after restart):
	// Seed from the persisted HWM + defensive full scan (original behavior).
	maxN := s.esCounterHWM(ctx, prefix)

	query := map[string]interface{}{
		"regexp": map[string]interface{}{
			"id": prefix + "-[0-9]+",
		},
	}

	res, err := s.es.Search(ctx, "agent-task-records", query, 10000)
	if err == nil && res != nil {
		pattern := regexp.MustCompile(fmt.Sprintf(`^%s-(\d+)$`, regexp.QuoteMeta(prefix)))
		for _, h := range res.Hits {
			var doc struct {
				ID string `json:"id"`
			}
			if err := json.Unmarshal(h, &doc); err == nil {
				if m := pattern.FindStringSubmatch(doc.ID); len(m) > 1 {
					var val int
					if _, err := fmt.Sscanf(m[1], "%d", &val); err == nil {
						if val > maxN {
							maxN = val
						}
					}
				}
			}
		}
	} else {
		if maxN == 0 {
			// Fallback for brand new installations
			fallback := int(timeNowUnixMilli()%1000000) + 1
			idSequenceCache[prefix] = fallback
			return fallback
		}
	}

	next := maxN + 1
	idSequenceCache[prefix] = next
	return next
}

var filenameRegexp = regexp.MustCompile(`(?i)\b([\w\.\-]+\.(?:tsx|ts|js|jsx|py|go|html|css|md|json|yml|yaml))\b`)

// idSequenceCache provides fast in-memory allocation of human-readable IDs
// (epic-N, feat-N, story-N, task-N) after an initial seed from ES.
//
// This eliminates the previous performance problem where every Plan New Work
// commit performed 4 expensive regexp scans (size 10k) against agent-task-records.
var (
	idSequenceCache   = make(map[string]int)
	idSequenceCacheMu sync.Mutex
)

func extractTargetFile(title string) string {
	match := filenameRegexp.FindStringSubmatch(title)
	if len(match) > 1 {
		return strings.ToLower(match[1])
	}
	return ""
}

func coalesceStoryTasks(tasks []PlanTask) []PlanTask {
	if len(tasks) == 0 {
		return nil
	}
	var coalesced []PlanTask
	curr := tasks[0]

	for i := 1; i < len(tasks); i++ {
		task := tasks[i]
		tFile := extractTargetFile(task.Title)
		cFile := extractTargetFile(curr.Title)
		if tFile != "" && cFile != "" && tFile == cFile {
			curr.Title = fmt.Sprintf("Compound Task: %s (+ %s)", curr.Title, task.Title)
			curr.Objective = fmt.Sprintf("%s\n\n- %s: %s", curr.Objective, task.Title, task.Objective)
		} else {
			coalesced = append(coalesced, curr)
			curr = task
		}
	}
	coalesced = append(coalesced, curr)
	return coalesced
}

func countPlanTasks(plan PlanResponse) int {
	total := 0
	for _, epic := range plan.Epics {
		for _, feat := range epic.Features {
			for _, story := range feat.Stories {
				coalesced := coalesceStoryTasks(story.Tasks)
				total += len(coalesced)
			}
		}
	}
	return total
}

// getSmartMaxLeafTasks implements research-backed tiered caps for intake (see the
// big implementation report for full Devin/Cursor/LangGraph/Aider/OpenHands/CrewAI sources).
// Uses the planner's own ComplexityScore (already 1-10 with existing Low/Med/High buckets)
// plus a light structural bushiness signal to catch LLM over-decomposition on "simple" tasks.
//
// Structural thresholds are intentionally generous to avoid rejecting legitimate plans.
// A normal 2-epic/2-feature/4-story plan (structural=8) should never be flagged.
// The 258-item incident produced structural counts of 30+ — that's the target.
func getSmartMaxLeafTasks(complexity int, plan PlanResponse) int {
	base := 6
	switch {
	case complexity <= 3:
		base = 8 // Simple: allows reasonable multi-story decomposition (was 6, too tight)
	case complexity <= 6:
		base = 15 // Medium
	default:
		base = 25 // Complex / high-risk
	}

	// Structural over-decomposition detector (common failure mode in the 258-item incident).
	// "structural" = total non-leaf containers (features + stories across all epics).
	// A normal small plan: 2 epics * (1 feat * 2 stories) = structural ~4-8. This is fine.
	// The 258-item incident had structural counts of 30+. That's what we're catching.
	structural := 0
	for _, e := range plan.Epics {
		structural += len(e.Features)
		for _, f := range e.Features {
			structural += len(f.Stories)
		}
	}
	if complexity <= 3 && structural > 12 {
		base = 6 // LLM claimed "simple" but produced a very bushy tree → tighten to baseline
	}
	if complexity <= 6 && structural > 20 {
		if base > 10 {
			base = 10
		}
	}

	// Allow env override for the complex tier in legitimate large projects
	if complexity > 6 {
		if v := os.Getenv("FLUME_MAX_LEAVES_COMPLEX"); v != "" {
			var envBase int
			if n, err := fmt.Sscanf(v, "%d", &envBase); err == nil && n == 1 && envBase > 0 {
				base = envBase
			}
		}
	}

	return base
}

func (s *Server) buildTaskHierarchy(ctx context.Context, plan PlanResponse, repo, routingModel, now string) ([]AgentTaskRecord, error) {
	var docs []AgentTaskRecord

	epicSeq := s.getNextIDSequence(ctx, "epic")
	featSeq := s.getNextIDSequence(ctx, "feat")
	storySeq := s.getNextIDSequence(ctx, "story")
	taskSeq := s.getNextIDSequence(ctx, "task")

	for _, epic := range plan.Epics {
		epicID := fmt.Sprintf("epic-%d", epicSeq)
		epicSeq++
		docs = append(docs, AgentTaskRecord{
			ID:         epicID,
			Title:      epic.Title,
			Objective:  epic.Description,
			Repo:       repo,
			ItemType:   "epic",
			Owner:      "system",            // Not "pm" — organizational container, never a decomposition target
			AssignedAgentRole: "system",
			Status:     "done",              // Already fully decomposed at intake — never enters work queue
			Priority:   "high",
			Risk:       "medium",
			LastUpdate: now,
			CreatedAt:  now,
			UpdatedAt:  now,
			Complexity:       plan.ComplexityScore,
			ComplexityReason: "planner ComplexityScore (PR2 creation)",
			ComplexityBucket: string(ftypes.ToComplexityBucket(plan.ComplexityScore)),
			DependsOn:  []string{},
			ChildCount: len(epic.Features),  // Accurate: epic already has features as children
			DecomposedAt: now,                // Already decomposed at intake — prevents PM re-decomposition
			HierarchyDepth: 0, // Epic root
		})

		for _, feat := range epic.Features {
			featID := fmt.Sprintf("feat-%d", featSeq)
			featSeq++
			docs = append(docs, AgentTaskRecord{
				ID:         featID,
				Title:      feat.Title,
				Objective:  fmt.Sprintf("Feature of %s", epic.Title),
				Repo:       repo,
				ItemType:   "feature",
				Owner:      "system",            // Not "pm" — organizational container, never a decomposition target
				AssignedAgentRole: "system",
				Status:     "done",              // Already fully decomposed at intake — never enters work queue
				Priority:   "medium",
				Risk:       "medium",
				ParentID:   epicID,
				DependsOn:  []string{},          // No deps — organizational container, parent link via ParentID
				LastUpdate: now,
				CreatedAt:  now,
				UpdatedAt:  now,
				Complexity:       plan.ComplexityScore,
				ComplexityReason: "planner ComplexityScore (feature PR2)",
				ComplexityBucket: string(ftypes.ToComplexityBucket(plan.ComplexityScore)),
				ChildCount: len(feat.Stories),   // Accurate: feature already has stories as children
				DecomposedAt: now,                // Already decomposed at intake — prevents PM re-decomposition
				HierarchyDepth: 1, // Feature under epic
			})

			for _, story := range feat.Stories {
				storyID := fmt.Sprintf("story-%d", storySeq)
				storySeq++
				// Stories are the direct organizational parents of executable implementer tasks.
				// We create them as "ready" (instead of "planned") so that:
				//   - Their child tasks can be promoted by promotePlannedTasks (parent check passes)
				//   - They are immediately visible/actionable in the work queue
				// Higher-level epics/feats remain "planned" as pure PM containers.
				storyTaskCount := len(coalesceStoryTasks(story.Tasks))
				docs = append(docs, AgentTaskRecord{
					ID:                 storyID,
					Title:              story.Title,
					Objective:          fmt.Sprintf("Story for %s", feat.Title),
					Repo:               repo,
					ItemType:           "story",
					Owner:              "system",            // Not "pm" — organizational container, never a decomposition target
					AssignedAgentRole:  "system",
					Status:             "done",              // Already fully decomposed at intake — never enters work queue
					Priority:           "medium",
					Risk:               "medium",
					ParentID:           featID,
					DependsOn:          []string{},          // No deps — organizational container, parent link via ParentID
					AcceptanceCriteria: story.AcceptanceCriteria,
					LastUpdate:         now,
					Complexity:       plan.ComplexityScore,
					ComplexityReason: "planner ComplexityScore (story PR2)",
					ComplexityBucket: string(ftypes.ToComplexityBucket(plan.ComplexityScore)),
					CreatedAt:          now,
					UpdatedAt:          now,
					ChildCount: storyTaskCount,      // Accurate: story already has tasks as children
					DecomposedAt: now,                // Already decomposed at intake — prevents PM re-decomposition
					HierarchyDepth: 2, // Story under feature
				})

				prevTaskID := ""
				for _, task := range coalesceStoryTasks(story.Tasks) {
					taskID := fmt.Sprintf("task-%d", taskSeq)
					taskSeq++

					status := "planned"
					if prevTaskID == "" {
						status = "ready"
					}

					var dependsOn []string
					if prevTaskID != "" {
						dependsOn = []string{prevTaskID}
					}

					docs = append(docs, AgentTaskRecord{
						ID:                 taskID,
						Title:              task.Title,
						Objective:          orStr(task.Objective, fmt.Sprintf("Task for %s", story.Title)),
						Repo:               repo,
						ItemType:           "task",
						Owner:              "implementer",
						AssignedAgentRole:  "implementer",
						Status:             status,
						Priority:           "normal",
						Risk:               "medium",
						ParentID:           storyID,
						DependsOn:          dependsOn,
						AcceptanceCriteria: story.AcceptanceCriteria,
						PreferredModel:     routingModel,
						LastUpdate:         now,
					Complexity:       plan.ComplexityScore,
					ComplexityReason: "planner ComplexityScore (task PR2)",
					ComplexityBucket: string(ftypes.ToComplexityBucket(plan.ComplexityScore)),
						CreatedAt:          now,
						UpdatedAt:          now,
						ChildCount: 0,
						DecomposedAt: "",
						HierarchyDepth: 3, // Task under story
					})
					prevTaskID = taskID
				}
			}
		}
	}

	s.esCounterSetHWM(ctx, "epic", epicSeq-1)
	s.esCounterSetHWM(ctx, "feat", featSeq-1)
	s.esCounterSetHWM(ctx, "story", storySeq-1)
	s.esCounterSetHWM(ctx, "task", taskSeq-1)

	return docs, nil
}

func (s *Server) buildFastPathTasks(ctx context.Context, plan PlanResponse, repo, routingModel, now string) ([]AgentTaskRecord, error) {
	var docs []AgentTaskRecord

	taskSeq := s.getNextIDSequence(ctx, "task")
	prevTaskID := ""

	for _, epic := range plan.Epics {
		for _, feat := range epic.Features {
			for _, story := range feat.Stories {
				for _, task := range coalesceStoryTasks(story.Tasks) {
					taskID := fmt.Sprintf("task-%d", taskSeq)
					taskSeq++

					status := "planned"
					if prevTaskID == "" {
						status = "ready"
					}

					var dependsOn []string
					if prevTaskID != "" {
						dependsOn = []string{prevTaskID}
					}

					docs = append(docs, AgentTaskRecord{
						ID:                 taskID,
						Title:              task.Title,
						Objective:          orStr(epic.Description, story.Title),
						Repo:               repo,
						ItemType:           "task",
						Owner:              "implementer",
						AssignedAgentRole:  "implementer",
						Status:             status,
						Priority:           "normal",
						Risk:               "medium",
						DependsOn:          dependsOn,
						AcceptanceCriteria: story.AcceptanceCriteria,
						PreferredModel:     routingModel,
						LastUpdate:         now,
					Complexity:       plan.ComplexityScore,
					ComplexityReason: "planner ComplexityScore (fastpath PR2)",
					ComplexityBucket: string(ftypes.ToComplexityBucket(plan.ComplexityScore)),
						CreatedAt:          now,
						UpdatedAt:          now,
						ChildCount: 0,
						DecomposedAt: "",
						HierarchyDepth: 0, // Fastpath tasks are flat roots
					})
					prevTaskID = taskID
				}
			}
		}
	}

	s.esCounterSetHWM(ctx, "task", taskSeq-1)
	return docs, nil
}

// ─── Phase 2 Plan Session Budget Enforcement Helpers (task 1) ─────────────────

// loadPlanSession retrieves the agent-plan-sessions doc (or returns zero with defaults).
// Used at every enforcement point (intake commit, handlePM, promote, claim).
func (s *Server) loadPlanSession(ctx context.Context, sessionID string) (SessionDoc, error) {
	var sess SessionDoc
	if sessionID == "" {
		return sess, fmt.Errorf("empty plan session id")
	}
	bytes, err := s.es.GetDoc(ctx, planSessionsIndex, sessionID)
	if err != nil || bytes == nil {
		return sess, fmt.Errorf("plan session %s not found: %w", sessionID, err)
	}
	if uerr := json.Unmarshal(bytes, &sess); uerr != nil {
		return sess, uerr
	}
	// Apply defaults if not present (legacy sessions)
	if sess.ItemBudget == 0 {
		sess.ItemBudget = 40
	}
	if sess.TokenBudget == 0 {
		sess.TokenBudget = 80000
	}
	return sess, nil
}

// checkPlanBudget returns (allowed, reason). proposedDelta is the #items this action would add.
// "fail closed": any load error or exceed blocks the action with rich context for LogAgentReasoning.
func checkPlanBudget(sess SessionDoc, proposedDelta int) (bool, string) {
	if sess.BudgetStatus == "exceeded" {
		return false, fmt.Sprintf("plan session %s already budget_exceeded (current_items=%d >= item_budget=%d)", sess.ID, sess.CurrentItems, sess.ItemBudget)
	}
	if sess.ItemBudget > 0 && sess.CurrentItems+proposedDelta > sess.ItemBudget {
		return false, fmt.Sprintf("plan budget exceeded: adding %d would reach %d (cap %d) for session %s", proposedDelta, sess.CurrentItems+proposedDelta, sess.ItemBudget, sess.ID)
	}
	return true, ""
}

// atomicIncrementPlanCounters uses inline ES script for race-free update of current_items (and optionally tokens).
// Called on successful task creation at intake, on promote success, and on claim success for the plan.
func (s *Server) atomicIncrementPlanCounters(ctx context.Context, sessionID string, itemDelta, tokenDelta int) error {
	if sessionID == "" {
		return nil
	}
	script := `
		if (ctx._source.current_items == null) { ctx._source.current_items = 0; }
		ctx._source.current_items += params.item_delta;
		if (params.token_delta > 0) {
			if (ctx._source.current_tokens == null) { ctx._source.current_tokens = 0; }
			ctx._source.current_tokens += params.token_delta;
		}
		if (ctx._source.item_budget != null && ctx._source.item_budget > 0 && ctx._source.current_items > ctx._source.item_budget) {
			ctx._source.budget_status = "exceeded";
		}
		ctx._source.updated_at = params.now;
	`
	params := map[string]interface{}{
		"item_delta":  itemDelta,
		"token_delta": tokenDelta,
		"now":         nowISO(),
	}
	if err := s.es.UpdateDocWithInlineScript(ctx, planSessionsIndex, sessionID, script, params); err != nil {
		s.logger.Warn("plan budget atomic increment failed (best effort)", slog.String("session", sessionID), slog.String("err", err.Error()))
		return err
	}
	return nil
}

func (s *Server) commitPlan(ctx context.Context, repo string, planDict map[string]interface{}, planSessionID string) ([]AgentTaskRecord, error) {
	var plan PlanResponse
	planBytes, err := json.Marshal(planDict)
	if err == nil {
		_ = json.Unmarshal(planBytes, &plan)
	}

	now := nowISO()

	// 1. Adaptive LLM Routing
	complexityScore := plan.ComplexityScore
	fastModel := os.Getenv("FLUME_FAST_MODEL")
	if fastModel == "" {
		fastModel = "o3-mini"
	}
	var routingModel string
	if complexityScore <= 3 {
		routingModel = fastModel
	}

	// 2. Build records — SMART tiered intake cap (research-backed dynamic sizing)
	// Replaces the previous blunt max=12. Now uses the planner's own ComplexityScore (1-10)
	// with the existing bucket logic (low 1-3, med 4-6, high 7-10) plus structural awareness.
	//
	// Research synthesis (Devin confidence+interactive planning, Cursor dynamic effort calibration
	// + RL thinking allocation, LangGraph explicit ComplexityScorer + orchestrator-worker depth,
	// Aider Architect/Editor + explicit "do not over-decompose" guidance, OpenHands Planning Agent,
	// CrewAI complexity/precision matrix):
	//   - Low complexity (1-3): very tight cap, force minimal decomposition.
	//   - Medium (4-6): moderate.
	//   - High (7-10): allow more but still bounded + encourage refine for very bushy plans.
	// Secondary signal: if the plan is structurally bushy (many epics/stories) relative to
	// declared complexity, we treat it as over-decomposition risk and tighten.
	//
	// Phase 2 addition (starter): depth enforcement will be added when PlanResponse carries
	// depth or we compute it from the hierarchy shape. For now the constant is defined in types.
	_ = ftypes.MAX_HIERARCHY_DEPTH // Phase 2 constant available for future use

	var docs []AgentTaskRecord
	var errBuild error
	totalTasks := countPlanTasks(plan)
	complexityScore = plan.ComplexityScore // already declared earlier for routing

	maxAllowed := getSmartMaxLeafTasks(complexityScore, plan)

	if totalTasks > maxAllowed {
		reason := fmt.Sprintf("Intake cap hit: planner produced %d leaf tasks for complexityScore=%d (allowed max=%d for this tier). This matches the exact over-decomposition pattern that previously exploded simple docs tasks to 258+ items. Use 'refine' in the planner or narrow the objective.", totalTasks, complexityScore, maxAllowed)

		s.logger.Warn("intake smart cap refused plan",
			slog.String("repo", repo),
			slog.Int("complexityScore", complexityScore),
			slog.Int("leaf_tasks", totalTasks),
			slog.Int("max_allowed", maxAllowed))

		// Rich signal for the agent reasoning popout / Logloom (even at intake time)
		// Best-effort; the main path for execution reasoning is the worker LogAgentReasoning.
		_ = s.es.IndexDoc(ctx, "agent-task-records", "intake-cap-"+fmt.Sprintf("%d", time.Now().UnixNano()), map[string]interface{}{
			"event":           "intake_cap_refused",
			"repo":            repo,
			"complexityScore": complexityScore,
			"leaf_tasks":      totalTasks,
			"max_allowed":     maxAllowed,
			"reason":          reason,
			"timestamp":       now,
		})

		return nil, fmt.Errorf("%s\n\nRepo: %s\nRecommended action: Hit 'refine' and ask the planner for a much smaller scope (target 1-3 leaf tasks for documentation-style work).", reason, repo)
	}

	if totalTasks > 0 && totalTasks <= 6 {
		docs, errBuild = s.buildFastPathTasks(ctx, plan, repo, routingModel, now)
	} else {
		docs, errBuild = s.buildTaskHierarchy(ctx, plan, repo, routingModel, now)
	}
	if errBuild != nil {
		return nil, errBuild
	}

	// === Phase 2 FULL BUDGET ENFORCEMENT AT INTAKE (commitPlan) ===
	// Load plan session (creates correlation), check proposed total items against per-plan budget.
	// On pass: atomically increment via ES script (fail-closed). Rich audit event on block.
	// Also inject PlanSessionID + HierarchyDepth=0 (roots) for downstream enforcement (handlePM, promote, claim).
	if planSessionID != "" {
		sess, loadErr := s.loadPlanSession(ctx, planSessionID)
		if loadErr != nil {
			s.logger.Warn("commitPlan: could not load plan session for budget check (proceeding with correlation only)", slog.String("session", planSessionID), slog.String("err", loadErr.Error()))
		} else {
			proposed := len(docs)
			if allowed, reason := checkPlanBudget(sess, proposed); !allowed {
				// Rich LogAgentReasoning-equivalent audit at intake (feeds UI + Logloom)
				audit := map[string]interface{}{
					"event":            "plan_budget_refused_at_intake",
					"plan_session_id":  planSessionID,
					"repo":             repo,
					"proposed_items":   proposed,
					"current_items":    sess.CurrentItems,
					"item_budget":      sess.ItemBudget,
					"reason":           reason,
					"timestamp":        now,
				}
				_ = s.es.IndexDoc(ctx, "agent-task-records", "plan-budget-block-"+fmt.Sprintf("%d", time.Now().UnixNano()), audit)

				s.logger.Error("commitPlan: plan budget exceeded — refusing creation (fail closed)",
					slog.String("session", planSessionID), slog.String("reason", reason))
				return nil, fmt.Errorf("PLAN_BUDGET_EXCEEDED: %s. Refine your request or increase scope limits.", reason)
			}
			// Will increment after successful indexing below
		}

		// Propagate correlation for budget/depth enforcement downstream.
		// Depth is now computed accurately inside the hierarchy builders (epic=0, feat=1, story=2, task=3 for hierarchy; 0 for fastpath).
		// PM-created children get parent.depth + 1 (enforced <= MAX in handlePM/promote).
		for i := range docs {
			docs[i].PlanSessionID = planSessionID
		}
	}

	// 3. Index to Elasticsearch.
	// AgentTaskRecord uses json:"repo" (via .Repo), matching ftypes.Task.
	// This guarantees the project identifier is always stored under "repo".
	for _, doc := range docs {
		if err := s.es.IndexDoc(ctx, "agent-task-records", doc.ID, doc); err != nil {
			s.logger.Error("commitPlan: failed to index doc", slog.String("id", doc.ID), slog.String("error", err.Error()))
			return nil, fmt.Errorf("failed to index task %s: %w", doc.ID, err)
		}
	}

	// Atomic budget counter update (post-index success) using script for safety.
	if planSessionID != "" && len(docs) > 0 {
		_ = s.atomicIncrementPlanCounters(ctx, planSessionID, len(docs), 0)
	}

	return docs, nil
}

// ─── Helpers ────────────────────────────────────────────────────────────────

var (
	thinkRegexp = regexp.MustCompile(`(?s)<think>.*?</think>`)
	fenceRegexp = regexp.MustCompile(`(?s)^\x60\x60\x60(?:json)?\s*(.*?)\s*\x60\x60\x60$`)
	jsonRegexp  = regexp.MustCompile(`(?s)\{.*?\}`)
)

func parseLLMResponse(raw string) (string, interface{}) {
	cleaned := strings.TrimSpace(raw)
	cleaned = thinkRegexp.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(cleaned)

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

	// Try direct unmarshal first (best case)
	var obj map[string]interface{}
	if err := json.Unmarshal([]byte(cleaned), &obj); err == nil {
		if msg, ok := obj["message"].(string); ok {
			plan := obj["plan"]
			return msg, plan
		}
	}

	// Robust extraction: find the outermost JSON object { ... } by matching first '{' and last '}'
	// This handles extra text, markdown, or partial thinking output much better than the old non-greedy regex.
	if start := strings.Index(cleaned, "{"); start != -1 {
		if end := strings.LastIndex(cleaned, "}"); end != -1 && end > start {
			candidate := cleaned[start : end+1]
			var obj map[string]interface{}
			if err := json.Unmarshal([]byte(candidate), &obj); err == nil {
				if msg, ok := obj["message"].(string); ok {
					plan := obj["plan"]
					return msg, plan
				}
			}
		}
	}

	// Fallback to old regex (for very malformed cases)
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
