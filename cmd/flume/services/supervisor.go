// Package services orchestrates in-process Flume service lifecycle.
package services

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/dashboard"
	"github.com/Fremen-Labs/flume/src/gateway"
)

// Supervisor manages the lifecycle of in-process services (Gateway and Dashboard).
type Supervisor struct {
	cfg    *config.Config
	logger *slog.Logger
	mu     sync.Mutex

	// Service handles for graceful shutdown
	dashServer  *dashboard.Server
	gatewayAddr string
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

// StartAll launches gateway and dashboard as goroutines.
func (s *Supervisor) StartAll(ctx context.Context) error {
	s.logger.Info("starting in-process service mesh",
		slog.Bool("native_mode", s.cfg.NativeMode))

	errCh := make(chan error, 2)
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
	time.Sleep(500 * time.Millisecond)

	s.mu.Lock()
	if s.gatewayAddr != "" {
		gwURL := "http://localhost" + s.gatewayAddr
		if s.gatewayAddr[0] != ':' {
			gwURL = "http://" + s.gatewayAddr
		}
		os.Setenv("FLUME_GATEWAY_URL", gwURL)
	}
	s.mu.Unlock()

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

	// Wait for first error or context cancellation
	select {
	case err := <-errCh:
		s.logger.Error("service mesh failure, initiating shutdown",
			slog.String("error", err.Error()))
		return err
	case <-ctx.Done():
		s.logger.Info("shutdown signal received, draining services")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()

	s.shutdown(shutdownCtx)

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
func (s *Supervisor) StartGatewayOnly(ctx context.Context) error {
	s.logger.Info("starting gateway service (standalone)")
	return s.startGateway(ctx)
}

// StartDashboardOnly runs only the dashboard service.
func (s *Supervisor) StartDashboardOnly(ctx context.Context) error {
	s.logger.Info("starting dashboard service (standalone)")
	return s.startDashboard(ctx)
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

	if cfg.StaticRoot == "" {
		if d := discoverLocalFrontendDist(); d != "" {
			cfg.StaticRoot = d
			s.logger.Info("auto-discovered frontend static root", slog.String("path", d))
		}
	}

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

// ─── Shutdown ───────────────────────────────────────────────────────────────

func (s *Supervisor) shutdown(ctx context.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.dashServer != nil {
		s.logger.Info("shutting down dashboard server")
		if err := s.dashServer.Shutdown(ctx); err != nil {
			s.logger.Warn("dashboard shutdown error",
				slog.String("error", err.Error()))
		}
	}

	s.logger.Info("service shutdown sequence complete")
}

// IsRunning returns true if the supervisor has active services.
func (s *Supervisor) IsRunning() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dashServer != nil
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

func discoverLocalFrontendDist() string {
	candidates := []string{
		"src/frontend/dist",
		"../src/frontend/dist",
		"../../src/frontend/dist",
	}

	if exe, err := os.Executable(); err == nil {
		base := filepath.Dir(exe)
		candidates = append(candidates,
			filepath.Join(base, "src", "frontend", "dist"),
			filepath.Join(base, "..", "src", "frontend", "dist"),
			filepath.Join(base, "..", "..", "src", "frontend", "dist"),
		)
	}

	for _, c := range candidates {
		abs, err := filepath.Abs(c)
		if err != nil {
			continue
		}
		if st, err := os.Stat(filepath.Join(abs, "index.html")); err == nil && !st.IsDir() {
			return abs
		}
	}
	return ""
}

