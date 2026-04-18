package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// isNonMacWorkstation returns true for OSes where Flume typically runs on
// discrete-GPU hosts (Linux/Windows). macOS builds use tighter concurrency
// defaults tuned for unified-memory Apple Silicon.
func isNonMacWorkstation() bool {
	return runtime.GOOS != "darwin"
}

// ─────────────────────────────────────────────────────────────────────────────
// Adaptive Ollama Concurrency Limiter
//
// Detects the system's available resources at startup and dynamically sets
// the maximum number of concurrent Ollama inference requests. This prevents
// the invisible serialization problem where 16 agents queue behind one
// Ollama instance, each waiting 2+ minutes per tool call.
//
// Detection strategy:
//  1. Query Ollama /api/ps to see loaded models and their VRAM usage
//  2. Fall back to CPU core count / 4 (conservative: 1 inference per 4 cores)
//  3. Allow override via FLUME_OLLAMA_MAX_CONCURRENT env var
//
// NodeSemaphoreMap extends this to a distributed node mesh: each registered
// Ollama node gets its own OllamaSemaphore sized from its capabilities, so
// concurrency limits are enforced independently per node.
// ─────────────────────────────────────────────────────────────────────────────

// NodeSemaphoreMap manages per-node concurrency semaphores for the distributed
// Ollama node mesh. Each node has an independent slot pool sized proportionally
// to its reported MemoryGB, preventing any single node from being overloaded
// while leaving healthier nodes idle.
type NodeSemaphoreMap struct {
	mu   sync.RWMutex
	sems map[string]*OllamaSemaphore // keyed by node ID
}

// NewNodeSemaphoreMap creates an empty semaphore map.
func NewNodeSemaphoreMap() *NodeSemaphoreMap {
	return &NodeSemaphoreMap{
		sems: make(map[string]*OllamaSemaphore),
	}
}

// SlotsForNode returns or lazily creates a semaphore for the given node.
// The slot count is derived from the node's MemoryGB: 1 slot per 8 GB,
// clamped to [1, maxSlots]. Mac builds cap at 8; Linux/Windows node mesh
// (discrete GPUs) allow up to 16 so multi-GPU hosts are not artificially idle.
func (m *NodeSemaphoreMap) SlotsForNode(node *Node) *OllamaSemaphore {
	m.mu.RLock()
	if sem, ok := m.sems[node.ID]; ok {
		m.mu.RUnlock()
		return sem
	}
	m.mu.RUnlock()

	slots := int(node.Capabilities.MemoryGB / 8)
	if slots < 1 {
		slots = 1
	}
	maxSlots := 8
	if isNonMacWorkstation() {
		maxSlots = 16
	}
	if slots > maxSlots {
		slots = maxSlots
	}

	log := Log()
	log.Info("node_semaphore: allocating slots for node",
		slog.String("node_id", node.ID),
		slog.Float64("memory_gb", node.Capabilities.MemoryGB),
		slog.Int("slots", slots),
	)

	sem := NewOllamaSemaphore(slots)

	m.mu.Lock()
	m.sems[node.ID] = sem
	m.mu.Unlock()

	return sem
}

// Remove deletes the semaphore for a node that has been deregistered.
func (m *NodeSemaphoreMap) Remove(nodeID string) {
	m.mu.Lock()
	delete(m.sems, nodeID)
	m.mu.Unlock()

	Log().Info("node_semaphore: removed slot pool for node",
		slog.String("node_id", nodeID),
	)
}

// Snapshot returns a map of nodeID → active/max slots for health metrics.
func (m *NodeSemaphoreMap) Snapshot() map[string][2]int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make(map[string][2]int, len(m.sems))
	for id, sem := range m.sems {
		out[id] = [2]int{sem.ActiveSlots(), sem.MaxSlots()}
	}
	return out
}

// OllamaSemaphore controls concurrent access to the Ollama inference engine.
type OllamaSemaphore struct {
	sem      chan struct{}
	maxSlots int
	mu       sync.RWMutex
}

// NewOllamaSemaphore creates a semaphore with the given max concurrency.
func NewOllamaSemaphore(maxConcurrent int) *OllamaSemaphore {
	if maxConcurrent < 1 {
		maxConcurrent = 1
	}
	return &OllamaSemaphore{
		sem:      make(chan struct{}, maxConcurrent),
		maxSlots: maxConcurrent,
	}
}

// Acquire blocks until a slot is available or the context is cancelled.
// Returns true if acquired, false if context was cancelled.
func (s *OllamaSemaphore) Acquire(ctx context.Context) bool {
	select {
	case s.sem <- struct{}{}:
		return true
	case <-ctx.Done():
		return false
	}
}

// TryAcquire attempts to acquire without blocking.
// Returns true if acquired, false if all slots are full.
func (s *OllamaSemaphore) TryAcquire() bool {
	select {
	case s.sem <- struct{}{}:
		return true
	default:
		return false
	}
}

// Release returns a slot to the pool.
func (s *OllamaSemaphore) Release() {
	<-s.sem
}

// MaxSlots returns the configured max concurrency.
func (s *OllamaSemaphore) MaxSlots() int {
	return s.maxSlots
}

// ActiveSlots returns how many slots are currently in use.
func (s *OllamaSemaphore) ActiveSlots() int {
	return len(s.sem)
}

// DetectOllamaCapacity determines the optimal concurrency limit for Ollama.
func DetectOllamaCapacity(ollamaBaseURL string) int {
	log := Log()

	// 1. Check explicit env override
	if envMax := strings.TrimSpace(os.Getenv("FLUME_OLLAMA_MAX_CONCURRENT")); envMax != "" {
		if n, err := strconv.Atoi(envMax); err == nil && n > 0 {
			log.Info("ollama concurrency from env override",
				slog.Int("max_concurrent", n),
				slog.String("source", "FLUME_OLLAMA_MAX_CONCURRENT"),
			)
			return n
		}
	}

	// 2. Query Ollama /api/ps to detect loaded models
	maxFromOllama := detectFromOllamaPS(ollamaBaseURL)
	if maxFromOllama > 0 {
		log.Info("ollama concurrency from model detection",
			slog.Int("max_concurrent", maxFromOllama),
			slog.String("source", "ollama_api_ps"),
		)
		return maxFromOllama
	}

	// 3. Fall back to CPU-based heuristic
	cores := runtime.NumCPU()
	maxFromCPU := cores / 4
	if maxFromCPU < 1 {
		maxFromCPU = 1
	}
	cpuCap := 4
	if isNonMacWorkstation() {
		cpuCap = 8
	}
	if maxFromCPU > cpuCap {
		maxFromCPU = cpuCap
	}

	log.Info("ollama concurrency from CPU heuristic",
		slog.Int("max_concurrent", maxFromCPU),
		slog.Int("cpu_cores", cores),
		slog.String("source", "cpu_heuristic"),
	)
	return maxFromCPU
}

// detectFromOllamaPS queries the Ollama /api/ps endpoint to determine how
// many model instances are loaded and their VRAM consumption.
func detectFromOllamaPS(baseURL string) int {
	url := strings.TrimRight(baseURL, "/") + "/api/ps"
	client := &http.Client{Timeout: 5 * time.Second}

	resp, err := client.Get(url)
	if err != nil {
		Log().Debug("could not reach ollama /api/ps",
			slog.String("error", err.Error()),
		)
		return 0
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 16*1024))
	if err != nil {
		return 0
	}

	var psResp struct {
		Models []struct {
			Name     string `json:"name"`
			Size     int64  `json:"size"`
			SizeVRAM int64  `json:"size_vram"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &psResp); err != nil {
		return 0
	}

	if len(psResp.Models) == 0 {
		// No models loaded yet — conservative on Mac; higher on Linux/Windows where
		// Ollama often backs multi-GPU or high-memory discrete cards.
		if isNonMacWorkstation() {
			return 6
		}
		return 2
	}

	// Ollama serializes inference for a single model. With one model loaded,
	// true parallelism is 1, but we allow multiple in-flight HTTP requests so
	// the pipeline stays fed (Ollama may batch or schedule across GPUs).
	//
	// With multiple models loaded (rare in local dev), each can run concurrently.
	modelCount := len(psResp.Models)

	perModelMult := 2
	maxCap := 8
	if isNonMacWorkstation() {
		perModelMult = 4
		maxCap = 16
	}
	maxConcurrent := modelCount * perModelMult
	if maxConcurrent > maxCap {
		maxConcurrent = maxCap
	}

	Log().Info("ollama model detection",
		slog.Int("models_loaded", modelCount),
		slog.Int("max_concurrent", maxConcurrent),
		slog.String("model_names", fmt.Sprintf("%v", modelNames(psResp.Models))),
	)

	return maxConcurrent
}

func modelNames(models []struct {
	Name     string `json:"name"`
	Size     int64  `json:"size"`
	SizeVRAM int64  `json:"size_vram"`
}) []string {
	names := make([]string, len(models))
	for i, m := range models {
		names[i] = m.Name
	}
	return names
}
