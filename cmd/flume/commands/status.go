package commands

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

var (
	statusWatch bool
	statusJSON  bool
)

var StatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Display the live health and status of the Flume ecosystem",
	Long: `Renders a rich status panel showing infrastructure health, worker states,
and task queue metrics. Use --watch for a live 2-second refresh loop.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()

		if statusWatch {
			for {
				fmt.Print("\033[H\033[2J") // ANSI clear screen
				if err := renderStatus(client, statusJSON); err != nil {
					log.Warn("Status fetch failed", "error", err)
				}
				time.Sleep(2 * time.Second)
			}
		}
		return renderStatus(client, statusJSON)
	},
}

func renderStatus(client *ui.FlumeClient, asJSON bool) error {
	health, err := client.Get("/api/health")
	if err != nil {
		return fmt.Errorf("dashboard unreachable (%s) — is Flume running? Try: flume start", client.BaseURL)
	}

	vault, _ := client.Get("/api/vault/status")
	workers, _ := client.Get("/api/workflow/workers")
	state, _ := client.Get("/api/system-state")

	if asJSON {
		out := map[string]any{
			"health":  health,
			"vault":   vault,
			"workers": workers,
			"state":   state,
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	// ── Header ──────────────────────────────────────────────────────────────
	ts := time.Now().Format("15:04:05")
	fmt.Println(ui.NeonGreen(fmt.Sprintf("  FLUME STATUS  ·  %s  ·  %s  ", client.BaseURL, ts)))
	fmt.Println()

	// ── Infrastructure ───────────────────────────────────────────────────────
	fmt.Println(ui.WarningGold(" INFRASTRUCTURE"))
	dashStatus := "Healthy"
	if health["status"] != "ok" {
		dashStatus = "Degraded"
	}
	fmt.Printf("  Dashboard  %s   %s\n", ui.StatusBadge(dashStatus), client.BaseURL)

	vaultSealed := true
	if vault != nil {
		if s, ok := vault["sealed"].(bool); ok {
			vaultSealed = s
		}
	}
	vaultLabel := "Unsealed"
	if vaultSealed {
		vaultLabel = "Sealed"
	}
	fmt.Printf("  Vault      %s\n", ui.StatusBadge(vaultLabel))

	esHealth := "Unknown"
	if state != nil {
		if es, ok := state["es_health"].(string); ok {
			esHealth = strings.ToUpper(es)
		}
	}
	fmt.Printf("  ES Cluster %s\n", ui.StatusBadge(esHealth))
	fmt.Println()

	// ── Workers ──────────────────────────────────────────────────────────────
	fmt.Println(ui.WarningGold(" WORKERS"))
	if workers != nil {
		headers := []string{"Worker", "Status", "Current Task", "Model"}
		var rows [][]string
		if wList, ok := workers["workers"].([]any); ok {
			for _, w := range wList {
				wm, ok := w.(map[string]any)
				if !ok {
					continue
				}
				name := stringVal(wm, "role", "—")
				status := stringVal(wm, "status", "unknown")
				task := stringVal(wm, "current_task", "—")
				model := stringVal(wm, "model", "—")
				rows = append(rows, []string{name, ui.StatusBadge(status), task, model})
			}
		}
		if len(rows) == 0 {
			fmt.Println("  No worker data available.")
		} else {
			fmt.Print(ui.RenderTable(headers, rows))
		}
	} else {
		fmt.Println("  Worker data unavailable.")
	}
	fmt.Println()

	// ── Task Queue ────────────────────────────────────────────────────────────
	fmt.Println(ui.WarningGold(" TASK QUEUE"))
	counts := map[string]int{"ready": 0, "in_progress": 0, "blocked": 0, "done": 0}
	if state != nil {
		if tasks, ok := state["tasks"].([]any); ok {
			for _, t := range tasks {
				tm, ok := t.(map[string]any)
				if !ok {
					continue
				}
				s := stringVal(tm, "status", "")
				if _, tracked := counts[s]; tracked {
					counts[s]++
				}
			}
		}
	}
	fmt.Printf("  %s  │  %s  │  %s  │  %s\n",
		ui.StatusBadge(fmt.Sprintf("Ready: %d", counts["ready"])),
		ui.StatusBadge(fmt.Sprintf("In Progress: %d", counts["in_progress"])),
		ui.StatusBadge(fmt.Sprintf("Blocked: %d", counts["blocked"])),
		ui.SuccessBlue(fmt.Sprintf("Done: %d", counts["done"])),
	)
	fmt.Println()
	return nil
}

func init() {
	StatusCmd.Flags().BoolVarP(&statusWatch, "watch", "w", false, "Live refresh every 2 seconds")
	StatusCmd.Flags().BoolVarP(&statusJSON, "json", "j", false, "Output raw JSON")
}
