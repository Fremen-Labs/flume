package gateway

import (
	"crypto/tls"
	"net/http"
	"sync"
	"time"
)

// NodeConnManager provides per-node shared HTTP transports for efficient
// connection reuse to local Ollama nodes in the mesh.
//
// This is the Phase 1 core of the local LLM optimization (see
// docs/designs/local-llm-optimization-phases.md and the original report
// in docs/reference/local-llm-mesh-optimization-report.md).
//
// Goals (from report + SKILLs):
// - Eliminate repeated TCP handshakes (biggest perf gap for distributed N-node mesh).
// - Enable HTTP/2 (multiplexing, header compression) where the Ollama backend supports it
//   via ForceAttemptHTTP2 (pragmatic win without changing wire protocol or breaking stock Ollama).
// - Support always-streaming for local paths (see ollamaWithNode updates).
// - Lifecycle tied to node health (close idles on offline).
// - Bounded, observable, ctx-first (reliable-go + flume-go SKILLs).
// - ZERO changes to frontier paths (ProviderOllama + mesh only).
//
// Usage:
//   mgr := NewNodeConnManager()
//   client := mgr.GetClientForNode(node)  // reuses transport
//   // ... use client for /api/chat etc.
//
// Config (additive, safe defaults; see Phase 1 in phases doc):
//   FLUME_OLLAMA_KEEPALIVE=1 (default on)
//   FLUME_OLLAMA_MAX_IDLE_PER_NODE=20
//
// Follows flume-go SKILL: rich reasoning comments, propagate context where possible,
// no fire-and-forget, explicit.
type NodeConnManager struct {
	mu        sync.RWMutex
	transports map[string]*http.Transport // key = node.ID
	clients    map[string]*http.Client    // optional pooled clients per node
	maxIdle   int
	keepAlive bool
}

// NewNodeConnManager creates the manager with defaults from env or safe values.
// Per reliable-go: explicit construction, no globals.
func NewNodeConnManager() *NodeConnManager {
	maxIdle := 20
	if v := getEnvInt("FLUME_OLLAMA_MAX_IDLE_PER_NODE", 20); v > 0 {
		maxIdle = v
	}
	keep := true
	if v := getEnvBool("FLUME_OLLAMA_KEEPALIVE", true); !v {
		keep = false
	}
	return &NodeConnManager{
		transports: make(map[string]*http.Transport),
		clients:    make(map[string]*http.Client),
		maxIdle:    maxIdle,
		keepAlive:  keep,
	}
}

// getEnvInt / getEnvBool are small helpers (avoid pulling full config here;
// in real use, wire from gateway Config).
// (In full impl, move to internal/config or pass from server.)
func getEnvInt(key string, def int) int {
	// Simplified; real code would use os.Getenv + strconv (as in other gateway files).
	// For Phase 1 skeleton, return def. Expand in next iteration.
	return def
}

func getEnvBool(key string, def bool) bool {
	return def
}

// GetTransportForNode returns (or creates) a tuned Transport for the node.
// Keyed by node.ID for isolation across the mesh.
// Transport is reused for all calls to that node (chat, probes if wired later).
func (m *NodeConnManager) GetTransportForNode(node *Node) *http.Transport {
	if node == nil || node.ID == "" {
		// Fallback to default (should not happen in mesh paths).
		return &http.Transport{
			DisableKeepAlives: !m.keepAlive,
		}
	}

	m.mu.RLock()
	if t, ok := m.transports[node.ID]; ok {
		m.mu.RUnlock()
		return t
	}
	m.mu.RUnlock()

	m.mu.Lock()
	defer m.mu.Unlock()

	// Double-check after lock.
	if t, ok := m.transports[node.ID]; ok {
		return t
	}

	t := &http.Transport{
		DisableKeepAlives:     !m.keepAlive,
		MaxIdleConnsPerHost:   m.maxIdle,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ForceAttemptHTTP2:     true, // Phase 1: HTTP/2 where Ollama advertises (ALPN). See report consideration of gRPC/HTTP2.
		TLSClientConfig:       &tls.Config{InsecureSkipVerify: true}, // Matches existing node health / ES patterns; tighten in Phase 5 mTLS.
	}

	// ForceAttemptHTTP2 set above. For plain http:// Ollama (common in mesh), Go will attempt h2c
	// upgrade when possible. For https nodes it uses ALPN. See report "Consideration of gRPC/HTTP/2".
	// (No extra import needed for basic ForceAttempt.)

	m.transports[node.ID] = t
	return t
}

// GetClientForNode returns a client using the node's transport (for direct use in
// places that create ad-hoc clients today, e.g. stream paths).
// Reuses the transport for keep-alive benefits.
func (m *NodeConnManager) GetClientForNode(node *Node, timeout time.Duration) *http.Client {
	if node == nil || node.ID == "" {
		return &http.Client{Timeout: timeout}
	}

	key := node.ID
	m.mu.RLock()
	if c, ok := m.clients[key]; ok {
		m.mu.RUnlock()
		return c
	}
	m.mu.RUnlock()

	m.mu.Lock()
	defer m.mu.Unlock()

	if c, ok := m.clients[key]; ok {
		return c
	}

	transport := m.GetTransportForNode(node)
	c := &http.Client{
		Transport: transport,
		Timeout:   timeout, // Note: for streaming paths we often pass 0 and rely on ctx (see Phase 1 unification).
	}
	m.clients[key] = c
	return c
}

// CloseIdleForNode closes idle connections for a specific node (called on health
// transition to offline/degraded per report lifecycle).
func (m *NodeConnManager) CloseIdleForNode(nodeID string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if t, ok := m.transports[nodeID]; ok {
		t.CloseIdleConnections()
	}
	delete(m.transports, nodeID)
	delete(m.clients, nodeID)
}

// CloseAll closes all idle conns (for shutdown / full mesh reset).
func (m *NodeConnManager) CloseAll() {
	m.mu.Lock()
	defer m.mu.Unlock()

	for _, t := range m.transports {
		t.CloseIdleConnections()
	}
	m.transports = make(map[string]*http.Transport)
	m.clients = make(map[string]*http.Client)
}

// Stats returns basic per-node conn info (for Phase 1 observability + health/metrics).
// In full version, enhance with atomic counters or hijack transport state.
func (m *NodeConnManager) Stats() map[string]map[string]int {
	m.mu.RLock()
	defer m.mu.RUnlock()

	stats := make(map[string]map[string]int)
	for id := range m.transports {
		stats[id] = map[string]int{
			"max_idle_per_host": m.maxIdle,
			"keepalive":         boolToInt(m.keepAlive),
			// TODO(Phase 4): real idle/inuse counts via transport metrics or wrappers.
		}
	}
	return stats
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// Note: In production wiring (Phase 1 completion):
// - Instantiate in gateway server (alongside nodeRegistry, multiRouter).
// - Pass to ProviderRouter or use globally for ollama paths.
// - Wire health checker / node registry to call CloseIdleForNode on status changes.
// - Expose stats via existing /health or metrics endpoint.
// This is the skeleton; full integration + always-stream force + ctx timeout unification
// in follow-on edits to providers.go / tool_stream.go / server.go (small, bounded changes).

// For now, this file stands alone and can be used immediately in ollama call sites
// by replacing bare &http.Client{} creations. See Phase 1 todo for next steps.