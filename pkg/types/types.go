// Package types defines the shared domain types for the Flume application.
//
// These types are consumed by all internal packages (worker, dashboard, llm, etc.)
// and are the single source of truth for JSON serialization schemas.
//
// Derived from LogLoom AST analysis of:
//   - worker-manager/lifecycle/state_machine.py (2 nodes) — TaskStatus, FSM transitions
//   - worker-manager/providers/registry.py (7 nodes)     — LLMResponse, LLMProvider
//   - worker-manager/roles/common.py (2 nodes)           — AgentResult
//   - dashboard/core/tasks.py (21 nodes)                 — Task model
//   - dashboard/core/projects_store.py (8 nodes)         — Project model
//   - llm_credentials_store.py (8 nodes)                 — LlmCredential, LlmMetadataDoc
//   - dashboard/agent_models_settings.py                 — AgentRoleSpec, AgentModelsDoc
package types

import "time"

// ─── Task Lifecycle ─────────────────────────────────────────────────────────

// TaskStatus represents the finite set of task lifecycle states.
// Derived from Python: TaskStateMachine.TRANSITIONS (state_machine.py)
type TaskStatus string

const (
	TaskStatusInbox           TaskStatus = "inbox"
	TaskStatusPlanned         TaskStatus = "planned"
	TaskStatusReady           TaskStatus = "ready"
	TaskStatusRunning         TaskStatus = "running"
	TaskStatusReview          TaskStatus = "review"
	TaskStatusReviewConsensus TaskStatus = "review-consensus"
	TaskStatusDone            TaskStatus = "done"
	TaskStatusBlocked         TaskStatus = "blocked"
	TaskStatusArchived        TaskStatus = "archived"
)

// ValidTransitions defines the FSM for task state changes.
// Direct port from Python: TaskStateMachine.TRANSITIONS.
var ValidTransitions = map[TaskStatus][]TaskStatus{
	TaskStatusInbox:           {TaskStatusPlanned, TaskStatusReady, TaskStatusArchived},
	TaskStatusPlanned:         {TaskStatusReady, TaskStatusBlocked, TaskStatusArchived},
	TaskStatusReady:           {TaskStatusRunning, TaskStatusBlocked, TaskStatusArchived},
	TaskStatusRunning:         {TaskStatusReview, TaskStatusReviewConsensus, TaskStatusDone, TaskStatusBlocked, TaskStatusReady, TaskStatusArchived},
	TaskStatusReview:          {TaskStatusReviewConsensus, TaskStatusDone, TaskStatusRunning, TaskStatusBlocked, TaskStatusReady, TaskStatusArchived},
	TaskStatusReviewConsensus: {TaskStatusDone, TaskStatusRunning, TaskStatusBlocked, TaskStatusReady, TaskStatusArchived},
	TaskStatusDone:            {TaskStatusArchived, TaskStatusReady, TaskStatusBlocked},
	TaskStatusBlocked:         {TaskStatusReady, TaskStatusArchived},
	TaskStatusArchived:        {TaskStatusReady},
}

// Task represents a work item in the Flume queue.
// Derived from Python: dashboard/core/tasks.py and ES document schema.
type Task struct {
	ID              string     `json:"id"`
	Title           string     `json:"title"`
	Description     string     `json:"objective,omitempty"`
	Status          TaskStatus `json:"status"`
	Priority        string     `json:"priority,omitempty"`
	// ProjectID holds the logical project/repo identifier for this task.
	// It serializes to the "repo" field in Elasticsearch (agent-task-records index)
	// and in JSON. This mapping was a common source of query bugs during the
	// Python-to-Go port (queries must use "repo", not "project_id").
	// See also: internal/worker/sweeps.go (unified promotePlannedTasks PR3), claim.go OCC, runner.go (deprecated ComputeReadyForRepo),
	// and api_intake.go AgentTaskRecord.
	ProjectID       string     `json:"repo,omitempty"`
	ParentID        string     `json:"parent_id,omitempty"`
	ItemType        string     `json:"item_type,omitempty"`
	AssignedWorker  string     `json:"assigned_worker,omitempty"`
	WorkerRole      string     `json:"worker_role,omitempty"`
	Model           string     `json:"model,omitempty"`
	Provider        string     `json:"provider,omitempty"`
	ExecutionHost   string     `json:"execution_host,omitempty"`
	CredentialID    string     `json:"credential_id,omitempty"`
	Tags            []string   `json:"tags,omitempty"`
	EstimatedTokens int        `json:"estimated_tokens,omitempty"`
	ActualTokens    int        `json:"actual_tokens,omitempty"`
	Complexity      int        `json:"complexity,omitempty"`
	Attempts        int        `json:"attempts,omitempty"`
	MaxAttempts     int        `json:"max_attempts,omitempty"`
	ErrorMessage    string     `json:"error_message,omitempty"`
	CreatedAt       time.Time  `json:"created_at"`
	UpdatedAt       time.Time  `json:"updated_at,omitempty"`
	LastUpdate      time.Time  `json:"last_update,omitempty"`
	CompletedAt     *time.Time `json:"completed_at,omitempty"`
	DependsOn       []string   `json:"depends_on,omitempty"`
	ReviewVerdict   string     `json:"review_verdict,omitempty"`
	Feedback        string     `json:"feedback,omitempty"`
}

// ─── Worker ─────────────────────────────────────────────────────────────────

// WorkerStatus represents a worker node's operational state.
type WorkerStatus string

const (
	WorkerStatusIdle    WorkerStatus = "idle"
	WorkerStatusActive  WorkerStatus = "active"
	WorkerStatusClaimed WorkerStatus = "claimed"
	WorkerStatusBusy    WorkerStatus = "busy"
	WorkerStatusDead    WorkerStatus = "dead"
)

// Worker represents a single agent worker in the node mesh.
// Derived from Python: worker-manager/orchestration/workers.py (7 nodes).
type Worker struct {
	Name             string       `json:"name"`
	Role             string       `json:"role"`
	Status           WorkerStatus `json:"status"`
	Model            string       `json:"model"`
	Provider         string       `json:"llm_provider"`
	ExecutionHost    string       `json:"execution_host"`
	CredentialID     string       `json:"credential_id,omitempty"`
	CurrentTaskID    string       `json:"current_task_id,omitempty"`
	CurrentTaskTitle string       `json:"current_task_title,omitempty"`
	PID              int          `json:"pid,omitempty"`
	LastHeartbeat    time.Time    `json:"last_heartbeat,omitempty"`
}

// ─── Project ────────────────────────────────────────────────────────────────

// Project represents a code project managed by Flume.
// Derived from Python: dashboard/core/projects_store.py (8 nodes).
type Project struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	RepoURL     string    `json:"repoUrl,omitempty"`
	Branch      string    `json:"branch,omitempty"`
	LocalPath   string    `json:"path,omitempty"`
	Description string    `json:"description,omitempty"`
	Tags        []string  `json:"tags,omitempty"`
	TaskCount   int       `json:"task_count,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at,omitempty"`
}

// ─── LLM Provider ───────────────────────────────────────────────────────────

// LLMResponse is the unified response envelope from any LLM provider.
// Derived from Python: @dataclass LLMResponse in providers/registry.py (35 nodes).
type LLMResponse struct {
	Content   string                 `json:"content"`
	Usage     map[string]interface{} `json:"usage,omitempty"`
	Telemetry map[string]interface{} `json:"telemetry,omitempty"`
	ToolCalls []ToolCall             `json:"tool_calls,omitempty"`
	Thoughts  string                 `json:"thoughts,omitempty"`
	Raw       map[string]interface{} `json:"raw,omitempty"`
}

// ToolCall represents a function call requested by the LLM.
type ToolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

// AgentResult captures the output of a single agent execution cycle.
// Derived from Python: @dataclass AgentResult in roles/common.py.
type AgentResult struct {
	Success       bool           `json:"success"`
	Output        string         `json:"output,omitempty"`
	ToolCalls     []ToolCall     `json:"tool_calls,omitempty"`
	TokensUsed    int            `json:"tokens_used,omitempty"`
	Errors        []string       `json:"errors,omitempty"`
	NextStatus    TaskStatus     `json:"next_status,omitempty"`
}

// ─── Credentials ────────────────────────────────────────────────────────────

// Credential represents a single LLM API credential.
// Derived from Python: LlmCredential(BaseModel) in llm_credentials_store.py.
type Credential struct {
	ID       string `json:"id"`
	Label    string `json:"label"`
	Provider string `json:"provider"`
	APIKey   string `json:"apiKey,omitempty"`
	BaseURL  string `json:"baseUrl,omitempty"`
}

// CredentialAction defines the set of valid mutation operations.
// Derived from Python: LlmActionPayload.action Literal type.
type CredentialAction string

const (
	CredentialActionUpsert   CredentialAction = "upsert"
	CredentialActionDelete   CredentialAction = "delete"
	CredentialActionActivate CredentialAction = "activate"
	CredentialActionDefault  CredentialAction = "default"
	CredentialActionPatch    CredentialAction = "patch"
)

// CredentialMetadata is the ES-stored credential metadata document.
// Derived from Python: LlmMetadataDoc(BaseModel).
type CredentialMetadata struct {
	Version             int          `json:"version"`
	ActiveCredentialID  string       `json:"activeCredentialId"`
	DefaultCredentialID string       `json:"defaultCredentialId"`
	Credentials         []Credential `json:"credentials"`
}

// CredentialActionPayload is the API request body for credential mutations.
// Derived from Python: LlmActionPayload(BaseModel).
type CredentialActionPayload struct {
	Action   CredentialAction `json:"action"`
	ID       string           `json:"id,omitempty"`
	Label    string           `json:"label,omitempty"`
	Provider string           `json:"provider,omitempty"`
	APIKey   string           `json:"apiKey,omitempty"`
	BaseURL  string           `json:"baseUrl,omitempty"`
}

// ─── Agent Model Configuration ──────────────────────────────────────────────

// AgentRoleSpec configures the LLM for a specific agent role.
// Derived from Python: AgentRoleSpec(BaseModel) in agent_models_settings.py.
type AgentRoleSpec struct {
	CredentialID  string `json:"credentialId,omitempty"`
	Provider      string `json:"provider,omitempty"`
	Model         string `json:"model,omitempty"`
	ExecutionHost string `json:"executionHost,omitempty"`
}

// AgentModelsDoc is the ES-stored agent model configuration.
// Derived from Python: AgentModelsDoc(BaseModel) in agent_models_settings.py.
type AgentModelsDoc struct {
	Version int                      `json:"version"`
	Global  AgentRoleSpec            `json:"global"`
	Roles   map[string]AgentRoleSpec `json:"roles,omitempty"`
}

// ─── Well-Known Constants ───────────────────────────────────────────────────

const (
	// OllamaCredentialID is the synthetic credential for local Ollama.
	OllamaCredentialID = "__ollama__"
	// SettingsDefaultCredentialID uses the global Settings → LLM credential.
	SettingsDefaultCredentialID = "__settings_default__"
	// OpenAIOAuthCredentialID uses the OAuth-based OpenAI credential.
	OpenAIOAuthCredentialID = "__openai_oauth__"
)

// ProviderAliases normalizes provider names to canonical IDs.
// Derived from Python: _PROVIDER_ALIASES dict in llm_credentials_store.py.
var ProviderAliases = map[string]string{
	"google":               "gemini",
	"google-ai":            "gemini",
	"google_ai":            "gemini",
	"googleaistudio":       "gemini",
	"generativelanguage":   "gemini",
}
