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
	logsFollow bool
	logsJSON   bool
	logsWorker string
	logsN      int
)

var LogsCmd = &cobra.Command{
	Use:   "logs",
	Short: "Stream or tail Flume ecosystem logs",
	Long: `Fetches logs from the Flume dashboard log API.
Use --follow for continuous polling. Filter by worker role with --worker.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()

		if logsFollow {
			log.Info("Following logs (Ctrl+C to stop)...")
			var lastLen int
			for {
				entries, err := fetchLogs(client)
				if err != nil {
					log.Warn("Log fetch failed", "error", err)
					time.Sleep(2 * time.Second)
					continue
				}
				if len(entries) > lastLen {
					for _, e := range entries[lastLen:] {
						printLogEntry(e, logsWorker, logsJSON)
					}
					lastLen = len(entries)
				}
				time.Sleep(2 * time.Second)
			}
		}

		entries, err := fetchLogs(client)
		if err != nil {
			return fmt.Errorf("failed to fetch logs: %w", err)
		}

		// Apply -n cap.
		if logsN > 0 && len(entries) > logsN {
			entries = entries[len(entries)-logsN:]
		}

		for _, e := range entries {
			printLogEntry(e, logsWorker, logsJSON)
		}
		return nil
	},
}

func fetchLogs(client *ui.FlumeClient) ([]map[string]any, error) {
	raw, err := client.GetRaw("/api/logs")
	if err != nil {
		return nil, err
	}
	// The /api/logs endpoint may return an array or a wrapping object.
	var entries []map[string]any
	if err := json.Unmarshal(raw, &entries); err != nil {
		// Try wrapped object.
		var wrapper map[string]any
		if err2 := json.Unmarshal(raw, &wrapper); err2 != nil {
			return nil, fmt.Errorf("unexpected log format: %w", err)
		}
		if arr, ok := wrapper["logs"].([]any); ok {
			for _, item := range arr {
				if m, ok := item.(map[string]any); ok {
					entries = append(entries, m)
				}
			}
		}
	}
	return entries, nil
}

func printLogEntry(e map[string]any, workerFilter string, asJSON bool) {
	if asJSON {
		b, _ := json.Marshal(e)
		fmt.Println(string(b))
		return
	}

	ts := stringVal(e, "timestamp", stringVal(e, "time", ""))
	level := strings.ToUpper(stringVal(e, "level", "INFO"))
	service := stringVal(e, "service", stringVal(e, "logger", "flume"))
	msg := stringVal(e, "message", stringVal(e, "msg", fmt.Sprintf("%v", e)))

	// Worker filter.
	if workerFilter != "" && !strings.Contains(strings.ToLower(service), strings.ToLower(workerFilter)) {
		return
	}

	// Colour by level.
	var levelStr string
	switch level {
	case "ERROR", "CRITICAL":
		levelStr = ui.ErrorRed(fmt.Sprintf("%-7s", level))
	case "WARNING", "WARN":
		levelStr = ui.WarningGold(fmt.Sprintf("%-7s", level))
	case "DEBUG":
		levelStr = fmt.Sprintf("%-7s", level) // dim
	default:
		levelStr = ui.SuccessBlue(fmt.Sprintf("%-7s", level))
	}

	fmt.Printf("[%s] %s | %-20s → %s\n", ts, levelStr, service, msg)
}

func init() {
	LogsCmd.Flags().BoolVarP(&logsFollow, "follow", "f", false, "Continuously poll for new log entries")
	LogsCmd.Flags().BoolVarP(&logsJSON, "json", "j", false, "Output raw JSON log entries")
	LogsCmd.Flags().StringVarP(&logsWorker, "worker", "w", "", "Filter logs by worker role (e.g. manager, handlers)")
	LogsCmd.Flags().IntVarP(&logsN, "lines", "n", 50, "Number of lines to tail (0 = all)")
}
