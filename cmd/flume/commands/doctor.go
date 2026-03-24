package commands

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os/exec"
	"runtime"
	"sync"
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

// DiagnosticsReport stores aggregated telemetry globally.
type DiagnosticsReport struct {
	DockerOnline    bool
	VaultSealed     bool
	VaultOnline     bool
	ElasticOnline   bool
	ElasticASTCount int

	ApiOnline     bool
	Projects      int
	QueuedWork    int
	CompletedWork int
	AgentsReady   int
	AgentsBusy    int

	mu sync.Mutex
}

var labelStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#00F0FF")).Bold(true).Width(27)
var valueStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#39FF14")).Bold(true)
var warnStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#FFD700")).Bold(true)
var errStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("#FF003C")).Bold(true).Blink(true)

func renderRow(label, value string) string {
	return fmt.Sprintf("│ %s %s", labelStyle.Render(label), value)
}

// Data Fetching Subsystems

func fetchDocker(report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	err := exec.Command("docker", "info").Run()

	report.mu.Lock()
	defer report.mu.Unlock()
	report.DockerOnline = (err == nil)
}

func fetchVault(client *http.Client, vaultURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	resp, err := client.Get(vaultURL + "/v1/sys/seal-status")

	if err == nil {
		defer resp.Body.Close()
		report.mu.Lock()
		report.VaultOnline = true
		var v VaultStatusData
		if json.NewDecoder(resp.Body).Decode(&v) == nil {
			report.VaultSealed = v.Sealed
		}
		report.mu.Unlock()
	}
}

func fetchElasticsearch(client *http.Client, esURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	respES, err := client.Get(esURL + "/_stats/docs")

	if err == nil {
		defer respES.Body.Close()
		report.mu.Lock()
		report.ElasticOnline = true
		var e ESStatsData
		if json.NewDecoder(respES.Body).Decode(&e) == nil {
			report.ElasticASTCount = e.All.Primaries.Docs.Count
		}
		report.mu.Unlock()
	}
}

func fetchDashboard(client *http.Client, apiURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()

	// Query both system-state and snapshot efficiently.
	statResp, errStat := client.Get(apiURL + "/api/system-state")
	snapResp, errSnap := client.Get(apiURL + "/api/snapshot")

	report.mu.Lock()
	defer report.mu.Unlock()

	if errStat == nil {
		defer statResp.Body.Close()
		report.ApiOnline = true
		var s SystemState
		if json.NewDecoder(statResp.Body).Decode(&s) == nil {
			report.AgentsReady = s.StandbyNodes
			report.AgentsBusy = s.ActiveStreams
		}
	}
	if errSnap == nil {
		defer snapResp.Body.Close()
		var snap SnapshotData
		if json.NewDecoder(snapResp.Body).Decode(&snap) == nil {
			report.Projects = len(snap.Projects)
			for _, t := range snap.Tasks {
				if t.Status == "completed" || t.Status == "done" || t.Status == "archived" {
					report.CompletedWork++
				} else {
					report.QueuedWork++
				}
			}
		}
	}
}

// Presentation Layer Mapping
func renderDiagnosticReport(report *DiagnosticsReport) {
	// Status resolutions
	dockerStat := errStyle.Render("OFFLINE (Daemon Unreachable)")
	if report.DockerOnline {
		dockerStat = valueStyle.Render("ONLINE")
	}

	vaultStat := errStyle.Render("OFFLINE")
	if report.VaultOnline {
		if report.VaultSealed {
			vaultStat = warnStyle.Render("SEALED (LOCKED)")
		} else {
			vaultStat = valueStyle.Render("UNSEALED (ACTIVE)")
		}
	}

	esStat := errStyle.Render("OFFLINE")
	if report.ElasticOnline {
		esStat = valueStyle.Render(fmt.Sprintf("ONLINE (%d AST Nodes Indexed)", report.ElasticASTCount))
	}

	apiStat := errStyle.Render("OFFLINE")
	if report.ApiOnline {
		apiStat = valueStyle.Render("ONLINE")
	}

	// UI Layout
	borderStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("#FFD700"))

	fmt.Println(ui.NeonGreen("Checking Infrastructure Subsystems Parallelly..."))
	fmt.Println(borderStyle.Render("┌─────────────────────────────────────────────────────────────────┐"))
	fmt.Println(renderRow("Host Target:", valueStyle.Render(fmt.Sprintf("%s_%s", runtime.GOOS, runtime.GOARCH))))
	fmt.Println(renderRow("Compute Topology:", valueStyle.Render(fmt.Sprintf("%d Logical Cores", runtime.NumCPU()))))
	fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
	fmt.Println(renderRow("Docker Daemon:", dockerStat))
	fmt.Println(renderRow("OpenBao KMS:", vaultStat))
	fmt.Println(renderRow("Elasticsearch Engine:", esStat))
	fmt.Println(renderRow("Flume API Orchestrator:", apiStat))
	fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
	fmt.Println(renderRow("Active Projects:", valueStyle.Render(fmt.Sprintf("%d", report.Projects))))
	fmt.Println(renderRow("Work In Queue:", valueStyle.Render(fmt.Sprintf("%d tasks", report.QueuedWork))))
	fmt.Println(renderRow("Completed Work:", valueStyle.Render(fmt.Sprintf("%d tasks", report.CompletedWork))))
	fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
	fmt.Println(renderRow("Agents Ready (Standby):", valueStyle.Render(fmt.Sprintf("%d", report.AgentsReady))))
	fmt.Println(renderRow("Agents Executing (Busy):", valueStyle.Render(fmt.Sprintf("%d", report.AgentsBusy))))
	fmt.Println(borderStyle.Render("└─────────────────────────────────────────────────────────────────┘"))
	fmt.Println("\n" + ui.NeonGreen("Diagnostic Telemetry Matrix Extracted Parallel."))
	fmt.Println()
}

var DoctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Diagnose Flume internal components & swarm health natively",
	Run: func(cmd *cobra.Command, args []string) {
		// Externalized Configurations
		esURL, _ := cmd.Flags().GetString("es-url")
		vaultURL, _ := cmd.Flags().GetString("vault-url")
		dashboardURL, _ := cmd.Flags().GetString("dashboard-url")

		client := &http.Client{Timeout: 3 * time.Second}
		report := &DiagnosticsReport{}
		var wg sync.WaitGroup

		fmt.Println("\n" + ui.CyberGradient(":: FLUME ECOSYSTEM TELEMETRY DIAGNOSTICS ::") + "\n")

		// Parallel Telemetry Execution Maps (Goroutines)
		wg.Add(4)
		go fetchDocker(report, &wg)
		go fetchVault(client, vaultURL, report, &wg)
		go fetchElasticsearch(client, esURL, report, &wg)
		go fetchDashboard(client, dashboardURL, report, &wg)

		wg.Wait() // Block execution safely until all endpoints respond or trace out

		// Dispatch Rendering
		renderDiagnosticReport(report)
	},
}

func init() {
	DoctorCmd.Flags().StringP("es-url", "e", "http://localhost:9200", "Elasticsearch Diagnostic Endpoint")
	DoctorCmd.Flags().StringP("vault-url", "v", "http://localhost:8200", "OpenBao Telemetry Endpoint")
	DoctorCmd.Flags().StringP("dashboard-url", "d", "http://localhost:8765", "Flume API Dashboard Endpoint")
}
