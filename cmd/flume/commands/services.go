package commands

import (
	"log/slog"

	"github.com/Fremen-Labs/flume/cmd/flume/services"
	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

// GatewayCmd runs only the gateway service.
// Used for distributed deployments where gateway, dashboard,
// and worker-manager run on separate nodes.
var GatewayCmd = &cobra.Command{
	Use:   "gateway",
	Short: "Run the LLM Gateway service (standalone)",
	Long: `Starts only the Flume Gateway service. Use this for distributed deployments
where gateway, dashboard, and worker-manager run as separate processes.

For single-node deployments, use 'flume start' which runs all services.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx := cmd.Context()
		log.Info("Starting Flume Gateway (standalone mode)")

		logger := slog.Default()
		cfg := config.Load(ctx, logger)
		sup := services.NewSupervisor(cfg, logger)
		return sup.StartGatewayOnly(ctx)
	},
}

// DashboardCmd runs only the dashboard API service.
var DashboardCmd = &cobra.Command{
	Use:   "dashboard",
	Short: "Run the Dashboard API service (standalone)",
	Long: `Starts only the Flume Dashboard API server. Use this for distributed deployments
where services run as separate processes.

For single-node deployments, use 'flume start' which runs all services.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx := cmd.Context()
		log.Info("Starting Flume Dashboard (standalone mode)")

		logger := slog.Default()
		cfg := config.Load(ctx, logger)
		sup := services.NewSupervisor(cfg, logger)
		return sup.StartDashboardOnly(ctx)
	},
}

// WorkerManagerCmd runs only the worker-manager service.
var WorkerManagerCmd = &cobra.Command{
	Use:   "worker-manager",
	Short: "Run the Worker Manager service (standalone)",
	Long: `Starts only the Flume Worker Manager heartbeat loop. Use this for distributed deployments
where services run as separate processes.

For single-node deployments, use 'flume start' which runs all services.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx := cmd.Context()
		log.Info("Starting Flume Worker Manager (standalone mode)")

		logger := slog.Default()
		cfg := config.Load(ctx, logger)
		sup := services.NewSupervisor(cfg, logger)
		return sup.StartWorkerOnly(ctx)
	},
}
