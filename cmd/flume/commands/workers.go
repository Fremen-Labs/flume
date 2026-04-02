package commands

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

var WorkersCmd = &cobra.Command{
	Use:   "workers",
	Short: "List and manage Flume worker daemons",
	Long:  `Display worker status, or start/stop/restart the agent worker pool.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return listWorkers()
	},
}

var workersStartCmd = &cobra.Command{
	Use:   "start",
	Short: "Start all Flume worker agents",
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()
		result, err := client.Post("/api/workflow/agents/start", nil)
		if err != nil {
			return fmt.Errorf("failed to start workers: %w", err)
		}
		_ = result
		fmt.Println(ui.SuccessBlue("Worker agents started."))
		return listWorkers()
	},
}

var workersStopCmd = &cobra.Command{
	Use:   "stop",
	Short: "Stop all Flume worker agents",
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()
		result, err := client.Post("/api/workflow/agents/stop", nil)
		if err != nil {
			return fmt.Errorf("failed to stop workers: %w", err)
		}
		_ = result
		fmt.Println(ui.WarningGold("Worker agents stopped."))
		return nil
	},
}

var workersRestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Restart all Flume worker agents",
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()
		fmt.Print(ui.WarningGold("Stopping workers..."))
		if _, err := client.Post("/api/workflow/agents/stop", nil); err != nil {
			return fmt.Errorf("failed to stop workers: %w", err)
		}
		time.Sleep(1 * time.Second)
		fmt.Println(ui.SuccessBlue(" stopped."))
		fmt.Print(ui.WarningGold("Starting workers..."))
		if _, err := client.Post("/api/workflow/agents/start", nil); err != nil {
			return fmt.Errorf("failed to start workers: %w", err)
		}
		fmt.Println(ui.SuccessBlue(" started."))
		return listWorkers()
	},
}

func listWorkers() error {
	client := ui.NewFlumeClient()
	data, err := client.Get("/api/workflow/workers")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	status, _ := client.Get("/api/workflow/agents/status")

	fmt.Println(ui.NeonGreen("  WORKERS  "))

	headers := []string{"ID / Hostname", "Role", "Status", "PID", "Current Task", "Uptime"}
	var rows [][]string

	// Merge worker list.
	wList, _ := data["workers"].([]any)
	for _, w := range wList {
		wm, ok := w.(map[string]any)
		if !ok {
			continue
		}
		id := stringVal(wm, "id", stringVal(wm, "hostname", "—"))
		role := stringVal(wm, "role", "—")
		wStatus := stringVal(wm, "status", "unknown")
		pid := stringVal(wm, "pid", "—")
		task := stringVal(wm, "current_task", "—")
		uptime := stringVal(wm, "uptime", "—")
		rows = append(rows, []string{id, role, ui.StatusBadge(wStatus), pid, task, uptime})
	}

	if len(rows) == 0 && status != nil {
		// Fall back to /api/workflow/agents/status format.
		b, _ := json.MarshalIndent(status, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	if len(rows) == 0 {
		fmt.Println(ui.WarningGold("No worker data available."))
		return nil
	}

	fmt.Print(ui.RenderTable(headers, rows))
	return nil
}

func init() {
	WorkersCmd.AddCommand(workersStartCmd)
	WorkersCmd.AddCommand(workersStopCmd)
	WorkersCmd.AddCommand(workersRestartCmd)
}
