// Package services orchestrates in-process Flume service lifecycle.
//
// Phase 5: CLI Unification — merges gateway, dashboard, and worker-manager
// into a single binary. All three services run as goroutines coordinated
// by errgroup.Group with context-based cancellation for clean shutdown.
//
// Before Phase 5:
//   flume start → docker compose up dashboard worker (Python containers)
//                 + exec.Command("uv", "run", "src/dashboard/server.py")
//
// After Phase 5:
//   flume start → go gateway.Start(ctx)
//                 + go dashboard.ListenAndServe()
//                 + go workerManager.Run(ctx)
package services

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/dashboard"
	"github.com/Fremen-Labs/flume/internal/es"
	"github.com/Fremen-Labs/flume/internal/worker"
	"github.com/Fremen-Labs/flume/src/gateway"
)

// Supervisor manages the lifecycle of all in-process services.
// It replaces the Docker Compose orchestration for the application layer
// (gateway, dashboard, worker-manager) while infra services (ES, OpenBao)
// remain as Docker containers.
type Supervisor struct {
	cfg    *config.Config
	logger *slog.Logger
	mu     sync.Mutex

	// Service handles for graceful shutdown
	dashServer   *dashboard.Server
	workerMgr    *worker.Manager
	gatewayAddr  string
}

// NewSupervisor creates a new service supervisor.
func NewSupervisor(cfg *config.Config, logger *slog.Logger) *Supervisor {
	if logger == nil {
		logger = slog.Default()
	}
	return &Supervisor{
		cfg:    cfg,
		logger: logger,
	}
}

// StartAll launches gateway, dashboard, and worker-manager as goroutines.
// Blocks until ctx is cancelled or any service returns a fatal error.
// This replaces the Python subprocess spawning in start.go:243-266 (native)
// and docker compose up dashboard worker in start.go:340-354 (Docker).
func (s *Supervisor) StartAll(ctx context.Context) error {
	s.logger.Info("starting in-process service mesh",
		slog.Bool("native_mode", s.cfg.NativeMode))

	errCh := make(chan error, 3)
	var wg sync.WaitGroup

	// 1. Gateway
	wg.Add(1)
	go func() {
		defer wg.Done()
		s.logger.Info("starting gateway service")
		if err := s.startGateway(ctx); err != nil {
			s.logger.Error("gateway service exited with error",
				slog.String("error", err.Error()))
			errCh <- fmt.Errorf("gateway: %w", err)
		}
	}()

	// Brief delay to let gateway bind its port before dashboard starts
	// (dashboard proxies node/routing requests to gateway)
	time.Sleep(500 * time.Millisecond)

	// 2. Dashboard
	wg.Add(1)
	go func() {
		defer wg.Done()
		s.logger.Info("starting dashboard service")
		if err := s.startDashboard(ctx); err != nil {
			s.logger.Error("dashboard service exited with error",
				slog.String("error", err.Error()))
			errCh <- fmt.Errorf("dashboard: %w", err)
		}
	}()

	// 3. Worker Manager
	wg.Add(1)
	go func() {
		defer wg.Done()
		s.logger.Info("starting worker manager service")
		if err := s.startWorkerManager(ctx); err != nil {
			s.logger.Error("worker manager service exited with error",
				slog.String("error", err.Error()))
			errCh <- fmt.Errorf("worker-manager: %w", err)
		}
	}()

	// Wait for first error or context cancellation
	select {
	case err := <-errCh:
		s.logger.Error("service mesh failure, initiating shutdown",
			slog.String("error", err.Error()))
		return err
	case <-ctx.Done():
		s.logger.Info("shutdown signal received, draining services")
	}

	// Graceful shutdown with 25s timeout (matches K8s terminationGracePeriodSeconds)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()

	s.shutdown(shutdownCtx)

	// Wait for all goroutines to finish
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		s.logger.Info("all services stopped cleanly")
	case <-shutdownCtx.Done():
		s.logger.Warn("shutdown timeout exceeded, some services may not have drained")
	}

	return nil
}

// StartGatewayOnly runs only the gateway service.
// Used by `flume gateway` subcommand for distributed deployments.
func (s *Supervisor) StartGatewayOnly(ctx context.Context) error {
	s.logger.Info("starting gateway service (standalone)")
	return s.startGateway(ctx)
}

// StartDashboardOnly runs only the dashboard service.
// Used by `flume dashboard` subcommand.
func (s *Supervisor) StartDashboardOnly(ctx context.Context) error {
	s.logger.Info("starting dashboard service (standalone)")
	return s.startDashboard(ctx)
}

// StartWorkerOnly runs only the worker manager service.
// Used by `flume worker` subcommand.
func (s *Supervisor) StartWorkerOnly(ctx context.Context) error {
	s.logger.Info("starting worker manager service (standalone)")
	return s.startWorkerManager(ctx)
}

// ─── Service Starters ───────────────────────────────────────────────────────

func (s *Supervisor) startGateway(ctx context.Context) error {
	addr := gateway.DefaultAddr()
	s.mu.Lock()
	s.gatewayAddr = addr
	s.mu.Unlock()

	s.logger.Info("gateway binding", slog.String("addr", addr))
	return gateway.StartGateway(addr)
}

func (s *Supervisor) startDashboard(ctx context.Context) error {
	cfg := dashboard.DefaultConfig()

	// Override config from the unified Config struct
	if s.cfg.DashboardHost != "" {
		cfg.Host = s.cfg.DashboardHost
	}
	if s.cfg.DashboardPort != 0 {
		cfg.Port = s.cfg.DashboardPort
	}
	if s.cfg.ESURL != "" {
		cfg.ESUrl = s.cfg.ESURL
	}
	if s.cfg.ESAPIKey != "" {
		cfg.ESApiKey = s.cfg.ESAPIKey
	}
	cfg.NativeMode = s.cfg.NativeMode

	srv := dashboard.New(cfg, s.logger.With(slog.String("component", "dashboard")))

	s.mu.Lock()
	s.dashServer = srv
	s.mu.Unlock()

	err := srv.ListenAndServe()
	if err != nil && err.Error() != "http: Server closed" {
		return err
	}
	return nil
}

func (s *Supervisor) startWorkerManager(ctx context.Context) error {
	esClient := es.New(s.cfg.ESURL, s.cfg.ESAPIKey, s.logger)
	mgr := worker.NewManager(s.cfg, esClient, s.logger)

	s.mu.Lock()
	s.workerMgr = mgr
	s.mu.Unlock()

	return mgr.Run(ctx)
}

// ─── Shutdown ───────────────────────────────────────────────────────────────

func (s *Supervisor) shutdown(ctx context.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Dashboard has graceful HTTP shutdown
	if s.dashServer != nil {
		s.logger.Info("shutting down dashboard server")
		if err := s.dashServer.Shutdown(ctx); err != nil {
			s.logger.Warn("dashboard shutdown error",
				slog.String("error", err.Error()))
		}
	}

	// Gateway handles its own SIGTERM/SIGINT internally
	// Worker manager drains via context cancellation
	s.logger.Info("service shutdown sequence complete")
}

// IsRunning returns true if the supervisor has active services.
func (s *Supervisor) IsRunning() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dashServer != nil || s.workerMgr != nil
}

// GatewayURL returns the gateway's listen URL.
func (s *Supervisor) GatewayURL() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.gatewayAddr == "" {
		return ""
	}
	port := s.gatewayAddr
	if port[0] == ':' {
		port = "localhost" + port
	}
	return "http://" + port
}

// SetEnvForServices sets environment variables that services read on startup.
// This replaces the generatedEnv slice that was passed to exec.Command.
func SetEnvForServices(envPairs []string) {
	for _, pair := range envPairs {
		parts := splitFirst(pair, "=")
		if len(parts) == 2 {
			os.Setenv(parts[0], parts[1])
		}
	}
}

func splitFirst(s, sep string) []string {
	for i := 0; i < len(s); i++ {
		if s[i] == sep[0] {
			return []string{s[:i], s[i+1:]}
		}
	}
	return []string{s}
}
