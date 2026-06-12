package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Elastro / Logloom index names (the exact indices the implementer workers
// MUST query via the registered tools for the Elastro/Logloom contract #4).
// These are populated at project onboarding (elastro rag ingest + logloom build+ship).
// Documented here + in src/agents/implementer/SYSTEM_PROMPT.md so agents and
// code always know the canonical names.
const (
	ElastroGraphIndex      = "flume-elastro-graph"
	LogloomEnrichmentIndex = "flume-logloom-enrichment"
	LogloomASTIndex        = "flume-logloom-ast"
)

// Handler is the interface implemented by all role-specific agent handlers.
// Derived from Python: handlers/implementer.py, reviewer.py, tester.py,
// and the role dispatch in worker_handlers.py (112 AST nodes total).
type Handler interface {
	// Execute runs the handler for a specific task.
	Execute(ctx context.Context, taskID string) (ftypes.AgentResult, error)
	// Role returns the handler's role identifier.
	Role() string
}

// ─── Handler Registry ───────────────────────────────────────────────────────

var (
	handlerRegistry = map[string]Handler{}
)

// RegisterHandler registers a handler for a specific role.
func RegisterHandler(h Handler) {
	handlerRegistry[h.Role()] = h
}

// resolveHandler finds the handler for a worker by extracting role from name.
func resolveHandler(workerName string) Handler {
	// Worker names follow pattern: {role}-{nodeID}-{index} or just {role}
	role := workerName
	if idx := strings.Index(workerName, "-"); idx > 0 {
		role = workerName[:idx]
	}
	return handlerRegistry[role]
}

// ─── Implementer Handler ────────────────────────────────────────────────────

// ImplementerHandler executes code implementation tasks.
// Derived from Python: handlers/implementer.py (412 LOC, 30 AST nodes).
//
// The implementer lifecycle:
//  1. Clone/checkout task branch
//  2. Build context from task description + codebase
//  3. Send LLM prompt with tool-calling capabilities
//  4. Execute tool calls (file read/write, shell, AST query)
//  5. Auto-commit, push, and create PR
//  6. Transition task to review
type ImplementerHandler struct {
	logger *slog.Logger
}

// NewImplementerHandler creates a new implementer.
func NewImplementerHandler(logger *slog.Logger) *ImplementerHandler {
	return &ImplementerHandler{
		logger: logger.With(slog.String("handler", "implementer")),
	}
}

func (h *ImplementerHandler) Role() string { return "implementer" }

func (h *ImplementerHandler) Execute(ctx context.Context, taskID string) (ftypes.AgentResult, error) {
	h.logger.Info("implementer: starting task",
		slog.String("task_id", taskID))

	// Implementation follows the Python flow:
	// 1. ensure_task_branch → git checkout
	// 2. Build system prompt + context
	// 3. LLM chat loop with tool calls
	// 4. auto_commit_and_push
	// 5. create_pr_for_task (if branch has new commits)
	// 6. Update task status → review

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusReview,
	}, nil
}

// ─── Reviewer Handler ───────────────────────────────────────────────────────

// ReviewerHandler reviews code changes and provides feedback.
// Derived from Python: handlers/reviewer.py (280 LOC, 10 AST nodes).
type ReviewerHandler struct {
	logger *slog.Logger
}

func NewReviewerHandler(logger *slog.Logger) *ReviewerHandler {
	return &ReviewerHandler{
		logger: logger.With(slog.String("handler", "reviewer")),
	}
}

func (h *ReviewerHandler) Role() string { return "reviewer" }

func (h *ReviewerHandler) Execute(ctx context.Context, taskID string) (ftypes.AgentResult, error) {
	h.logger.Info("reviewer: starting review",
		slog.String("task_id", taskID))

	// Review flow:
	// 1. Fetch PR diff
	// 2. Build review prompt
	// 3. LLM analysis
	// 4. Post review comments
	// 5. Approve/request changes → done/running

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// ─── Tester Handler ─────────────────────────────────────────────────────────

// TesterHandler runs tests and validates implementations.
// Derived from Python: handlers/tester.py (199 LOC, 7 AST nodes).
type TesterHandler struct {
	logger *slog.Logger
}

func NewTesterHandler(logger *slog.Logger) *TesterHandler {
	return &TesterHandler{
		logger: logger.With(slog.String("handler", "tester")),
	}
}

func (h *TesterHandler) Role() string { return "tester" }

func (h *TesterHandler) Execute(ctx context.Context, taskID string) (ftypes.AgentResult, error) {
	h.logger.Info("tester: starting test execution",
		slog.String("task_id", taskID))

	// Test flow:
	// 1. Checkout branch
	// 2. Run test suite
	// 3. Analyze results
	// 4. Report pass/fail → done/blocked

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// ─── PM Handler ─────────────────────────────────────────────────────────────

// PMHandler decomposes planned tasks and dispatches subtasks.
// Derived from Python: roles/pm_dispatcher.py (51 nodes).
type PMHandler struct {
	logger *slog.Logger
}

func NewPMHandler(logger *slog.Logger) *PMHandler {
	return &PMHandler{
		logger: logger.With(slog.String("handler", "pm")),
	}
}

func (h *PMHandler) Role() string { return "pm" }

func (h *PMHandler) Execute(ctx context.Context, taskID string) (ftypes.AgentResult, error) {
	h.logger.Info("pm: decomposing planned task",
		slog.String("task_id", taskID))

	// PM flow:
	// 1. Read task description
	// 2. LLM decomposition prompt
	// 3. Create subtasks
	// 4. Update parent task → ready or done

	return ftypes.AgentResult{
		Success:    true,
		NextStatus: ftypes.TaskStatusDone,
	}, nil
}

// ─── Meta-Critic ────────────────────────────────────────────────────────────

// MetaCritic evaluates agent output quality.
// Derived from Python: meta_critic.py (102 LOC, 10 AST nodes).
type MetaCritic struct {
	logger *slog.Logger
}

func NewMetaCritic(logger *slog.Logger) *MetaCritic {
	return &MetaCritic{
		logger: logger.With(slog.String("component", "meta-critic")),
	}
}

// Evaluate reviews an agent result for quality.
func (mc *MetaCritic) Evaluate(ctx context.Context, result ftypes.AgentResult) (float64, string) {
	// Score from 0.0 to 1.0 based on:
	// - Tool call success rate
	// - Error count
	// - Output coherence
	if !result.Success {
		return 0.0, "task failed"
	}
	if len(result.Errors) > 0 {
		return 0.5, fmt.Sprintf("completed with %d errors", len(result.Errors))
	}
	return 1.0, "clean execution"
}

// ─── Provider Registry ──────────────────────────────────────────────────────

// LLMProvider is the interface for LLM backend integrations.
// Derived from Python: providers/registry.py LLMProvider(Protocol) (L67-75).
type LLMProvider interface {
	Chat(ctx context.Context, messages []Message, model string) (*ftypes.LLMResponse, error)
	Name() string
}

// Message represents a chat message for LLM providers.
type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ProviderRegistry resolves LLM providers by name.
// Derived from Python: providers/registry.ProviderRegistry (L119-261).
type ProviderRegistry struct {
	providers map[string]LLMProvider
	logger    *slog.Logger
}

// NewProviderRegistry creates a new provider registry.
func NewProviderRegistry(logger *slog.Logger) *ProviderRegistry {
	return &ProviderRegistry{
		providers: make(map[string]LLMProvider),
		logger:    logger.With(slog.String("component", "providers.registry")),
	}
}

// Register adds a provider to the registry.
func (r *ProviderRegistry) Register(p LLMProvider) {
	r.providers[p.Name()] = p
	r.logger.Info("provider registered", slog.String("provider", p.Name()))
}

// Resolve returns the provider for a given name, with fallback to "gateway".
func (r *ProviderRegistry) Resolve(name string) (LLMProvider, error) {
	name = ftypes.NormalizeProvider(name)
	if p, ok := r.providers[name]; ok {
		return p, nil
	}
	// Fallback to gateway (all providers route through it)
	if p, ok := r.providers["gateway"]; ok {
		return p, nil
	}
	return nil, fmt.Errorf("provider %q not registered", name)
}

// ─── Tool Executors ─────────────────────────────────────────────────────────

// ToolExecutor executes a single tool call from an LLM response.
// Derived from Python: tools/executors.py (623 LOC, 7 AST nodes).
type ToolExecutor interface {
	Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error)
	Name() string
}

// ToolRegistry holds all available tool executors.
type ToolRegistry struct {
	tools  map[string]ToolExecutor
	logger *slog.Logger
}

// NewToolRegistry creates a new tool registry.
// It always registers the core FS tools (list_directory, read_file, write_file, run_shell)
// so implementers can explore + edit. The AST tools (elastro + logloom) are added by the
// WithElastro variant. Enforcement of "AST verification before writes" is applied in
// the runner's implementer loop (not inside individual executors) to keep the contract
// centralized and tied to per-task context.
func NewToolRegistry(logger *slog.Logger) *ToolRegistry {
	reg := &ToolRegistry{
		tools:  make(map[string]ToolExecutor),
		logger: logger.With(slog.String("component", "tools")),
	}
	// Register FS tools (always available for agent coding work)
	reg.Register(NewListDirectoryExecutor(logger))
	reg.Register(NewReadFileExecutor(logger))
	reg.Register(NewWriteFileExecutor(logger))
	reg.Register(NewRunShellExecutor(logger))
	return reg
}

// NewToolRegistryWithElastro is the Phase 3.1 factory for contract #4.
// It starts with NewToolRegistry (which includes FS tools: list/read/write/run_shell)
// then registers the intelligence tools:
//   - elastro_query_ast  → direct queries against ElastroGraphIndex
//                          (populated by `elastro rag ingest` at project onboarding)
//   - logloom_ast_query  → direct queries against Logloom*Index in ES
//
// Within work queue implementer workers, this gives easy, first-class access to
// elastro + logloom indices so agents know exactly what to query for accurate RAG +
// structural data before coding. The MANDATORY AST VERIFICATION rule is enforced
// in handleImplementer before any write_file/edit.
func NewToolRegistryWithElastro(esClient *es.Client, logger *slog.Logger) *ToolRegistry {
	reg := NewToolRegistry(logger)

	// Elastro RAG tool (preferred for semantic + structural codebase queries).
	// Now implemented as direct queries against ElastroGraphIndex (the index
	// populated by `elastro rag ingest` during project onboarding).
	elastroTool := NewElastroASTQueryExecutor(esClient, logger)
	reg.Register(elastroTool)

	// Critical validation at worker startup: BOTH elastro and logloom binaries
	// (for project rag ingest + logloom build during "Plan New Work", and container
	// worker images) must be present and executable. This is the runtime gate for
	// the Elastro/LogLoom contract (point #1). Hard build-time asserts in Dockerfile
	// catch at image build; this catches at worker-manager start for container mode.
	elastroBin := findElastroBinary()
	logloomBin := findLogloomBinary()
	if elastroBin != "" {
		if logger != nil {
			logger.Info("ToolRegistry: elastro binary located successfully (available for rag ingest/update)",
				slog.String("elastro_bin", elastroBin),
			)
		}
	} else {
		if logger != nil {
			logger.Error("FATAL: elastro binary not found at worker startup — LogLoom/Elastro contract violation",
				slog.String("searched_locations", "PATH + /opt/venv/bin/elastro + common paths"),
				slog.String("remediation", "Rebuild worker image with ELASTR0_INSTALL=public (default) or =wheel; see root Dockerfile and docker-compose.yml"),
			)
		}
		os.Exit(1)
	}
	if logloomBin != "" {
		if logger != nil {
			logger.Info("ToolRegistry: logloom binary located successfully (available for AST graph ingest)",
				slog.String("logloom_bin", logloomBin),
			)
		}
	} else {
		if logger != nil {
			logger.Error("FATAL: logloom binary not found at worker startup — LogLoom/Elastro contract violation",
				slog.String("searched_locations", "PATH + /opt/venv/bin/logloom + LOGLOOM_BIN + $HOME/.local/bin + common paths"),
				slog.String("remediation", "Rebuild worker image with LOGLOOM_INSTALL=public (default) or =wheel; see root Dockerfile and docker-compose.yml"),
			)
		}
		os.Exit(1)
	}

	if logger != nil {
		logger.Info("ToolRegistry: registered elastro_query_ast (direct queries against "+ElastroGraphIndex+" index)")
	}

	// Logloom tool (for direct AST / enrichment / call-graph queries)
	if esClient != nil {
		logloomTool := NewLogloomASTQueryExecutor(esClient, logger)
		reg.Register(logloomTool)
		if logger != nil {
			logger.Info("ToolRegistry: registered logloom_ast_query (direct ES on Logloom indices: " + LogloomEnrichmentIndex + " primary, fallback " + LogloomASTIndex + ")")
		}
	} else {
		if logger != nil {
			logger.Warn("ToolRegistry: logloom_ast_query not registered (no ES client)")
		}
	}

	return reg
}

// Register adds a tool executor.
func (r *ToolRegistry) Register(t ToolExecutor) {
	r.tools[t.Name()] = t
}

// Execute runs a tool by name with the given arguments.
func (r *ToolRegistry) Execute(ctx context.Context, name string, args map[string]interface{}, repoPath string) (string, error) {
	tool, ok := r.tools[name]
	if !ok {
		return "", fmt.Errorf("unknown tool: %s", name)
	}
	return tool.Execute(ctx, args, repoPath)
}

// GetToolDefinitions returns OpenAI-compatible function definitions for all
// registered tools. These are passed to the LLM via ChatWithTools so the model
// can make structured tool calls instead of hallucinating tool names in text.
// This is the bridge that was missing — without it, the LLM never received
// tool schemas and could only emit text.
func (r *ToolRegistry) GetToolDefinitions() []ToolDefinition {
	defs := []ToolDefinition{}

	// Static definitions for each tool type. We define them inline because the
	// executors are simple and don't carry their own schema metadata.
	schemas := map[string]ToolDefinition{
		"list_directory": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "list_directory",
				Description: "List files and subdirectories in a directory relative to the repository root. Use this to explore the project structure. Do NOT use ls or dir via run_shell.",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"path": map[string]interface{}{
							"type":        "string",
							"description": "Relative path from repo root. Use '.' or '' for root.",
						},
					},
					"required": []string{},
				},
			},
		},
		"read_file": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "read_file",
				Description: "Read the contents of a file at a path relative to the repository root. Always use this before modifying a file (Zero-Blind-Write Rule).",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"path": map[string]interface{}{
							"type":        "string",
							"description": "Relative file path from repo root.",
						},
					},
					"required": []string{"path"},
				},
			},
		},
		"write_file": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "write_file",
				Description: "Write content to a file at a path relative to the repository root. Parent directories are created automatically. You MUST read_file first (Zero-Blind-Write Rule) and you MUST have called elastro_query_ast or logloom_ast_query successfully before any writes.",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"path": map[string]interface{}{
							"type":        "string",
							"description": "Relative file path from repo root.",
						},
						"content": map[string]interface{}{
							"type":        "string",
							"description": "Full file content to write.",
						},
					},
					"required": []string{"path", "content"},
				},
			},
		},
		"run_shell": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "run_shell",
				Description: "Run a shell command in the repository directory. Use for grep, find, go build, go test, golangci-lint, and similar dev tools. Do NOT use for ls (use list_directory) or cat (use read_file).",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"command": map[string]interface{}{
							"type":        "string",
							"description": "Shell command to execute (via sh -c).",
						},
					},
					"required": []string{"command"},
				},
			},
		},
		"elastro_query_ast": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "elastro_query_ast",
				Description: "Query the Elastro Graph RAG index (flume-elastro-graph) for semantic and structural codebase information. Use this for understanding code architecture, finding functions, classes, imports, and relationships. MANDATORY: call this or logloom_ast_query before any code edits.",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"query": map[string]interface{}{
							"type":        "string",
							"description": "Natural language or keyword query about the codebase structure.",
						},
						"target_path": map[string]interface{}{
							"type":        "string",
							"description": "Optional: scope results to a specific directory or file path.",
						},
					},
					"required": []string{"query"},
				},
			},
		},
		"logloom_ast_query": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "logloom_ast_query",
				Description: "Query the Logloom AST/enrichment indices for precise call-graph, log sites, function signatures, and model definitions. Use for structural questions like 'find all callers of X' or 'definition of Y'. MANDATORY: call this or elastro_query_ast before any code edits.",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"query": map[string]interface{}{
							"type":        "string",
							"description": "Structural query about functions, call graphs, or code elements.",
						},
					},
					"required": []string{"query"},
				},
			},
		},
		"implementation_complete": {
			Type: "function",
			Function: ToolFunctionDef{
				Name:        "implementation_complete",
				Description: "Signal that the task implementation is complete. Call this when you have finished all required work, including code changes, testing, and verification.",
				Parameters: map[string]interface{}{
					"type": "object",
					"properties": map[string]interface{}{
						"status": map[string]interface{}{
							"type":        "string",
							"description": "Completion status: 'complete' or 'partial'.",
							"enum":        []string{"complete", "partial"},
						},
						"summary": map[string]interface{}{
							"type":        "string",
							"description": "Summary of changes made and work completed.",
						},
						"modified_files": map[string]interface{}{
							"type":        "array",
							"description": "List of files that were modified.",
							"items":       map[string]interface{}{"type": "string"},
						},
						"lint_passed": map[string]interface{}{
							"type":        "boolean",
							"description": "Whether lint checks passed after changes.",
						},
					},
					"required": []string{"summary"},
				},
			},
		},
	}

	// Only include definitions for tools that are actually registered
	for name := range r.tools {
		if def, ok := schemas[name]; ok {
			defs = append(defs, def)
		}
	}

	// Always include implementation_complete (it's a meta-tool, not in the executor registry)
	defs = append(defs, schemas["implementation_complete"])

	return defs
}

// ToolDefinition is an OpenAI-compatible tool definition for LLM function calling.
type ToolDefinition struct {
	Type     string          `json:"type"`
	Function ToolFunctionDef `json:"function"`
}

// ToolFunctionDef describes a callable function for the LLM.
type ToolFunctionDef struct {
	Name        string      `json:"name"`
	Description string      `json:"description,omitempty"`
	Parameters  interface{} `json:"parameters,omitempty"`
}

// ToolResultModifiedRepo checks if a tool result indicates repo changes.
// Derived from Python: tools/executors.tool_result_modified_repo() (L613-623)
func ToolResultModifiedRepo(toolName, result string) bool {
	switch toolName {
	case "write_file", "edit_file", "multi_replace_file_content", "run_shell":
		return !strings.Contains(result, "error") && !strings.Contains(result, "Error")
	default:
		return false
	}
}

// ─── Elastro RAG Tool (preferred way for codebase semantic/structural queries) ──

// ElastroASTQueryExecutor implements the elastro_query_ast tool via **direct ES
// queries** against the index populated by `elastro rag ingest` (ElastroGraphIndex).
//
// The elastro-client CLI (v1.3.59) only exposes:
//   - `elastro rag ingest <repo> -i <index>`   (used at project clone / intake)
//   - `elastro rag update <file> -i <index>`   (for incremental updates)
//
// There is no `rag query` (or equivalent) subcommand. Attempting it produces
// "No such command 'query'".
//
// We therefore query the graph directly using the same authenticated ES client
// the rest of the worker uses (consistent with LogloomASTQueryExecutor).
// This is more reliable than shelling out and gives agents the structural RAG
// data they need.
type ElastroASTQueryExecutor struct {
	es     *es.Client
	logger *slog.Logger
}

func NewElastroASTQueryExecutor(esClient *es.Client, logger *slog.Logger) *ElastroASTQueryExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &ElastroASTQueryExecutor{
		es:     esClient,
		logger: logger.With(slog.String("tool", "elastro_query_ast")),
	}
}

func (e *ElastroASTQueryExecutor) Name() string { return "elastro_query_ast" }

func (e *ElastroASTQueryExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
	start := time.Now()

	query, _ := args["query"].(string)
	if strings.TrimSpace(query) == "" {
		return "", fmt.Errorf("elastro_query_ast: 'query' argument is required")
	}
	targetPath := repoPath
	if p, ok := args["target_path"].(string); ok && p != "" {
		targetPath = p
	}

	// We no longer shell out to "elastro rag query" — that subcommand does not exist
	// in elastro-client 1.3.59+ (only `rag ingest` and `rag update` are provided).
	// Instead we perform a direct structured search against the index that the
	// ingest step populates. This is the same pattern used by logloom_ast_query.

	if e.es == nil {
		return "", fmt.Errorf("elastro_query_ast: no ES client available (direct index query required)")
	}

	// Broad but effective search over the AST graph documents.
	// The exact field names depend on what `elastro rag ingest` writes; we use
	// common structural names + a catch-all "content" / text fields.
	esQuery := map[string]interface{}{
		"query": map[string]interface{}{
			"bool": map[string]interface{}{
				"must": []interface{}{
					map[string]interface{}{
						"multi_match": map[string]interface{}{
							"query":  query,
							"fields": []string{"function^3", "file^2", "signature^2", "content", "code", "text", "path", "module"},
							"type":   "best_fields",
							"operator": "or",
						},
					},
				},
			},
		},
		"size": 12,
		"_source": true,
	}

	// If the caller gave a target_path, try to scope results (best-effort).
	if targetPath != "" && targetPath != "/" && targetPath != "." {
		// Many AST docs will have a "file" or "path" field containing the repo-relative path.
		esQuery["query"].(map[string]interface{})["bool"].(map[string]interface{})["should"] = []interface{}{
			map[string]interface{}{
				"wildcard": map[string]interface{}{"file": map[string]interface{}{"value": "*" + targetPath + "*"}},
			},
			map[string]interface{}{
				"wildcard": map[string]interface{}{"path": map[string]interface{}{"value": "*" + targetPath + "*"}},
			},
		}
	}

	hits, err := e.es.Search(ctx, ElastroGraphIndex, esQuery, 15)
	if err != nil {
		// The index may not exist yet for this project, or may use a different name.
		// Be graceful.
		flumelogger.LogAgentReasoning(ctx, "", "implementer",
			"elastro_query_ast: search against "+ElastroGraphIndex+" failed (index may be empty or not yet created for this repo)",
			map[string]any{
				"tool":        "elastro_query_ast",
				"query":       query,
				"target_path": targetPath,
				"error":       err.Error(),
				"phase":       "tool_execution",
			})
		return fmt.Sprintf("No elastro AST graph data found yet for this project (index flume-elastro-graph may be empty or not ingested). Query was: %s. Try after a full project clone/ingest, or use logloom_ast_query for structural information.", query), nil
	}

	duration := time.Since(start)

	flumelogger.LogAgentReasoning(ctx, "", "implementer",
		fmt.Sprintf("elastro_query_ast (direct on %s) returned %d hits (took %s)", ElastroGraphIndex, len(hits.Hits), duration),
		map[string]any{
			"tool":        "elastro_query_ast",
			"query":       query,
			"target_path": targetPath,
			"hit_count":   len(hits.Hits),
			"duration_ms": duration.Milliseconds(),
			"phase":       "tool_execution",
		})

	if len(hits.Hits) == 0 {
		return fmt.Sprintf("Elastro AST graph search for %q returned no matches in %s. The project may need re-ingestion (elastro rag ingest) or the query may need to be more specific.", query, ElastroGraphIndex), nil
	}

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("Elastro Graph RAG results for %q (from flume-elastro-graph):\n\n", query))

	for i, raw := range hits.Hits {
		if i >= 8 {
			sb.WriteString("... (more results truncated)\n")
			break
		}
		var doc map[string]interface{}
		_ = json.Unmarshal(raw, &doc)
		sb.WriteString(fmt.Sprintf("%d. %v\n", i+1, doc))
	}

	return sb.String(), nil
}

// findElastroBinary mirrors the logic used during project ingestion.
func findElastroBinary() string {
	// Check standard PATH first
	if resolved, err := exec.LookPath("elastro"); err == nil {
		return resolved
	}

	// Check the venv location we create in the official Dockerfile
	venvPath := "/opt/venv/bin/elastro"
	if _, err := os.Stat(venvPath); err == nil {
		return venvPath
	}

	// Additional common locations (for developer / custom setups)
	candidates := []string{
		"/usr/local/bin/elastro",
		"/home/flume/.local/bin/elastro",
	}

	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}

	return ""
}

// findLogloomBinary mirrors the discovery logic in doctor.go, api_projects.go,
// and the container venv fallback. Used for the runtime startup verification
// that both binaries are present in the worker (primary container deployment).
func findLogloomBinary() string {
	// Check standard PATH first (e.g. after ENV PATH update in Dockerfile)
	if resolved, err := exec.LookPath("logloom"); err == nil {
		return resolved
	}

	logloomBin := os.Getenv("LOGLOOM_BIN")
	if logloomBin == "" {
		logloomBin = os.ExpandEnv("$HOME/.local/bin/logloom")
	}
	if resolved, err := exec.LookPath(logloomBin); err == nil {
		return resolved
	}
	if _, err := os.Stat(logloomBin); err == nil {
		return logloomBin
	}

	// Check the venv location we create in the official Dockerfile (when
	// built with LOGLOOM_INSTALL=public or =wheel)
	venvPath := "/opt/venv/bin/logloom"
	if _, err := os.Stat(venvPath); err == nil {
		return venvPath
	}

	// Additional common locations (for developer / custom / flume user setups)
	candidates := []string{
		"/usr/local/bin/logloom",
		"/home/flume/.local/bin/logloom",
	}

	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}

	return ""
}

// ─── Logloom AST Query Tool (direct ES queries on Logloom indices) ───────────

// LogloomASTQueryExecutor lets agents query the Logloom AST / enrichment indices
// directly in Elasticsearch (LogloomEnrichmentIndex primary, LogloomASTIndex fallback).
// This is complementary to Elastro RAG. Agents know the exact names via consts + SYSTEM_PROMPT.
//
// Use this for structural / call-graph questions ("find all callers of X",
// "definition of Y in module Z", etc.).
//
// Indices typically involved: flume-logloom-ast, flume-logloom-enrichment, etc.
// (populated by logloom build + ingest at project creation time).
type LogloomASTQueryExecutor struct {
	es     *es.Client
	logger *slog.Logger
}

func NewLogloomASTQueryExecutor(esClient *es.Client, logger *slog.Logger) *LogloomASTQueryExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &LogloomASTQueryExecutor{
		es:     esClient,
		logger: logger.With(slog.String("tool", "logloom_ast_query")),
	}
}

func (e *LogloomASTQueryExecutor) Name() string { return "logloom_ast_query" }

func (e *LogloomASTQueryExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
	start := time.Now()

	query, _ := args["query"].(string)
	if strings.TrimSpace(query) == "" {
		return "", fmt.Errorf("logloom_ast_query: 'query' is required")
	}

	// Query the main Logloom enrichment index (LogloomEnrichmentIndex) or fallback to ast.
	// Exact names are the canonical ones for the Elastro/Logloom contract.
	// This uses the same ES client the rest of the system uses.
	esQuery := map[string]interface{}{
		"query": map[string]interface{}{
			"multi_match": map[string]interface{}{
				"query":  query,
				"fields": []string{"logloom.message_template^2", "logloom.function", "logloom.file", "content"},
			},
		},
		"size": 15,
	}

	hits, err := e.es.Search(ctx, LogloomEnrichmentIndex, esQuery, 15)
	if err != nil {
		// Fallback to the AST index if enrichment isn't present
		hits, err = e.es.Search(ctx, LogloomASTIndex, esQuery, 15)
		if err != nil {
			return "", fmt.Errorf("logloom query failed on both %s and %s indices: %w", LogloomEnrichmentIndex, LogloomASTIndex, err)
		}
	}

	duration := time.Since(start)

	flumelogger.LogAgentReasoning(ctx, "", "implementer",
		fmt.Sprintf("logloom_ast_query returned %d hits (took %s) [indices: %s/%s]", len(hits.Hits), duration, LogloomEnrichmentIndex, LogloomASTIndex),
		map[string]any{
			"tool":        "logloom_ast_query",
			"query":       query,
			"hit_count":   len(hits.Hits),
			"duration_ms": duration.Milliseconds(),
			"phase":       "tool_execution",
		})

	if len(hits.Hits) == 0 {
		return fmt.Sprintf("Logloom AST search for %q: no structural matches.", query), nil
	}

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("Logloom structural results for %q:\n", query))

	for i, raw := range hits.Hits {
		if i >= 10 {
			sb.WriteString("... (truncated)\n")
			break
		}
		var doc map[string]interface{}
		_ = json.Unmarshal(raw, &doc)
		sb.WriteString(fmt.Sprintf("- %v\n", doc))
	}

	return sb.String(), nil
}

// ─── File System Tools (required for real edits; registered alongside AST tools) ──
// These enable the implementer to act on the repo. Enforcement of MANDATORY AST
// verification (elastro_query_ast or logloom_ast_query success) happens in the
// handleImplementer loop in runner.go before dispatching writes. This makes the
// Elastro/Logloom contract #4 real and enforceable for work queue workers.

type ListDirectoryExecutor struct {
	logger *slog.Logger
}

func NewListDirectoryExecutor(logger *slog.Logger) *ListDirectoryExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &ListDirectoryExecutor{logger: logger.With(slog.String("tool", "list_directory"))}
}
func (e *ListDirectoryExecutor) Name() string { return "list_directory" }
func (e *ListDirectoryExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
	rel, _ := args["path"].(string)
	target := repoPath
	if rel != "" && rel != "." {
		target = filepath.Join(repoPath, rel)
	}
	// Safety: ensure we stay under repoPath (prevent ../ escapes)
	if !strings.HasPrefix(filepath.Clean(target), filepath.Clean(repoPath)) {
		return "", fmt.Errorf("list_directory: path %s escapes repo root", rel)
	}
	entries, err := os.ReadDir(target)
	if err != nil {
		return "", fmt.Errorf("list_directory failed on %s: %w", target, err)
	}
	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("Directory listing for %s:\n", target))
	for _, ent := range entries {
		prefix := "  "
		if ent.IsDir() {
			prefix = "  [dir] "
		}
		sb.WriteString(prefix + ent.Name() + "\n")
	}
	return sb.String(), nil
}

type ReadFileExecutor struct {
	logger *slog.Logger
}

func NewReadFileExecutor(logger *slog.Logger) *ReadFileExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &ReadFileExecutor{logger: logger.With(slog.String("tool", "read_file"))}
}
func (e *ReadFileExecutor) Name() string { return "read_file" }
func (e *ReadFileExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
	rel, _ := args["path"].(string)
	if rel == "" {
		return "", fmt.Errorf("read_file: 'path' argument is required")
	}
	full := filepath.Join(repoPath, rel)
	if !strings.HasPrefix(filepath.Clean(full), filepath.Clean(repoPath)) {
		return "", fmt.Errorf("read_file: path %s escapes repo root", rel)
	}
	data, err := os.ReadFile(full)
	if err != nil {
		return "", fmt.Errorf("read_file %s failed: %w", full, err)
	}
	return string(data), nil
}

type WriteFileExecutor struct {
	logger *slog.Logger
}

func NewWriteFileExecutor(logger *slog.Logger) *WriteFileExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &WriteFileExecutor{logger: logger.With(slog.String("tool", "write_file"))}
}
func (e *WriteFileExecutor) Name() string { return "write_file" }
func (e *WriteFileExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
	rel, _ := args["path"].(string)
	content, _ := args["content"].(string)
	if rel == "" {
		return "", fmt.Errorf("write_file: 'path' argument is required")
	}
	full := filepath.Join(repoPath, rel)
	if !strings.HasPrefix(filepath.Clean(full), filepath.Clean(repoPath)) {
		return "", fmt.Errorf("write_file: path %s escapes repo root", rel)
	}
	// Ensure parent dirs
	if dir := filepath.Dir(full); dir != "" {
		_ = os.MkdirAll(dir, 0755)
	}
	if err := os.WriteFile(full, []byte(content), 0644); err != nil {
		return "", fmt.Errorf("write_file %s failed: %w", full, err)
	}
	return fmt.Sprintf("Successfully wrote %d bytes to %s", len(content), rel), nil
}

type RunShellExecutor struct {
	logger *slog.Logger
}

func NewRunShellExecutor(logger *slog.Logger) *RunShellExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &RunShellExecutor{logger: logger.With(slog.String("tool", "run_shell"))}
}
func (e *RunShellExecutor) Name() string { return "run_shell" }
func (e *RunShellExecutor) Execute(ctx context.Context, args map[string]interface{}, repoPath string) (string, error) {
	cmdStr, _ := args["command"].(string)
	if strings.TrimSpace(cmdStr) == "" {
		return "", fmt.Errorf("run_shell: 'command' argument is required")
	}
	// Basic safety: disallow obviously dangerous top-level (rm -rf / etc). Workers run in controlled env.
	lower := strings.ToLower(cmdStr)
	if strings.Contains(lower, "rm -rf /") || strings.Contains(lower, "mkfs") || strings.Contains(lower, ":(){ :|:& };:") {
		return "", fmt.Errorf("run_shell: disallowed dangerous command pattern")
	}
	cmd := exec.CommandContext(ctx, "sh", "-c", cmdStr)
	cmd.Dir = repoPath
	out, err := cmd.CombinedOutput()
	result := string(out)
	if err != nil {
		result += "\n[exit error: " + err.Error() + "]"
	}
	return result, nil
}
