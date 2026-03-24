package commands

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os/exec"
	"runtime"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/lipgloss"
	"github.com/spf13/cobra"
)

// Telemetry Structs
type SystemState struct {
	StandbyNodes  int `json:"standbyNodes"`
	ActiveStreams int `json:"activeStreams"`
	TotalNodes    int `json:"totalNodes"`
}

type TaskObj struct {
	Status string `json:"status"`
}

type SnapshotData struct {
	Projects []interface{} `json:"projects"`
	Tasks    []TaskObj     `json:"tasks"`
}

type ESStatsData struct {
	All struct {
		Primaries struct {
			Docs struct {
				Count int `json:"count"`
			} `json:"docs"`
		} `json:"primaries"`
	} `json:"_all"`
}

type VaultStatusData struct {
	Sealed bool `json:"sealed"`
}

var labelStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#00F0FF")).Bold(true).Width(27)
var valueStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#39FF14")).Bold(true)
var warnStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#FFD700")).Bold(true)
var errStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#FF003C")).Bold(true).Blink(true)

func renderRow(label, value string) string {
	return fmt.Sprintf("│ %s %s", labelStyle.Render(label), value)
}

var DoctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Diagnose Flume internal components & swarm health",
	Run: func(cmd *cobra.Command, args []string) {
		client := http.Client{Timeout: 2 * time.Second}

		fmt.Println("\n" + ui.CyberGradient(":: FLUME ECOSYSTEM TELEMETRY DIAGNOSTICS ::") + "\n")

		// 1. Docker
		dockerStat := valueStyle.Render("ONLINE")
		if err := exec.Command("docker", "info").Run(); err != nil {
			dockerStat = errStyle.Render("OFFLINE (Daemon Unreachable)")
		}

		// 2. OpenBao
		vaultStat := errStyle.Render("OFFLINE")
		resp, err := client.Get("http://localhost:8200/v1/sys/seal-status")
		if err == nil {
			defer resp.Body.Close()
			var v VaultStatusData
			json.NewDecoder(resp.Body).Decode(&v)
			if v.Sealed {
				vaultStat = warnStyle.Render("SEALED (LOCKED)")
			} else {
				vaultStat = valueStyle.Render("UNSEALED (ACTIVE)")
			}
		}

		// 3. Elasticsearch
		esStat := errStyle.Render("OFFLINE")
		respES, err := client.Get("http://localhost:9200/_stats/docs")
		astDocs := 0
		if err == nil {
			defer respES.Body.Close()
			var e ESStatsData
			json.NewDecoder(respES.Body).Decode(&e)
			astDocs = e.All.Primaries.Docs.Count
			esStat = valueStyle.Render(fmt.Sprintf("ONLINE (%d AST Nodes Indexed)", astDocs))
		}

		// 4. FastAPI & Metrics
		apiStat := errStyle.Render("OFFLINE")

		readyAgents := 0
		busyAgents := 0
		projects := 0
		queuedWork := 0
		completedWork := 0

		respDash, err := client.Get("http://localhost:8765/api/system-state")
		if err == nil {
			defer respDash.Body.Close()
			apiStat = valueStyle.Render("ONLINE")
			var s SystemState
			json.NewDecoder(respDash.Body).Decode(&s)
			readyAgents = s.StandbyNodes
			busyAgents = s.ActiveStreams
		}

		respSnap, err := client.Get("http://localhost:8765/api/snapshot")
		if err == nil {
			defer respSnap.Body.Close()
			var snap SnapshotData
			json.NewDecoder(respSnap.Body).Decode(&snap)
			projects = len(snap.Projects)
			for _, t := range snap.Tasks {
				if t.Status == "completed" || t.Status == "done" || t.Status == "archived" {
					completedWork++
				} else {
					queuedWork++
				}
			}
		}

		// UI Output
		borderStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("#FFD700"))

		fmt.Println(ui.NeonGreen("Checking Infrastructure Subsystems..."))
		fmt.Println(borderStyle.Render("┌─────────────────────────────────────────────────────────────────┐"))
		fmt.Println(renderRow("Host Target:", valueStyle.Render(fmt.Sprintf("%s_%s", runtime.GOOS, runtime.GOARCH))))
		fmt.Println(renderRow("Compute Topology:", valueStyle.Render(fmt.Sprintf("%d Logical Cores", runtime.NumCPU()))))
		fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
		fmt.Println(renderRow("Docker Daemon:", dockerStat))
		fmt.Println(renderRow("OpenBao KMS:", vaultStat))
		fmt.Println(renderRow("Elasticsearch Engine:", esStat))
		fmt.Println(renderRow("Flume API Orchestrator:", apiStat))
		fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
		fmt.Println(renderRow("Active Projects:", valueStyle.Render(fmt.Sprintf("%d", projects))))
		fmt.Println(renderRow("Work In Queue:", valueStyle.Render(fmt.Sprintf("%d tasks", queuedWork))))
		fmt.Println(renderRow("Completed Work:", valueStyle.Render(fmt.Sprintf("%d tasks", completedWork))))
		fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
		fmt.Println(renderRow("Agents Ready (Standby):", valueStyle.Render(fmt.Sprintf("%d", readyAgents))))
		fmt.Println(renderRow("Agents Executing (Busy):", valueStyle.Render(fmt.Sprintf("%d", busyAgents))))
		fmt.Println(borderStyle.Render("└─────────────────────────────────────────────────────────────────┘"))
		fmt.Println("\n" + ui.NeonGreen("Diagnostic Telemetry Matrix Extracted."))
		fmt.Println()
	},
}
