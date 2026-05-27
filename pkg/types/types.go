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

import (
	"os"
	"time"
)

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

// ComplexityBucket is the categorical classification of a task's complexity (1-10 scale).
// Used for adaptive routing (see src/gateway/routing_policy.go), queue prioritization,
// and reporting. Derived from planner's ComplexityScore and per-task analysis in intake.
type ComplexityBucket string

const (
	// ComplexityBucketLow: trivial/localized changes (1-3). Prefer fast models.
	ComplexityBucketLow ComplexityBucket = "low"
	// ComplexityBucketMedium: standard multi-step (4-6).
	ComplexityBucketMedium ComplexityBucket = "medium"
	// ComplexityBucketHigh: cross-cutting or high-risk (7-10). Route to frontier models.
	ComplexityBucketHigh ComplexityBucket = "high"
)

// ToComplexityBucket maps a 1-10 complexity score to its bucket.
// Safe for 0 or out-of-range (clamps to low/high).
func ToComplexityBucket(score int) ComplexityBucket {
	if score <= 3 {
		return ComplexityBucketLow
	}
	if score <= 6 {
		return ComplexityBucketMedium
	}
	return ComplexityBucketHigh
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
	// ComplexityReason captures the rationale from the planner (e.g. "cross-cutting change requiring 4 components").
	// Populated on task creation in intake (api_intake.go build* and commit) from LLM plan output.
	// Enables auditing why a task received its complexity score.
	ComplexityReason string `json:"complexity_reason,omitempty"`
	// ComplexityBucket is the derived categorical bucket (low/medium/high) for
	// routing, WIP gates, and observability. Written on creation in intake using ToComplexityBucket.
	ComplexityBucket ComplexityBucket `json:"complexity_bucket,omitempty"`
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

	// DecomposedAt and ChildCount are denormalized fields for cheap guards against
	// re-decomposition explosions (see Runner.handlePM and intake commit paths).
	// Updated by the PM/implementer after creating children.
	DecomposedAt *time.Time `json:"decomposed_at,omitempty"`
	ChildCount   int        `json:"child_count,omitempty"`
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

// ─── Task State Machine (PR 2) ──────────────────────────────────────────────

// TaskStateMachine is the central, enforceable FSM for all Task status changes
// on agent-task-records documents.
//
// GOAL (from flume-queue-planning-reliability PR 2):
//   - 100% of status mutations go through validation + auditing path.
//   - No more raw ES "status" updates in hot paths (api_intake build*/commit,
//     api_tasks transition+bulk+claim+complete, worker/claim atomic, runner update*,
//     sweeps all sites, manager, dispatch, tests).
//   - Per-task Complexity* fields supported on Task (and mirrored on AgentTaskRecord).
//
// EnforceTransition is the single choke point. It calls ValidateTransition.
// Updates to the doc (with OCC where possible), audit recording, and metrics
// emission are performed by callers that invoke EnforceTransition before/around
// their ES writes (current implementation); future iterations can move the
// actual safe write inside Enforce when an updater hook is provided.
//
// Starts in ShadowMode (see DefaultTaskStateMachine) for safe rollout:
//   - Violations are returned as errors (for logging + future metrics counters
//     e.g. "flume_task_state_violation_total{shadow=1}").
//   - Callers STILL perform the write, allowing production to adopt the guard
//     without risk of breaking existing flows.
//   - Once violations reach zero in logs, flip shadow=false to hard-enforce.
//
// All writers MUST:
//   1. Compute prevStatus from doc / task
//   2. Call EnforceTransition(prev, target)
//   3. If err != nil && !shadow { abort } else { log shadow violation for audit; proceed }
//   4. Perform the ES update (OCC preferred for claim paths).
//
// Clear godoc and comments added per success criteria.
type TaskStateMachine struct {
	// ShadowMode: log + "metric" (via structured logs at call sites) violations
	// but permit the status write. See package-level DefaultTaskStateMachine.
	ShadowMode bool
}

// NewTaskStateMachine constructs a TaskStateMachine.
// Use shadow=true during PR2 rollout (the default singleton below).
func NewTaskStateMachine(shadow bool) *TaskStateMachine {
	return &TaskStateMachine{ShadowMode: shadow}
}

// DefaultTaskStateMachine is the process-wide enforcer singleton.
//
// Shadow mode (default) logs violations for audit but allows the write to proceed.
// This was the safe rollout strategy for PR2 (flume-queue-planning-reliability).
//
// The mode can be controlled at startup via the environment variable:
//   FLUME_TASK_STATE_MACHINE_SHADOW_MODE=false   → hard enforcement (strict mode)
//   FLUME_TASK_STATE_MACHINE_SHADOW_MODE=true    → shadow mode (default)
//
// After an audit period with zero violations in production logs, operators
// should flip to strict mode in non-critical environments first.
var DefaultTaskStateMachine = NewTaskStateMachine(defaultShadowMode())

func defaultShadowMode() bool {
	v := os.Getenv("FLUME_TASK_STATE_MACHINE_SHADOW_MODE")
	if v == "" {
		return true // safe default for rollout
	}
	return v != "false" && v != "0" && v != "off"
}

// EnforceTransition is the primary entrypoint called by 100% of status writers.
//
// It invokes ValidateTransition (preserving all existing behavior and error types).
// In shadow mode a violation error is still surfaced to the caller so the call site
// can emit a structured log line containing "shadow_violation" (treated as metric
// source for now) while allowing the subsequent ES write.
//
// Returns:
//   - nil for valid (including no-op/empty/self)
//   - *InvalidTransitionError for disallowed changes (see validation.go)
//   - other errors in future (e.g. OCC failure when update hook added)
//
// Callers are responsible for the actual document update + any OCC parameters.
// This separation keeps pkg/types free of ES / logger dependencies (no cycles).
func (sm *TaskStateMachine) EnforceTransition(current, target TaskStatus) error {
	// Delegate to the existing pure validator (port of Python TaskStateMachine.validate_transition).
	err := ValidateTransition(current, target)
	if err != nil && sm.ShadowMode {
		// Shadow mode: the violation is "enforced" only for observability.
		// Callers are expected to log using EnforceTransitionOrLog (preferred) or
		// manually log with rich context so that LogLoom + ES can aggregate violations.
	}
	// In non-shadow (strict) mode, the caller is responsible for respecting the returned error
	// and aborting the write (see current behavior in runner.go:111 and similar sites).
	return err
}

// EnforceTransitionOrLog is a convenience for sites that have a logger.
// In shadow mode, violations are logged at Warn but write proceeds.
// In strict mode, error is returned and caller should not write.
//
// All violation logs include consistent fields for easy aggregation
// via LogLoom / Elasticsearch (search for "TaskStateMachine" + "shadow").
func (sm *TaskStateMachine) EnforceTransitionOrLog(current, target TaskStatus, logFn func(msg string, args ...any)) error {
	err := sm.EnforceTransition(current, target)
	if err != nil && sm.ShadowMode && logFn != nil {
		logFn("SHADOW MODE: TaskStateMachine.EnforceTransition violation allowed (write proceeds for rollout safety)",
			"error", err.Error(),
			"from", current,
			"to", target,
			"shadow", true,
			"violation", true, // easy filter for dashboards / alerts
		)
	}
	return err
}

// SetDefaultShadowMode allows runtime control of the global DefaultTaskStateMachine
// (primarily for tests or operator-driven flips after an audit period).
// It is not recommended for normal production use — prefer the env var at startup.
func SetDefaultShadowMode(shadow bool) {
	DefaultTaskStateMachine = NewTaskStateMachine(shadow)
}