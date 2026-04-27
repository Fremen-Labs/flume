package commands

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"runtime"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/lipgloss"
	"github.com/spf13/cobra"
)

type SystemState struct {
	StandbyNodes  int `json:"standbyNodes"`
	ActiveStreams int `json:"activeStreams"`
	TotalNodes    int `json:"totalNodes"`
}

type ESHealthData struct {
	Status string `json:"status"`
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

type DeterminismStatus struct {
	Status      string `json:"status"`
	Description string `json:"description"`
}

// DiagnosticsReport stores aggregated telemetry globally.
type DiagnosticsReport struct {
	DockerOnline    bool   `json:"dockerOnline"`
	VaultSealed     bool   `json:"vaultSealed"`
	VaultOnline     bool   `json:"vaultOnline"`
	ElasticOnline   bool   `json:"elasticOnline"`
	ElasticStatus   string `json:"elasticStatus"`
	ElasticASTCount int    `json:"elasticAstCount"`

	ApiOnline     bool `json:"apiOnline"`
	Projects      int  `json:"projects"`
	QueuedWork    int  `json:"queuedWork"`
	CompletedWork int  `json:"completedWork"`
	AgentsReady   int  `json:"agentsReady"`
	AgentsBusy    int  `json:"agentsBusy"`

	LlmOnline   bool   `json:"llmOnline"`
	LlmTarget   string `json:"llmTarget"`
	LlmLatency  string `json:"llmLatency"`

	// Deep probe results (only populated with --deep)
	DeepProbeRan     bool    `json:"deepProbeRan"`
	DeepProbeOk      bool    `json:"deepProbeOk"`
	DeepProbeLatency string  `json:"deepProbeLatency,omitempty"`
	DeepProbeTokens  int     `json:"deepProbeTokens,omitempty"`
	DeepProbeTPS     float64 `json:"deepProbeTps,omitempty"`
	DeepProbeError   string  `json:"deepProbeError,omitempty"`

	NativeDeterminism DeterminismStatus `json:"nativeDeterminism"`

	Suggestions []string `json:"suggestions"`

	mu sync.Mutex `json:"-"`
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
	report.mu.Lock()
	defer report.mu.Unlock()

	err := exec.Command("docker", "info").Run()
	report.DockerOnline = (err == nil)
	if !report.DockerOnline {
		report.Suggestions = append(report.Suggestions, "Docker Daemon is unreachable. Start Docker Desktop or OrbStack.")
	}
}

func fetchVault(client *http.Client, vaultURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	report.mu.Lock()
	defer report.mu.Unlock()

	resp, err := client.Get(vaultURL + "/v1/sys/seal-status")
	if err == nil && resp.StatusCode == 200 {
		defer resp.Body.Close()
		report.VaultOnline = true
		var v VaultStatusData
		if json.NewDecoder(resp.Body).Decode(&v) == nil {
			report.VaultSealed = v.Sealed
			if report.VaultSealed {
				report.Suggestions = append(report.Suggestions, "OpenBao is SEALED. Ensure `flume start` booted properly without interruptions.")
			}
		}
	} else if err == nil && resp.StatusCode == 503 {
	    defer resp.Body.Close()
	    report.VaultOnline = true
	    report.VaultSealed = true
	    report.Suggestions = append(report.Suggestions, "OpenBao is SEALED (503). Ensure `docker-compose` executed the `bootstrap` container successfully.")
	} else if err == nil && resp.StatusCode == 501 {
        defer resp.Body.Close()
        report.VaultOnline = true
        report.VaultSealed = true
        report.Suggestions = append(report.Suggestions, "OpenBao is UNINITIALIZED (501). Reboot `flume start -n` explicitly to force bootstrap cluster mapping.")
	} else {
		report.Suggestions = append(report.Suggestions, "OpenBao container is completely offline. Run `flume destroy` and `flume start -n` to rebuild cluster topology.")
	}
}

func fetchElasticsearch(client *http.Client, esURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	report.mu.Lock()
	defer report.mu.Unlock()

	respES, err := client.Get(esURL + "/_stats/docs")
	if err == nil {
		defer respES.Body.Close()
		report.ElasticOnline = true
		var e ESStatsData
		if json.NewDecoder(respES.Body).Decode(&e) == nil {
			report.ElasticASTCount = e.All.Primaries.Docs.Count
		}
	}
}

func fetchDashboard(client *http.Client, apiURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	report.mu.Lock()
	defer report.mu.Unlock()

	// Query both system-state and snapshot efficiently.
	statResp, errStat := client.Get(apiURL + "/api/system-state")
	snapResp, errSnap := client.Get(apiURL + "/api/snapshot")

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

	if !report.ApiOnline {
		report.Suggestions = append(report.Suggestions, "Flume REST API is unreachable. Check for port 8765 collisions natively.")
	}
}

func fetchLlmGateway(client *http.Client, baseURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	report.mu.Lock()
	defer report.mu.Unlock()

	report.LlmTarget = baseURL
	start := time.Now()
	resp, err := client.Get(baseURL + "/models")
	latency := time.Since(start)

	if err == nil {
		defer resp.Body.Close()
		report.LlmOnline = true
		report.LlmLatency = latency.Truncate(time.Millisecond).String()
	} else {
		report.Suggestions = append(report.Suggestions, fmt.Sprintf("LLM Engine natively unreachable at %s. Ensure Exo or Ollama is running and LOCAL_LLM_HOST is configured.", baseURL))
	}
}

func fetchNativeDeterminism(client *http.Client, dispatcherURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()
	
	resp, err := client.Get(dispatcherURL + "/healthz")
	
	report.mu.Lock()
	defer report.mu.Unlock()

	if err == nil && resp.StatusCode == 200 {
		defer resp.Body.Close()
		report.NativeDeterminism = DeterminismStatus{
			Status:      "ACTIVE",
			Description: "Dispatcher running natively (" + runtime.Version() + ")",
		}
	} else {
		report.NativeDeterminism = DeterminismStatus{
			Status:      "DEGRADED",
			Description: "Dispatcher Offline (" + runtime.Version() + ")",
		}
		report.Suggestions = append(report.Suggestions, "Native DAG Dispatcher is offline at port 8766. Run `flume dispatch` to ensure deterministic execution is healthy.")
	}
}

// fetchDeepLLMProbe sends a timed test prompt through the Gateway /v1/chat/completions
// endpoint and measures actual inference speed (tokens/sec). This catches slow
// models that would exceed the planner timeout before users hit "Plan New Work".
func fetchDeepLLMProbe(gatewayURL string, report *DiagnosticsReport, wg *sync.WaitGroup) {
	defer wg.Done()

	// Use a generous timeout — the whole point is measuring slow models.
	client := &http.Client{Timeout: 60 * time.Second}

	payload := map[string]interface{}{
		"model": "auto",
		"messages": []map[string]string{
			{"role": "user", "content": "Respond with exactly one sentence: What is 2+2?"},
		},
		"max_tokens": 50,
		"temperature": 0.0,
	}
	body, _ := json.Marshal(payload)

	start := time.Now()
	resp, err := client.Post(gatewayURL+"/v1/chat/completions", "application/json", bytes.NewReader(body))
	latency := time.Since(start)

	report.mu.Lock()
	defer report.mu.Unlock()

	report.DeepProbeRan = true

	if err != nil {
		report.DeepProbeError = fmt.Sprintf("Gateway unreachable: %v", err)
		report.Suggestions = append(report.Suggestions,
			"Deep LLM probe failed — Gateway did not respond within 60s. Check that Ollama is loaded and responsive.")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		respBody, _ := io.ReadAll(resp.Body)
		report.DeepProbeError = fmt.Sprintf("HTTP %d: %s", resp.StatusCode, truncateBytes(respBody, 200))
		report.Suggestions = append(report.Suggestions,
			fmt.Sprintf("Deep LLM probe returned HTTP %d. Ensure your model is loaded in Ollama.", resp.StatusCode))
		return
	}

	var result struct {
		Usage struct {
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err == nil {
		report.DeepProbeTokens = result.Usage.CompletionTokens
		if result.Usage.CompletionTokens > 0 {
			report.DeepProbeTPS = float64(result.Usage.CompletionTokens) / latency.Seconds()
		}
	}

	report.DeepProbeOk = true
	report.DeepProbeLatency = latency.Truncate(time.Millisecond).String()

	// Warn if inference is dangerously slow for planning workloads.
	if latency > 30*time.Second {
		report.Suggestions = append(report.Suggestions,
			fmt.Sprintf("LLM inference took %s for a trivial prompt. Planning complex tasks will likely exceed the 300s timeout. Consider using a smaller/faster model or increasing FLUME_PLANNER_TIMEOUT_SECONDS.",
				latency.Truncate(time.Second)))
	} else if latency > 10*time.Second {
		report.Suggestions = append(report.Suggestions,
			fmt.Sprintf("LLM inference took %s — adequate but slow. Complex plans may approach the 300s timeout.",
				latency.Truncate(time.Second)))
	}
}

// truncateBytes returns the first n bytes of b as a string.
func truncateBytes(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[:n]) + "…"
}

// Presentation Layer Mapping
func renderDiagnosticReport(report *DiagnosticsReport, jsonOutput bool) {
	if jsonOutput {
		data, _ := json.MarshalIndent(report, "", "  ")
		fmt.Println(string(data))
		return
	}

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
		esHealthColor := valueStyle
		if report.ElasticStatus == "yellow" {
			esHealthColor = warnStyle
		} else if report.ElasticStatus == "red" {
			esHealthColor = errStyle
		}
		
		esStat = valueStyle.Render("ONLINE") + " (Status: " + esHealthColor.Render(report.ElasticStatus) + fmt.Sprintf(", %d AST Nodes)", report.ElasticASTCount)
	}

	apiStat := errStyle.Render("OFFLINE")
	if report.ApiOnline {
		apiStat = valueStyle.Render("ONLINE")
	}

	llmStat := errStyle.Render(fmt.Sprintf("OFFLINE (%s unreachable)", report.LlmTarget))
	if report.LlmOnline {
		llmStat = valueStyle.Render(fmt.Sprintf("ONLINE (%s) - Latency: %s", report.LlmTarget, report.LlmLatency))
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
	fmt.Println(renderRow("Local LLM Base URI:", llmStat))
	fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
	fmt.Println(renderRow("Active Projects:", valueStyle.Render(fmt.Sprintf("%d", report.Projects))))
	fmt.Println(renderRow("Work In Queue:", valueStyle.Render(fmt.Sprintf("%d tasks", report.QueuedWork))))
	fmt.Println(renderRow("Completed Work:", valueStyle.Render(fmt.Sprintf("%d tasks", report.CompletedWork))))
	fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
	fmt.Println(renderRow("Agents Ready (Standby):", valueStyle.Render(fmt.Sprintf("%d", report.AgentsReady))))
	fmt.Println(renderRow("Agents Executing (Busy):", valueStyle.Render(fmt.Sprintf("%d", report.AgentsBusy))))
	fmt.Println(renderRow("Determinism Loop:", valueStyle.Render(fmt.Sprintf("%s (%s)", report.NativeDeterminism.Status, report.NativeDeterminism.Description))))

	// Deep LLM probe results
	if report.DeepProbeRan {
		fmt.Println(borderStyle.Render("├─────────────────────────────────────────────────────────────────┤"))
		if report.DeepProbeOk {
			tpsStr := fmt.Sprintf("%.1f tok/s", report.DeepProbeTPS)
			if report.DeepProbeTPS < 5 {
				tpsStr = errStyle.Render(tpsStr + " (SLOW)")
			} else if report.DeepProbeTPS < 15 {
				tpsStr = warnStyle.Render(tpsStr + " (moderate)")
			} else {
				tpsStr = valueStyle.Render(tpsStr + " (fast)")
			}
			fmt.Println(renderRow("Deep LLM Probe:", valueStyle.Render("PASS")))
			fmt.Println(renderRow("Inference Latency:", valueStyle.Render(report.DeepProbeLatency)))
			fmt.Println(renderRow("Throughput:", tpsStr))
			fmt.Println(renderRow("Completion Tokens:", valueStyle.Render(fmt.Sprintf("%d", report.DeepProbeTokens))))
		} else {
			fmt.Println(renderRow("Deep LLM Probe:", errStyle.Render("FAIL")))
			fmt.Println(renderRow("Error:", errStyle.Render(report.DeepProbeError)))
		}
	}

	fmt.Println(borderStyle.Render("└─────────────────────────────────────────────────────────────────┘"))

	if len(report.Suggestions) > 0 {
		fmt.Println("\n" + ui.WarningGold("Diagnostic Healing Suggestions:"))
		for _, idx := range report.Suggestions {
			fmt.Println(errStyle.Render(" [!] ") + idx)
		}
	}

	fmt.Println("\n" + ui.NeonGreen("Diagnostic Telemetry Matrix Extracted Parallel."))
	fmt.Println()
}

var DoctorCmd = &cobra.Command{
	Use:   "doctor",
	Short: "Diagnose Flume internal components & swarm health natively",
	Long: `Diagnose Flume internal components & swarm health natively.

Use --deep to send a timed test prompt through the LLM and measure
actual inference speed. This catches slow models that would exceed
the planner timeout before users hit "Plan New Work".`,
	Run: func(cmd *cobra.Command, args []string) {
		// Externalized Configurations
		esURL, _ := cmd.Flags().GetString("es-url")
		vaultURL, _ := cmd.Flags().GetString("vault-url")
		dashboardURL, _ := cmd.Flags().GetString("dashboard-url")
		llmURL, _ := cmd.Flags().GetString("llm-url")
		jsonOutput, _ := cmd.Flags().GetBool("json")
		deepProbe, _ := cmd.Flags().GetBool("deep")
		gatewayURL, _ := cmd.Flags().GetString("gateway-url")

		client := &http.Client{Timeout: 3 * time.Second}
		report := &DiagnosticsReport{}
		var wg sync.WaitGroup

		if !jsonOutput {
			fmt.Println("\n" + ui.CyberGradient(":: FLUME ECOSYSTEM TELEMETRY DIAGNOSTICS ::") + "\n")
		}

		// Parallel Telemetry Execution Maps (Goroutines)
		probeCount := 6
		if deepProbe {
			probeCount = 7
		}
		wg.Add(probeCount)
		go fetchDocker(report, &wg)
		go fetchVault(client, vaultURL, report, &wg)
		go fetchElasticsearch(client, esURL, report, &wg)
		go fetchDashboard(client, dashboardURL, report, &wg)
		go fetchLlmGateway(client, llmURL, report, &wg)
		go fetchNativeDeterminism(client, "http://localhost:8766", report, &wg)

		if deepProbe {
			if !jsonOutput {
				fmt.Println(ui.WarningGold("Deep LLM inference probe active — sending test prompt..."))
			}
			go fetchDeepLLMProbe(gatewayURL, report, &wg)
		}

		wg.Wait() // Block execution safely until all endpoints respond or trace out

		// Dispatch Rendering
		renderDiagnosticReport(report, jsonOutput)
	},
}

func init() {
	DoctorCmd.Flags().StringP("es-url", "e", "https://localhost:9200", "Elasticsearch Diagnostic Endpoint")
	DoctorCmd.Flags().StringP("vault-url", "v", "http://localhost:8200", "OpenBao Telemetry Endpoint")
	DoctorCmd.Flags().StringP("dashboard-url", "d", "http://localhost:8765", "Flume API Dashboard Endpoint")
	DoctorCmd.Flags().StringP("llm-url", "l", "http://host.docker.internal:52415/v1", "Local LLM Inference Engine Endpoint")
	DoctorCmd.Flags().StringP("gateway-url", "g", "http://localhost:8090", "Flume Gateway Endpoint (used by --deep)")
	DoctorCmd.Flags().BoolP("json", "j", false, "Output explicit raw JSON payload without any rendering")
	DoctorCmd.Flags().Bool("deep", false, "Run a timed LLM inference probe to measure model speed")
}
