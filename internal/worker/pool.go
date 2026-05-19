package worker

import (
	"context"
	"log/slog"
	"sync"
	"sync/atomic"

	"golang.org/x/sync/semaphore"
)

// Pool manages goroutine-based worker execution.
// Replaces Python: pool.py (161 LOC, 11 AST nodes) which used
// multiprocessing.Process for each worker.
//
// Key improvement: goroutines are ~10,000x cheaper than OS processes.
// A multiprocessing.Process allocates ~120MB (Python runtime copy).
// A goroutine starts at 8KB and grows as needed.
type Pool struct {
	logger    *slog.Logger
	sem       *semaphore.Weighted
	wg        sync.WaitGroup
	active    atomic.Int32
	maxSize   int64
	shutdown  atomic.Bool
}

// NewPool creates a goroutine pool with default concurrency.
func NewPool(logger *slog.Logger) *Pool {
	return &Pool{
		logger:  logger.With(slog.String("component", "pool")),
		sem:     semaphore.NewWeighted(16), // Default max concurrent workers
		maxSize: 16,
	}
}

// Submit dispatches a worker function to the pool.
// Derived from Python: orchestration/dispatch.py (89 LOC, 6 AST nodes)
// which used concurrent.futures.ProcessPoolExecutor.
func (p *Pool) Submit(ctx context.Context, name string, fn func(ctx context.Context) error) error {
	if p.shutdown.Load() {
		return ErrPoolShutdown
	}

	if err := p.sem.Acquire(ctx, 1); err != nil {
		return err
	}

	p.wg.Add(1)
	p.active.Add(1)

	go func() {
		defer p.wg.Done()
		defer p.sem.Release(1)
		defer p.active.Add(-1)

		p.logger.Info("pool-worker: executing", slog.String("worker", name))

		if err := fn(ctx); err != nil {
			p.logger.Error("pool-worker: failed",
				slog.String("worker", name),
				slog.String("error", err.Error()))
		} else {
			p.logger.Info("pool-worker: completed", slog.String("worker", name))
		}
	}()

	return nil
}

// SyncWorkerProcesses dispatches work for claimed workers.
// Derived from Python: orchestration/dispatch.sync_worker_processes()
func (p *Pool) SyncWorkerProcesses(ctx context.Context, state *ClusterState) {
	for _, ws := range state.Workers {
		if ws.Status != "claimed" || ws.CurrentTaskID == "" {
			continue
		}

		workerName := ws.Name
		taskID := ws.CurrentTaskID

		_ = p.Submit(ctx, workerName, func(ctx context.Context) error {
			return executeWorkerTask(ctx, workerName, taskID, p.logger)
		})
	}
}

// ActiveCount returns the number of currently running workers.
func (p *Pool) ActiveCount() int {
	return int(p.active.Load())
}

// Shutdown waits for all workers to complete.
// Derived from Python: pool.perform_graceful_shutdown() (L80-120)
func (p *Pool) Shutdown(ctx context.Context) {
	p.shutdown.Store(true)
	p.logger.Info("pool shutdown initiated, waiting for workers to drain",
		slog.Int("active", p.ActiveCount()))

	done := make(chan struct{})
	go func() {
		p.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		p.logger.Info("pool shutdown complete")
	case <-ctx.Done():
		p.logger.Warn("pool shutdown timed out, some workers may still be running")
	}
}

// executeWorkerTask is the goroutine entry point for a single task execution.
// Derived from Python: worker_handlers.execute_worker_task() (L2234-2286)
func executeWorkerTask(ctx context.Context, workerName, taskID string, logger *slog.Logger) error {
	logger.Info("pool-worker: executing task",
		slog.String("worker", workerName),
		slog.String("task_id", taskID))

	// The actual handler dispatch will be implemented in handlers.go
	// For Phase 1, this is the goroutine skeleton that replaces
	// Python's multiprocessing.Process fork.
	handler := resolveHandler(workerName)
	if handler == nil {
		logger.Warn("no handler found for worker", slog.String("worker", workerName))
		return nil
	}

	result, err := handler.Execute(ctx, taskID)
	if err != nil {
		logger.Error("pool-worker: task failed",
			slog.String("worker", workerName),
			slog.String("task_id", taskID),
			slog.String("error", err.Error()))
		return err
	}

	logger.Info("pool-worker: task completed",
		slog.String("worker", workerName),
		slog.String("task_id", taskID),
		slog.Bool("success", result.Success))
	return nil
}

// ErrPoolShutdown is returned when submitting to a shutdown pool.
var ErrPoolShutdown = &poolError{"pool is shutting down"}

type poolError struct{ msg string }

func (e *poolError) Error() string { return e.msg }
