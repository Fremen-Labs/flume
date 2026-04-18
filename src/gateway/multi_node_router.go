package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
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

// ExecuteSmartRoute selects the optimal node and routes the request.
// For non-Ollama providers, delegates directly to ProviderRouter.Route().
func (m *MultiNodeRouter) ExecuteSmartRoute(ctx context.Context, req *ChatRequest, taskType string, withTools bool) (*ChatResponse, error) {
	log := WithContext(ctx)

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
	node := m.registry.SelectNode(taskType, minScore, requiresHighParam)
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
	return m.router.RouteToNode(ctx, cloned, nodeURL, node.AuthToken, withTools)
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
