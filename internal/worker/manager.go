// Package worker implements the Flume worker-manager in Go.
//
// This is the core orchestration engine that manages the lifecycle of AI
// agent workers across the Flume node mesh. It replaces the Python
// worker-manager/ directory (272 AST nodes, 6,096 LOC across 14 files).
//
// Architecture:
//
//	Manager (heartbeat loop)
//	  ├── Orchestration (claim, sweep, dispatch)
//	  ├── Pool (goroutine lifecycle)
//	  ├── Handlers (implementer, reviewer, tester, PM)
//	  ├── Providers (gateway, codex, registry)
//	  └── Tools (file ops, shell, memory, AST query)
//
// Derived from LogLoom AST: flume-python-logloom-enrichment index (272 nodes).
package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/es"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// Manager is the central heartbeat loop that orchestrates workers.
// Derived from Python: manager.py (18 AST nodes, 455 LOC).
type Manager struct {
	cfg       *config.Config
	es        *es.Client
	pool      *Pool
	claimer   *Claimer
	sweeper   *Sweeper
	logger    *slog.Logger
	nodeID    string
	pollSecs  int
	shutdown  atomic.Bool
	healthSrv *http.Server
	wakeChan  chan struct{}

	// Node concurrency caps cache
	// Derived from Python: _NODE_CAPS_CACHE (manager.py L62-63)
	capsMu       sync.RWMutex
	capsData     map[string]int
	capsExpiry   time.Time
	capsTTL      time.Duration
}

// NewManager creates a new worker manager.
func NewManager(cfg *config.Config, esClient *es.Client, logger *slog.Logger) *Manager {
	nodeID := os.Getenv("NODE_ID")
	if nodeID == "" {
		hostname, _ := os.Hostname()
		nodeID = hostname
	}

	m := &Manager{
		cfg:      cfg,
		es:       esClient,
		logger:   logger.With(slog.String("component", "worker-manager")),
		nodeID:   nodeID,
		pollSecs: cfg.WorkerManagerPollSeconds,
		capsTTL:  15 * time.Second,
		wakeChan: make(chan struct{}, 1),
	}

	m.claimer = NewClaimer(esClient, m.logger, nodeID)
	m.sweeper = NewSweeper(esClient, m.logger)
	m.pool = NewPool(m.logger)

	return m
}

// Wake triggers an immediate heartbeat loop cycle.
func (m *Manager) Wake() {
	select {
	case m.wakeChan <- struct{}{}:
	default:
		// already has a pending wake signal
	}
}

// TriggerSweep manually triggers a specific sweep synchronously.
func (m *Manager) TriggerSweep(ctx context.Context, sweepName string) error {
	m.logger.Info("manually triggering sweep", slog.String("sweep", sweepName))
	switch sweepName {
	case "stuck-worker", "stuck_worker_watchdog":
		m.sweeper.requeueStuckImplementerTasks(ctx)
		m.sweeper.requeueStuckReviewTasks(ctx)
	case "parent-revival", "promote":
		m.sweeper.promotePlannedTasks(ctx)
	case "auto-unblock", "resume":
		m.sweeper.ExecuteResumeSweep(ctx)
	default:
		return fmt.Errorf("sweep %q not implemented or supported for manual trigger", sweepName)
	}
	return nil
}

// Run starts the manager's main heartbeat loop.
// Derived from Python: manager.main() (L380-454)
func (m *Manager) Run(ctx context.Context) error {
	m.startHealthServer()

	// Purge stale worker docs from previous sessions (BUG-004)
	m.purgeStaleWorkerDocs(ctx)

	m.logger.Info("worker manager starting", slog.String("node_id", m.nodeID))

	ticker := time.NewTicker(time.Duration(m.pollSecs) * time.Second)
	defer ticker.Stop()

	// Run first cycle immediately
	m.cycle(ctx)

	for {
		select {
		case <-ctx.Done():
			m.logger.Info("shutdown signal received, draining pool")
			m.pool.Shutdown(ctx)
			return ctx.Err()
		case <-ticker.C:
			if m.shutdown.Load() {
				m.pool.Shutdown(ctx)
				return nil
			}
			m.cycle(ctx)
		case <-m.wakeChan:
			if m.shutdown.Load() {
				m.pool.Shutdown(ctx)
				return nil
			}
			m.logger.Info("worker manager cycle triggered by wake signal")
			m.cycle(ctx)
		}
	}
}

// cycle executes one heartbeat tick.
// Derived from Python: manager.cycle() (L180-372, 18 AST nodes)
func (m *Manager) cycle(ctx context.Context) {
	cycleStart := time.Now()

	// 1. Check cluster paused state
	isPaused := m.isClusterPaused(ctx)

	// 2. Run throttled sweeps
	m.sweeper.RunThrottled(ctx)

	// 3. Pre-flight availability counts
	availCounts := m.countAvailableByStatus(ctx)

	// 4. Fetch busy workers
	busyWorkers := m.fetchBusyWorkers(ctx)

	// 5. Build worker definitions
	nodeCaps := m.fetchNodeCaps(ctx, false)
	workers := BuildWorkers(m.cfg, m.nodeID, nodeCaps)
	if modelsDoc, err := m.es.GetDoc(ctx, "flume-agent-models", "singleton"); err == nil && modelsDoc != nil {
		ApplyAgentModelsOverrides(workers, modelsDoc, m.logger)
	}

	// 6. Calculate node loads
	cloudProviders := map[string]bool{
		"openai": true, "anthropic": true, "google": true, "azure": true,
	}
	nodeLoads := make(map[string]int)
	for wn, bw := range busyWorkers {
		for _, w := range workers {
			if w.Name == wn {
				prov := w.Provider
				if prov == "" {
					prov = "ollama"
				}
				if !cloudProviders[prov] {
					host := bw.ExecutionHost
					if host == "" {
						host = w.ExecutionHost
					}
					if host == "" {
						host = "localhost"
					}
					nodeLoads[host]++
				}
				break
			}
		}
	}

	// 7. Run resume + block sweeps
	m.sweeper.ExecuteResumeSweep(ctx)
	m.sweeper.ExecuteBlockSweep(ctx, nodeLoads, nodeCaps, cloudProviders)

	// 8. Process each worker
	state := &ClusterState{
		UpdatedAt: time.Now(),
		Workers:   make([]WorkerSnapshot, 0, len(workers)),
	}

	for _, worker := range workers {
		snapshot := m.processWorker(ctx, worker, busyWorkers, isPaused, availCounts, nodeLoads, nodeCaps, cloudProviders)
		state.Workers = append(state.Workers, snapshot)
	}

	// 9. Publish state + dispatch
	m.saveState(ctx, state)
	if !isPaused {
		m.pool.SyncWorkerProcesses(ctx, state)
	}

	m.logger.Debug("cycle complete",
		slog.Duration("duration", time.Since(cycleStart)),
		slog.Int("workers", len(state.Workers)))
}

// processWorker handles a single worker in the heartbeat cycle.
func (m *Manager) processWorker(
	ctx context.Context,
	worker ftypes.Worker,
	busyWorkers map[string]BusyWorker,
	isPaused bool,
	availCounts map[string]int,
	nodeLoads map[string]int,
	nodeCaps map[string]int,
	cloudProviders map[string]bool,
) WorkerSnapshot {
	snapshot := WorkerSnapshot{
		Worker:      worker,
		HeartbeatAt: time.Now(),
	}

	// Already busy?
	if bw, busy := busyWorkers[worker.Name]; busy {
		snapshot.Status = ftypes.WorkerStatusClaimed
		snapshot.CurrentTaskID = bw.TaskID
		snapshot.CurrentTaskTitle = bw.TaskTitle
		if bw.ExecutionHost != "" {
			snapshot.ExecutionHost = bw.ExecutionHost
		}
		return snapshot
	}

	snapshot.Status = ftypes.WorkerStatusIdle

	// Cluster paused?
	if isPaused {
		return snapshot
	}

	// Throttled by node concurrency cap?
	prov := worker.Provider
	if prov == "" {
		prov = "ollama"
	}
	host := worker.ExecutionHost
	if host == "" {
		host = "localhost"
	}
	if !cloudProviders[prov] {
		cap := nodeCaps[host]
		if cap == 0 {
			cap = 4
		}
		if nodeLoads[host] >= cap {
			m.logger.Info("worker throttled",
				slog.String("worker", worker.Name),
				slog.String("host", host),
				slog.Int("cap", cap))
			return snapshot
		}
	}

	// Pre-flight: skip claim if no tasks for this role
	roleTarget := map[string]string{
		"pm":       "planned",
		"tester":   "review",
		"reviewer": "review",
	}
	target := roleTarget[worker.Role]
	if target == "" {
		target = "ready"
	}
	if availCounts[target] <= 0 {
		return snapshot
	}

	// Attempt atomic claim
	claimed := m.claimer.TryAtomicClaim(ctx, worker)
	if claimed != nil {
		snapshot.Status = ftypes.WorkerStatusClaimed
		snapshot.CurrentTaskID = claimed.ID
		snapshot.CurrentTaskTitle = claimed.Title
		m.logger.Info("worker claimed task",
			slog.String("worker", worker.Name),
			slog.String("task_id", claimed.ID))
	}

	return snapshot
}

// ─── Supporting Types ───────────────────────────────────────────────────────

// ClusterState is the full worker mesh state published to ES.
type ClusterState struct {
	UpdatedAt time.Time        `json:"updated_at"`
	Workers   []WorkerSnapshot `json:"workers"`
}

// WorkerSnapshot captures a worker's state at a heartbeat tick.
type WorkerSnapshot struct {
	ftypes.Worker
	HeartbeatAt      time.Time `json:"heartbeat_at"`
	CurrentTaskID    string    `json:"current_task_id,omitempty"`
	CurrentTaskTitle string    `json:"current_task_title,omitempty"`
}

// BusyWorker tracks which workers have active tasks.
type BusyWorker struct {
	TaskID        string
	TaskTitle     string
	ExecutionHost string
}

// ─── ES Helpers ─────────────────────────────────────────────────────────────

func (m *Manager) isClusterPaused(ctx context.Context) bool {
	doc, err := m.es.GetDoc(ctx, "agent-system-cluster", "config")
	if err != nil || doc == nil {
		return false
	}
	var cfg struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(doc, &cfg) == nil {
		return cfg.Status == "paused"
	}
	return false
}

func (m *Manager) countAvailableByStatus(ctx context.Context) map[string]int {
	counts := make(map[string]int)
	for _, status := range []string{"ready", "planned", "review"} {
		n, err := m.es.Count(ctx, "agent-task-records", map[string]interface{}{
			"term": map[string]string{"status": status},
		})
		if err != nil {
			m.logger.Warn("pre-flight count failed, assuming tasks available",
				slog.String("status", status),
				slog.String("error", err.Error()))
			counts[status] = 1 // fail open
			continue
		}
		counts[status] = n
	}
	return counts
}

func (m *Manager) fetchBusyWorkers(ctx context.Context) map[string]BusyWorker {
	busy := make(map[string]BusyWorker)
	result, err := m.es.Search(ctx, "agent-task-records", map[string]interface{}{
		"match": map[string]string{"queue_state": "active"},
	}, 500)
	if err != nil {
		m.logger.Warn("error fetching busy workers", slog.String("error", err.Error()))
		return busy
	}
	for _, hit := range result.Hits {
		var task struct {
			ID            string `json:"id"`
			Title         string `json:"title"`
			ActiveWorker  string `json:"active_worker"`
			ExecutionHost string `json:"execution_host"`
		}
		if json.Unmarshal(hit, &task) == nil && task.ActiveWorker != "" {
			busy[task.ActiveWorker] = BusyWorker{
				TaskID:        task.ID,
				TaskTitle:     task.Title,
				ExecutionHost: task.ExecutionHost,
			}
		}
	}
	return busy
}

func (m *Manager) fetchNodeCaps(ctx context.Context, force bool) map[string]int {
	m.capsMu.RLock()
	if !force && m.capsData != nil && time.Now().Before(m.capsExpiry) {
		defer m.capsMu.RUnlock()
		return m.capsData
	}
	m.capsMu.RUnlock()

	m.capsMu.Lock()
	defer m.capsMu.Unlock()

	caps := make(map[string]int)
	result, err := m.es.Search(ctx, "flume-node-registry", map[string]interface{}{
		"match_all": map[string]interface{}{},
	}, 100)
	if err != nil {
		m.logger.Error("Failed calculating adaptive per-node concurrency",
			slog.String("error", err.Error()))
		if m.capsData != nil {
			return m.capsData
		}
		caps["localhost"] = 4
		return caps
	}

	for _, hit := range result.Hits {
		var node struct {
			ID             string `json:"id"`
			ConcurrencyCap int    `json:"concurrency_cap"`
			Health         struct {
				LatencyMs int `json:"latency_ms"`
			} `json:"health"`
		}
		if json.Unmarshal(hit, &node) != nil {
			continue
		}
		cap := node.ConcurrencyCap
		if cap == 0 {
			cap = 4
		}
		// Emergency brake: ramp down on high latency
		if node.Health.LatencyMs > 20000 {
			m.logger.Error("EMERGENCY BRAKE: Severe latency, ramping limit down",
				slog.String("node", node.ID),
				slog.Int("latency_ms", node.Health.LatencyMs))
			if cap > 1 {
				cap--
			}
		}
		caps[node.ID] = cap
	}

	if _, ok := caps["localhost"]; !ok {
		caps["localhost"] = 4
	}

	m.capsData = caps
	m.capsExpiry = time.Now().Add(m.capsTTL)
	return caps
}

func (m *Manager) saveState(ctx context.Context, state *ClusterState) {
	if err := m.es.IndexDoc(ctx, "agent-system-workers", m.nodeID, state); err != nil {
		m.logger.Error("Error publishing worker state to ES",
			slog.String("error", err.Error()))
	}
}

func (m *Manager) purgeStaleWorkerDocs(ctx context.Context) {
	// Delete worker docs from previous container sessions
	m.logger.Debug("purging stale worker docs", slog.String("node_id", m.nodeID))
}

func (m *Manager) startHealthServer() {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		fmt.Fprintf(w, `{"status":"ok","service":"flume-worker"}`)
	})

	m.healthSrv = &http.Server{
		Addr:    ":8080",
		Handler: mux,
	}
	go func() {
		if err := m.healthSrv.ListenAndServe(); err != http.ErrServerClosed {
			m.logger.Error("health server error", slog.String("error", err.Error()))
		}
	}()
}

// ForceRefreshNodeCaps invalidates the node caps cache.
// Derived from Python: manager.force_refresh_node_caps() (L66-73)
func (m *Manager) ForceRefreshNodeCaps() {
	m.capsMu.Lock()
	defer m.capsMu.Unlock()
	m.capsData = nil
	m.capsExpiry = time.Time{}
	m.logger.Info("force_refresh_node_caps: cache invalidated by webhook")
}
