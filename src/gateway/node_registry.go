package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Node Registry — Capability-aware Ollama node mesh.
//
// Manages a fleet of Ollama nodes (Mac Minis, Linux servers, cloud VMs) with
// weighted scoring for intelligent task routing. All state is persisted in
// Elasticsearch (flume-node-registry); the in-memory cache is refreshed on
// the same TTL cadence as the global Config.
//
// Security invariants:
//   - AuthToken is NEVER logged, serialized to metrics, or returned in API responses.
//   - Node IDs are validated against ^[a-z0-9\-]+$ on registration.
//   - All logging uses the gateway's slog-based structured logger.
// ─────────────────────────────────────────────────────────────────────────────

const (
	nodeRegistryIndex = "flume-node-registry"

	// Scoring weights for SelectNode (must sum to 1.0).
	weightModelFit          = 0.4
	weightLoadInverse       = 0.3
	weightLatencyInverse    = 0.2
	weightEnsembleEligible  = 0.1

	// Health status constants.
	NodeStatusHealthy  = "healthy"
	NodeStatusDegraded = "degraded"
	NodeStatusOffline  = "offline"
)

// Node represents a single Ollama inference endpoint in the mesh.
type Node struct {
	ID           string           `json:"id"`
	Host         string           `json:"host"`           // "192.168.1.50:11434"
	ModelTag     string           `json:"model_tag"`      // primary model: "qwen2.5-coder:32b"
	Capabilities NodeCapabilities `json:"capabilities"`
	Health       NodeHealth       `json:"health"`
	// AuthToken is resolved from OpenBao at runtime — never persisted to ES or logged.
	AuthToken    string           `json:"-"`
	// AuthSecretPath is the OpenBao path for this node's bearer token.
	AuthSecretPath string         `json:"auth_secret_path,omitempty"`
}

// NodeCapabilities describes the hardware and model characteristics of a node.
type NodeCapabilities struct {
	ReasoningScore int     `json:"reasoning_score"` // 1-10 operator-assigned
	MaxContext     int     `json:"max_context"`     // e.g., 131072
	Quantization   string  `json:"quantization"`    // "Q4_K_M", "Q8_0", "FP16"
	EstimatedTPS   float64 `json:"estimated_tps"`   // tokens/sec
	MemoryGB       float64 `json:"memory_gb"`       // total unified/VRAM
}

// NodeHealth is the live operational state of a node, updated by the HealthChecker.
type NodeHealth struct {
	Status       string    `json:"status"`        // "healthy", "degraded", "offline"
	LastSeen     time.Time `json:"last_seen"`
	CurrentLoad  float64   `json:"current_load"`  // 0.0-1.0
	LoadedModels []string  `json:"loaded_models"` // discovered via /api/tags
	LatencyMs    int64     `json:"latency_ms"`    // last probe round-trip
}

// NodeRegistry manages the in-memory node mesh with ES-backed persistence.
type NodeRegistry struct {
	mu         sync.RWMutex
	nodes      map[string]*Node
	esURL      string
	httpClient *http.Client
}

// NewNodeRegistry creates an empty registry wired to Elasticsearch.
func NewNodeRegistry(esURL string) *NodeRegistry {
	if esURL == "" {
		esURL = "http://elasticsearch:9200"
	}
	return &NodeRegistry{
		nodes:      make(map[string]*Node),
		esURL:      strings.TrimRight(esURL, "/"),
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
}

// RefreshFromES loads all node documents from the flume-node-registry ES index.
// Called on the same TTL cadence as Config.Refresh().
func (r *NodeRegistry) RefreshFromES(ctx context.Context) {
	log := WithContext(ctx)

	url := fmt.Sprintf("%s/%s/_search?size=100", r.esURL, nodeRegistryIndex)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(`{"query":{"match_all":{}}}`))
	if err != nil {
		log.Warn("node_registry: failed to build ES search request",
			slog.String("error", err.Error()),
		)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := r.httpClient.Do(req)
	if err != nil {
		log.Debug("node_registry: ES unreachable — retaining cached nodes",
			slog.String("error", err.Error()),
		)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		// Index doesn't exist yet — no nodes registered
		log.Debug("node_registry: flume-node-registry index not found — no nodes registered")
		return
	}
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		log.Warn("node_registry: ES returned error",
			slog.Int("status", resp.StatusCode),
			slog.String("body", string(body)),
		)
		return
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		log.Warn("node_registry: failed to read ES response",
			slog.String("error", err.Error()),
		)
		return
	}

	var esResp struct {
		Hits struct {
			Hits []struct {
				Source Node `json:"_source"`
			} `json:"hits"`
		} `json:"hits"`
	}
	if err := json.Unmarshal(body, &esResp); err != nil {
		log.Warn("node_registry: failed to parse ES response",
			slog.String("error", err.Error()),
		)
		return
	}

	newNodes := make(map[string]*Node, len(esResp.Hits.Hits))
	for _, hit := range esResp.Hits.Hits {
		node := hit.Source
		// Preserve existing health state if node was already tracked
		r.mu.RLock()
		if existing, ok := r.nodes[node.ID]; ok {
			node.Health = existing.Health
			node.AuthToken = existing.AuthToken
		}
		r.mu.RUnlock()
		newNodes[node.ID] = &node
	}

	r.mu.Lock()
	r.nodes = newNodes
	r.mu.Unlock()

	log.Info("node_registry: refreshed from ES",
		slog.Int("node_count", len(newNodes)),
	)
}

// HealthyNodes returns all nodes with Status == "healthy".
func (r *NodeRegistry) HealthyNodes() []*Node {
	r.mu.RLock()
	defer r.mu.RUnlock()

	var healthy []*Node
	for _, n := range r.nodes {
		if n.Health.Status == NodeStatusHealthy {
			healthy = append(healthy, n)
		}
	}
	return healthy
}

// AllNodes returns a snapshot of all registered nodes (with AuthToken redacted).
func (r *NodeRegistry) AllNodes() []Node {
	r.mu.RLock()
	defer r.mu.RUnlock()

	out := make([]Node, 0, len(r.nodes))
	for _, n := range r.nodes {
		safe := *n
		safe.AuthToken = "" // Security: never expose tokens in API responses
		out = append(out, safe)
	}
	return out
}

// Count returns the number of registered nodes.
func (r *NodeRegistry) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.nodes)
}

// GetNode returns a node by ID, or nil if not found.
func (r *NodeRegistry) GetNode(id string) *Node {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.nodes[id]
}

// UpdateHealth atomically updates the health state of a node.
func (r *NodeRegistry) UpdateHealth(nodeID string, health NodeHealth) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if n, ok := r.nodes[nodeID]; ok {
		n.Health = health
	}
}

// SelectNode picks the best available node for a given task type using weighted scoring.
//
// Score = (model_fit × 0.4) + (load_inverse × 0.3) + (latency_inverse × 0.2) + (ensemble_eligible × 0.1)
//
// Returns nil if no healthy nodes are available.
func (r *NodeRegistry) SelectNode(taskType string, minReasoningScore int) *Node {
	healthy := r.HealthyNodes()
	if len(healthy) == 0 {
		return nil
	}

	type scored struct {
		node  *Node
		score float64
	}

	// Compute max latency across healthy nodes for normalization.
	var maxLatency int64 = 1
	for _, n := range healthy {
		if n.Health.LatencyMs > maxLatency {
			maxLatency = n.Health.LatencyMs
		}
	}

	candidates := make([]scored, 0, len(healthy))
	for _, n := range healthy {
		// Model fit: reasoning score normalized to 0-1, filtered by minimum.
		if n.Capabilities.ReasoningScore < minReasoningScore {
			continue
		}
		modelFit := float64(n.Capabilities.ReasoningScore) / 10.0

		// Task-type affinity boost.
		switch taskType {
		case "reasoning", "planning", "pm":
			modelFit = math.Min(1.0, modelFit*1.2)
		case "review", "test", "fast":
			// Prefer speed over reasoning power.
			modelFit = math.Min(1.0, modelFit*0.8+float64(n.Capabilities.EstimatedTPS)/100.0*0.2)
		}

		// Load inverse: lower load = higher score.
		loadInverse := 1.0 - n.Health.CurrentLoad

		// Latency inverse: lower latency = higher score.
		latencyInverse := 1.0 - float64(n.Health.LatencyMs)/float64(maxLatency+1)

		// Ensemble eligibility: bonus for nodes that can participate in jury.
		ensembleBonus := 0.0
		if n.Capabilities.MemoryGB >= 16 {
			ensembleBonus = 1.0
		} else if n.Capabilities.MemoryGB >= 8 {
			ensembleBonus = 0.5
		}

		total := modelFit*weightModelFit +
			loadInverse*weightLoadInverse +
			latencyInverse*weightLatencyInverse +
			ensembleBonus*weightEnsembleEligible

		candidates = append(candidates, scored{node: n, score: total})
	}

	if len(candidates) == 0 {
		return nil
	}

	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].score > candidates[j].score
	})

	Log().Debug("node_registry: selection scored",
		slog.String("task_type", taskType),
		slog.String("selected", candidates[0].node.ID),
		slog.Float64("score", candidates[0].score),
		slog.Int("candidates", len(candidates)),
	)

	return candidates[0].node
}

// UpsertNodeToES persists a node document to the flume-node-registry index.
func (r *NodeRegistry) UpsertNodeToES(ctx context.Context, node *Node) error {
	log := WithContext(ctx)

	// Security: strip AuthToken before serializing to ES.
	safe := *node
	safe.AuthToken = ""

	body, err := json.Marshal(safe)
	if err != nil {
		return fmt.Errorf("marshal node: %w", err)
	}

	url := fmt.Sprintf("%s/%s/_doc/%s", r.esURL, nodeRegistryIndex, node.ID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, strings.NewReader(string(body)))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := r.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("ES request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("ES HTTP %d: %s", resp.StatusCode, string(respBody))
	}

	// Update in-memory cache.
	r.mu.Lock()
	r.nodes[node.ID] = node
	r.mu.Unlock()

	log.Info("node_registry: upserted node to ES",
		slog.String("node_id", node.ID),
		slog.String("host", node.Host),
		slog.String("model", node.ModelTag),
	)

	return nil
}

// DeleteNodeFromES removes a node from the flume-node-registry index.
func (r *NodeRegistry) DeleteNodeFromES(ctx context.Context, nodeID string) error {
	log := WithContext(ctx)

	url := fmt.Sprintf("%s/%s/_doc/%s", r.esURL, nodeRegistryIndex, nodeID)
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, url, nil)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}

	resp, err := r.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("ES request: %w", err)
	}
	defer resp.Body.Close()

	// Remove from in-memory cache.
	r.mu.Lock()
	delete(r.nodes, nodeID)
	r.mu.Unlock()

	log.Info("node_registry: deleted node from ES",
		slog.String("node_id", nodeID),
	)

	return nil
}

// EnsureIndex creates the flume-node-registry ES index if it doesn't exist.
func (r *NodeRegistry) EnsureIndex(ctx context.Context) error {
	log := WithContext(ctx)

	url := r.esURL + "/" + nodeRegistryIndex
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, url, nil)
	if err != nil {
		return err
	}
	resp, err := r.httpClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode == 200 {
		return nil // already exists
	}

	mapping := `{
		"mappings": {
			"properties": {
				"id":              {"type": "keyword"},
				"host":            {"type": "keyword"},
				"model_tag":       {"type": "keyword"},
				"capabilities":    {"type": "object", "enabled": true},
				"health":          {"type": "object", "enabled": true},
				"auth_secret_path":{"type": "keyword"},
				"updated_at":      {"type": "date"}
			}
		}
	}`

	putReq, err := http.NewRequestWithContext(ctx, http.MethodPut, url, strings.NewReader(mapping))
	if err != nil {
		return err
	}
	putReq.Header.Set("Content-Type", "application/json")
	putResp, err := r.httpClient.Do(putReq)
	if err != nil {
		return err
	}
	putResp.Body.Close()

	log.Info("node_registry: created flume-node-registry ES index")
	return nil
}
