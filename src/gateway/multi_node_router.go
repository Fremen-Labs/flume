package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Multi-Node Router — Capability-aware routing across the Ollama node mesh.
//
// Wraps ProviderRouter to add intelligent node selection for Ollama requests.
// Non-Ollama providers (OpenAI, Anthropic, Gemini, xAI) pass through directly
// to ProviderRouter.Route() without any node mesh logic.
//
// Routing algorithm:
//   1. Query NodeRegistry.HealthyNodes()
//   2. If no healthy nodes → frontier fallback
//   3. Score each node via weighted formula (model fit, load, latency, ensemble)
//   4. Select top-scoring node, override Ollama base URL
//   5. Execute via ProviderRouter.Route()
//   6. On failure → try next-best → all-fail → frontier escalation
//
// All logging uses the gateway's slog-based structured logger.
// ─────────────────────────────────────────────────────────────────────────────

// MultiNodeRouter coordinates requests across the Ollama node mesh.
type MultiNodeRouter struct {
	router   *ProviderRouter
	registry *NodeRegistry
	config   *Config
}

// NewMultiNodeRouter creates a router with node mesh awareness.
func NewMultiNodeRouter(router *ProviderRouter, registry *NodeRegistry, config *Config) *MultiNodeRouter {
	return &MultiNodeRouter{
		router:   router,
		registry: registry,
		config:   config,
	}
}

// ExecuteSmartRoute selects the optimal routing path based on the active
// RoutingPolicy mode, then dispatches the request accordingly.
//
// Mode semantics:
//   - frontier_only: Bypass node mesh, use weighted frontier selection.
//   - hybrid:        Complexity + probability gate decides frontier vs local.
//   - local_only:    Existing node mesh routing (default, zero-regression).
func (m *MultiNodeRouter) ExecuteSmartRoute(ctx context.Context, req *ChatRequest, taskType string, withTools bool) (*ChatResponse, error) {
	log := WithContext(ctx)
	policy := m.config.GetRoutingPolicy()

	switch policy.Mode {
	case RoutingModeFrontierOnly:
		return m.executeFrontierOnly(ctx, req, taskType, withTools, policy)

	case RoutingModeHybrid:
		return m.executeHybrid(ctx, req, taskType, withTools, policy)

	default:
		// RoutingModeLocalOnly — existing behavior, zero changes.
		return m.executeLocalOnly(ctx, req, taskType, withTools, log)
	}
}

// executeFrontierOnly routes all requests to frontier models via weighted selection.
func (m *MultiNodeRouter) executeFrontierOnly(ctx context.Context, req *ChatRequest, taskType string, withTools bool, policy *RoutingPolicy) (*ChatResponse, error) {
	log := WithContext(ctx)

	model := policy.SelectWeightedFrontier(req.AgentRole)
	if model == nil {
		log.Error("routing_policy: frontier_only mode — no frontier models available (all circuit-broken)")
		return nil, fmt.Errorf("frontier_only mode: all frontier models are circuit-broken — no available endpoints")
	}

	log.Info("routing_policy: frontier_only dispatch",
		slog.String("model", model.Model),
		slog.String("provider", model.Provider),
		slog.String("agent_role", req.AgentRole),
		slog.String("task_type", taskType),
	)

	Metrics.RecordRoutingDecision("frontier_direct", taskType)

	cloned := cloneChatRequest(req)
	cloned.Model = model.Model
	cloned.Provider = model.Provider
	cloned.CredentialID = model.CredentialID

	resp, err := m.router.Route(ctx, cloned, withTools)
	if err != nil {
		return nil, err
	}

	// Enforce spend budget after every frontier response
	if resp != nil {
		policy.EnforceSpendBudget(model.Model, resp.Usage)
		policy.PersistSpendIfDue(ctx, m.config.esURL, m.config.httpClient)
	}

	return resp, nil
}

// executeHybrid uses complexity + probability gating to route between
// frontier and local nodes. Falls back to frontier on total mesh failure.
func (m *MultiNodeRouter) executeHybrid(ctx context.Context, req *ChatRequest, taskType string, withTools bool, policy *RoutingPolicy) (*ChatResponse, error) {
	log := WithContext(ctx)

	useFrontier := policy.ShouldUseFrontier(taskType) && policy.HasAvailableFrontierModels()

	if useFrontier {
		model := policy.SelectWeightedFrontier(req.AgentRole)
		if model != nil {
			log.Info("routing_policy: hybrid → frontier path",
				slog.String("model", model.Model),
				slog.String("provider", model.Provider),
				slog.String("task_type", taskType),
			)

			Metrics.RecordRoutingDecision("hybrid_frontier", taskType)

			cloned := cloneChatRequest(req)
			cloned.Model = model.Model
			cloned.Provider = model.Provider
			cloned.CredentialID = model.CredentialID

			resp, err := m.router.Route(ctx, cloned, withTools)
			if err != nil {
				log.Warn("routing_policy: hybrid frontier path failed, trying local mesh",
					slog.String("error", err.Error()),
				)
				// Fall through to local mesh routing
			} else {
				if resp != nil {
					policy.EnforceSpendBudget(model.Model, resp.Usage)
					policy.PersistSpendIfDue(ctx, m.config.esURL, m.config.httpClient)
				}
				return resp, nil
			}
		}
	}

	// Local mesh path — same as executeLocalOnly
	log.Info("routing_policy: hybrid → local mesh path",
		slog.String("task_type", taskType),
	)
	Metrics.RecordRoutingDecision("hybrid_local", taskType)
	
	resp, err := m.executeLocalOnly(ctx, req, taskType, withTools, log)
	if err != nil {
		// HA Fallback: If local mesh actively drops the request because all nodes are offline,
		// and we have a Frontier fallback standing by, execute high-availability routing!
		if policy.HasAvailableFrontierModels() {
			log.Warn("routing_policy: local mesh unavailable, executing HA failover to frontier",
				slog.String("error", err.Error()),
				slog.String("task_type", taskType),
			)
			fallbackModel := policy.SelectWeightedFrontier(req.AgentRole)
			if fallbackModel != nil {
				Metrics.RecordRoutingDecision("hybrid_ha_failover", taskType)
				
				cloned := cloneChatRequest(req)
				cloned.Model = fallbackModel.Model
				cloned.Provider = fallbackModel.Provider
				cloned.CredentialID = fallbackModel.CredentialID

				haResp, haErr := m.router.Route(ctx, cloned, withTools)
				if haResp != nil {
					policy.EnforceSpendBudget(fallbackModel.Model, haResp.Usage)
					policy.PersistSpendIfDue(ctx, m.config.esURL, m.config.httpClient)
				}
				return haResp, haErr
			}
		}
		// Return original error if frontier fails or is unavailable
		return nil, err
	}
	return resp, nil
}

// executeLocalOnly is the original ExecuteSmartRoute behavior — preserved exactly
// for zero functional regressions when mode=local_only.
func (m *MultiNodeRouter) executeLocalOnly(ctx context.Context, req *ChatRequest, taskType string, withTools bool, log *slog.Logger) (*ChatResponse, error) {
	// Resolve effective provider to decide if node mesh applies.
	_, provider, _ := m.config.ResolveModel(req)

	// Non-Ollama providers bypass the mesh entirely.
	if strings.ToLower(provider) != ProviderOllama {
		Metrics.RecordRoutingDecision("direct_provider", taskType)
		return m.router.Route(ctx, req, withTools)
	}

	// ── Node Mesh Routing ────────────────────────────────────────────────

	// If no nodes are registered, fall back to the default single-node path.
	if m.registry.Count() == 0 {
		Metrics.RecordRoutingDecision("single_node", taskType)
		return m.router.Route(ctx, req, withTools)
	}

	// Determine minimum reasoning score from task type.
	minScore := 0
	switch taskType {
	case "reasoning", "planning", "pm":
		minScore = 5
	case "implementation", "code":
		minScore = 3
	}

	// Determine if this task warrants a high-parameter backend.
	requiresHighParam := taskType == "code" || withTools

	// Select the best node.
	node := m.registry.SelectNode(taskType, minScore, requiresHighParam, withTools)
	if node == nil {
		// No healthy nodes meeting criteria → frontier fallback.
		log.Warn("multi_node_router: no suitable nodes — escalating to frontier",
			slog.String("task_type", taskType),
			slog.Int("min_reasoning", minScore),
			slog.Bool("requires_high_param", requiresHighParam),
		)
		Metrics.RecordRoutingDecision("frontier_no_nodes", taskType)
		return m.routeFrontierFallback(ctx, req, withTools)
	}

	// Route to selected node.
	log.Info("multi_node_router: routing to node",
		slog.String("node_id", node.ID),
		slog.String("host", node.Host),
		slog.String("model", node.ModelTag),
		slog.String("task_type", taskType),
		slog.Bool("with_tools", withTools),
		slog.Float64("load", node.Health.CurrentLoad),
		slog.Int64("latency_ms", node.Health.LatencyMs),
	)

	Metrics.RecordRoutingDecision("local_node", taskType)
	Metrics.RecordNodeRequest(node.ID, node.ModelTag)
	Metrics.SetNodeLoad(node.ID, node.Health.CurrentLoad)

	// Inject asynchronous Kanban telemetry back out to Elasticsearch natively
	if req.TaskID != "" {
		go func(taskID, host, model string) {
			esURL := os.Getenv("ES_URL")
			if esURL == "" {
				esURL = "http://elasticsearch:9200"
			}
			esURL = strings.TrimRight(esURL, "/")
			index := os.Getenv("ES_INDEX_TASKS")
			if index == "" {
				index = "agent-task-records"
			}
			payload := map[string]interface{}{
				"doc": map[string]string{
					"execution_host": host,
					"model":          model,
				},
			}
			body, _ := json.Marshal(payload)
			reqES, _ := http.NewRequest("POST", fmt.Sprintf("%s/%s/_update/%s", esURL, index, taskID), bytes.NewReader(body))
			reqES.Header.Set("Content-Type", "application/json")
			if apiKey := os.Getenv("ES_API_KEY"); apiKey != "" {
				reqES.Header.Set("Authorization", "ApiKey "+apiKey)
			}
			client := &http.Client{Timeout: 3 * time.Second}
			resp, err := client.Do(reqES)
			if err != nil {
				Log().Warn("failed to update execution telemetry on ES", slog.String("task_id", taskID), slog.String("error", err.Error()))
				return
			}
			defer resp.Body.Close()
			if resp.StatusCode >= 400 {
				Log().Warn("non-200 response updating execution telemetry", slog.String("task_id", taskID), slog.Int("status", resp.StatusCode))
			} else {
				Log().Info("synchronized execution telemetry to ES dynamically", slog.String("task_id", taskID), slog.String("host", host), slog.String("model", model))
			}
		}(req.TaskID, node.Host, node.ModelTag)
	}

	resp, err := m.routeToNode(ctx, req, node, withTools)
	if err == nil {
		return resp, nil
	}

	// Primary node failed — try fallback nodes.
	log.Warn("multi_node_router: primary node failed, trying fallback",
		slog.String("node_id", node.ID),
		slog.String("error", err.Error()),
	)

	fallbackResp, fallbackErr := m.tryFallbackNodes(ctx, req, node.ID, taskType, withTools)
	if fallbackErr == nil {
		return fallbackResp, nil
	}

	// All local nodes failed → frontier escalation.
	log.Warn("multi_node_router: all local nodes failed — frontier escalation",
		slog.String("task_type", taskType),
	)
	Metrics.RecordRoutingDecision("frontier_all_failed", taskType)
	return m.routeFrontierFallback(ctx, req, withTools)
}

// routeToNode routes a request to a specific Ollama node by overriding the base URL.
func (m *MultiNodeRouter) routeToNode(ctx context.Context, req *ChatRequest, node *Node, withTools bool) (*ChatResponse, error) {
	// Build node-specific URL.
	nodeURL := "http://" + node.Host

	// Override the Ollama base URL for this request.
	cloned := cloneChatRequest(req)
	cloned.Provider = ProviderOllama
	cloned.Model = node.ModelTag

	// Use the node-specific routing path.
	resp, err := m.router.RouteToNode(ctx, cloned, nodeURL, node.AuthToken, withTools)
	if err == nil && resp != nil {
		resp.Telemetry = &Telemetry{
			NodeID:   node.ID,
			NodeHost: node.Host,
			Model:    node.ModelTag,
		}
		log := WithContext(ctx)
		log.Info("telemetry payload attached", slog.String("node_id", node.ID))
	}
	return resp, err
}

// tryFallbackNodes attempts to route to any other healthy node besides the failed one.
func (m *MultiNodeRouter) tryFallbackNodes(ctx context.Context, req *ChatRequest, failedNodeID, taskType string, withTools bool) (*ChatResponse, error) {
	healthy := m.registry.HealthyNodes()
	for _, n := range healthy {
		if n.ID == failedNodeID {
			continue
		}

		log := WithContext(ctx)
		log.Info("multi_node_router: trying fallback node",
			slog.String("node_id", n.ID),
			slog.String("host", n.Host),
		)

		Metrics.RecordNodeRequest(n.ID, n.ModelTag)
		resp, err := m.routeToNode(ctx, req, n, withTools)
		if err == nil {
			return resp, nil
		}

		log.Warn("multi_node_router: fallback node also failed",
			slog.String("node_id", n.ID),
			slog.String("error", err.Error()),
		)
	}

	return nil, fmt.Errorf("all fallback nodes exhausted")
}

// routeFrontierFallback routes to the configured frontier model (cloud).
func (m *MultiNodeRouter) routeFrontierFallback(ctx context.Context, req *ChatRequest, withTools bool) (*ChatResponse, error) {
	fallback := m.config.FrontierFallbackModel
	if fallback == "" {
		fallback = "gpt-4o"
	}

	log := WithContext(ctx)
	log.Warn("multi_node_router: frontier fallback",
		slog.String("model", fallback),
	)

	Metrics.RecordEscalation()

	cloned := cloneChatRequest(req)
	cloned.Model = fallback
	cloned.Provider = "" // re-resolved by ProviderRouter based on model rules

	return m.router.Route(ctx, cloned, withTools)
}
