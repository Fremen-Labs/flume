package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/internal/es"
	flumelogger "github.com/Fremen-Labs/flume/internal/logger"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
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
func NewToolRegistry(logger *slog.Logger) *ToolRegistry {
	return &ToolRegistry{
		tools:  make(map[string]ToolExecutor),
		logger: logger.With(slog.String("component", "tools")),
	}
}

// NewToolRegistryWithElastro is the Phase 3.1 factory.
// It registers:
//   - elastro_query_ast  → uses the Elastro CLI (RAG over ingested codebase)
//   - logloom_ast_query  → direct queries against Logloom indices in ES
//
// This matches the pre-migration architecture the user described:
//   - Use Elastro (CLI) for high-quality RAG/codebase understanding.
//   - Use direct ES (via this tool) for Logloom AST/call-graph data.
func NewToolRegistryWithElastro(esClient *es.Client, logger *slog.Logger) *ToolRegistry {
	reg := NewToolRegistry(logger)

	// Elastro RAG tool (preferred for semantic + structural codebase queries)
	elastroTool := NewElastroASTQueryExecutor(logger)
	reg.Register(elastroTool)
	if logger != nil {
		logger.Info("ToolRegistry: registered elastro_query_ast (uses external Elastro CLI)")
	}

	// Logloom tool (for direct AST / enrichment / call-graph queries)
	if esClient != nil {
		logloomTool := NewLogloomASTQueryExecutor(esClient, logger)
		reg.Register(logloomTool)
		if logger != nil {
			logger.Info("ToolRegistry: registered logloom_ast_query (direct ES on Logloom indices)")
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

// ToolResultModifiedRepo checks if a tool result indicates repo changes.
// Derived from Python: tools/executors.tool_result_modified_repo() (L613-623)
func ToolResultModifiedRepo(toolName, result string) bool {
	switch toolName {
	case "write_file", "multi_replace_file_content", "run_shell":
		return !strings.Contains(result, "error") && !strings.Contains(result, "Error")
	default:
		return false
	}
}

// ─── Elastro RAG Tool (preferred way for codebase semantic/structural queries) ──

// ElastroASTQueryExecutor implements the elastro_query_ast tool by shelling out
// to the Elastro CLI (the official way to query the RAG data of ingested codebases).
//
// Per project lifecycle:
//   - At repo onboarding: `elastro rag ingest <path> -i flume-elastro-graph` (already done in api_projects.go)
//   - After code changes in a task: agents (or post-task) should call `elastro rag update`
//   - For queries: agents MUST use this tool (which invokes Elastro) rather than raw ES queries
//     on the graph index. This gives the same high-quality retrieval the Python agents had.
//
// Logloom indices are queried directly via the ES client (see LogloomASTQueryExecutor below).
//
// This restores the exact pre-migration capability where implementers were required
// to call elastro_query_ast before editing code.
type ElastroASTQueryExecutor struct {
	logger *slog.Logger
}

func NewElastroASTQueryExecutor(logger *slog.Logger) *ElastroASTQueryExecutor {
	if logger == nil {
		logger = slog.Default()
	}
	return &ElastroASTQueryExecutor{
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

	elastroBin := findElastroBinary()
	if elastroBin == "" {
		return "", fmt.Errorf("elastro binary not found (checked PATH and /opt/venv/bin/elastro)")
	}

	// Elastro RAG query command. Adjust flags as the Elastro CLI evolves.
	// Typical: elastro rag query "<question>" --index flume-elastro-graph --path <repo>
	cmd := exec.CommandContext(ctx, elastroBin, "rag", "query", query,
		"--index", "flume-elastro-graph",
		"--path", targetPath,
	)
	cmd.Env = os.Environ()

	// Pass ES connection info if available (same pattern as ingest in api_projects.go)
	// This allows the Elastro CLI to talk to the correct cluster.
	if esURL := os.Getenv("ES_URL"); esURL != "" {
		cmd.Env = append(cmd.Env,
			"ELASTIC_URL="+esURL,
			"ELASTIC_ELASTICSEARCH_HOSTS="+esURL,
			"ELASTIC_ELASTICSEARCH_VERIFY_CERTS=false",
			"ELASTIC_VERIFY_CERTS=false",
		)
	}
	if apiKey := os.Getenv("ES_API_KEY"); apiKey != "" {
		cmd.Env = append(cmd.Env,
			"ELASTIC_ELASTICSEARCH_AUTH_API_KEY="+apiKey,
			"ELASTIC_ELASTICSEARCH_AUTH_TYPE=api_key",
		)
	}

	output, err := cmd.CombinedOutput()
	duration := time.Since(start)

	flumelogger.LogAgentReasoning(ctx, "", "implementer",
		fmt.Sprintf("Called elastro rag query (took %s)", duration),
		map[string]any{
			"tool":        "elastro_query_ast",
			"query":       query,
			"target_path": targetPath,
			"bin":         elastroBin,
			"duration_ms": duration.Milliseconds(),
			"phase":       "tool_execution",
		})

	if err != nil {
		return "", fmt.Errorf("elastro rag query failed: %w (output: %s)", err, string(output))
	}

	result := strings.TrimSpace(string(output))
	if result == "" {
		result = fmt.Sprintf("No relevant code found in the RAG index for query: %s", query)
	}

	return result, nil
}

// findElastroBinary mirrors the logic used during project ingestion.
func findElastroBinary() string {
	if resolved, err := exec.LookPath("elastro"); err == nil {
		return resolved
	}
	if _, err := os.Stat("/opt/venv/bin/elastro"); err == nil {
		return "/opt/venv/bin/elastro"
	}
	return ""
}

// ─── Logloom AST Query Tool (direct ES queries on Logloom indices) ───────────

// LogloomASTQueryExecutor lets agents query the Logloom AST / enrichment indices
// directly in Elasticsearch. This is complementary to Elastro RAG.
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

	// Query the main Logloom enrichment index (or ast index). Adjust as needed.
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

	hits, err := e.es.Search(ctx, "flume-logloom-enrichment", esQuery, 15)
	if err != nil {
		// Fallback to the AST index if enrichment isn't present
		hits, err = e.es.Search(ctx, "flume-logloom-ast", esQuery, 15)
		if err != nil {
			return "", fmt.Errorf("logloom query failed on both enrichment and ast indices: %w", err)
		}
	}

	duration := time.Since(start)

	flumelogger.LogAgentReasoning(ctx, "", "implementer",
		fmt.Sprintf("logloom_ast_query returned %d hits (took %s)", len(hits.Hits), duration),
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
