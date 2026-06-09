package commands

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

// knownProviders is the canonical list of supported LLM providers.
// Used for both display and input validation in set-provider.
var knownProviders = []struct {
	num, name, desc string
}{
	{"1", "openai", "OpenAI GPT-4o, GPT-4, GPT-3.5 — cloud API"},
	{"2", "anthropic", "Claude 3.5 Sonnet, Claude 3 Opus — cloud API"},
	{"3", "ollama", "Local Ollama models — self-hosted on this machine"},
	{"4", "exo", "Mac MLX distributed inference — Apple Silicon cluster"},
	{"5", "gemini", "Google Gemini Pro / Flash — cloud API"},
	{"6", "grok", "xAI Grok — cloud API"},
}

var configJSON bool

var ConfigCmd = &cobra.Command{
	Use:   "config",
	Short: "Manage Flume LLM, Elasticsearch, and system configuration",
	Long:  `View or update LLM provider, model, API credentials, and Elasticsearch settings.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		return showConfig()
	},
}

var configShowCmd = &cobra.Command{
	Use:   "show",
	Short: "Show the current Flume configuration (secrets masked)",
	RunE: func(cmd *cobra.Command, args []string) error {
		return showConfig()
	},
}

var configProvidersCmd = &cobra.Command{
	Use:   "providers",
	Short: "List all available LLM providers",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(ui.NeonGreen("  AVAILABLE LLM PROVIDERS  "))
		for _, p := range knownProviders {
			fmt.Printf("  %s. %-12s → %s\n", p.num, ui.SuccessBlue(p.name), p.desc)
		}
	},
}

var configSetProviderCmd = &cobra.Command{
	Use:   "set-provider",
	Short: "Interactively update the LLM provider and credentials",
	RunE: func(cmd *cobra.Command, args []string) error {
		reader := bufio.NewReader(os.Stdin)

		fmt.Print(ui.WarningGold("Enter provider number (1-6): "))
		provInput, err := reader.ReadString('\n')
		if err != nil {
			return fmt.Errorf("failed to read provider input: %w", err)
		}
		provInput = strings.TrimSpace(provInput)

		// Build lookup maps from the canonical knownProviders list.
		numToName := make(map[string]string, len(knownProviders))
		nameSet := make(map[string]bool, len(knownProviders))
		for _, p := range knownProviders {
			numToName[p.num] = p.name
			nameSet[p.name] = true
		}

		// Resolve: numeric → name, or validate as a known provider name.
		provider, ok := numToName[provInput]
		if !ok {
			// Accept provider name typed directly, but only if it is known.
			if !nameSet[provInput] {
				return fmt.Errorf(
					"unknown provider '%s': must be a number 1–6 or one of: %s",
					sanitizeForTerminal(provInput),
					strings.Join(func() []string {
						names := make([]string, len(knownProviders))
						for i, p := range knownProviders {
							names[i] = p.name
						}
						return names
					}(), ", "),
				)
			}
			provider = provInput
		}

		fmt.Print(ui.WarningGold("Enter model name (e.g. gpt-4o, leave blank for default): "))
		model, err := reader.ReadString('\n')
		if err != nil {
			return fmt.Errorf("failed to read model input: %w", err)
		}
		model = strings.TrimSpace(model)

		apiKey := ""
		if provider != "ollama" && provider != "exo" {
			fmt.Print(ui.WarningGold("Enter API key (input hidden — press Enter): "))
			apiKey, err = reader.ReadString('\n')
			if err != nil {
				return fmt.Errorf("failed to read API key: %w", err)
			}
			apiKey = strings.TrimSpace(apiKey)
		}

		client := ui.NewFlumeClient()
		payload := map[string]any{
			"provider": provider,
			"model":    model,
		}
		if apiKey != "" {
			payload["apiKey"] = apiKey
		}
		if _, err := client.Put("/api/settings/llm", payload); err != nil {
			return fmt.Errorf("failed to update LLM settings: %w", err)
		}
		fmt.Println(ui.SuccessBlue(fmt.Sprintf(
			"LLM provider updated to '%s' (model: '%s').",
			sanitizeForTerminal(provider),
			sanitizeForTerminal(model),
		)))
		return nil
	},
}

var configSetESURLCmd = &cobra.Command{
	Use:   "set-es-url <url>",
	Short: "Update the Elasticsearch endpoint URL",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		esURL := args[0]
		if !strings.HasPrefix(esURL, "http://") && !strings.HasPrefix(esURL, "https://") {
			return fmt.Errorf("URL must start with http:// or https://")
		}
		client := ui.NewFlumeClient()
		if _, err := client.Put("/api/settings/system", map[string]string{"es_url": esURL}); err != nil {
			return fmt.Errorf("failed to update ES URL: %w", err)
		}
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("Elasticsearch URL updated to: %s", sanitizeForTerminal(esURL))))
		return nil
	},
}

var restartUIFlag bool

var configRestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Restart Flume services (or just the UI/dashboard with --ui)",
	Long: `Restart services or perform a targeted frontend rebuild + dashboard restart.

Use --ui / -u for fast frontend development:
  flume config restart --ui

This will:
  1. Gracefully stop the dashboard (container or process).
  2. Run "npm run build" for the React/Vite frontend.
  3. (Docker) Copy fresh dist/ into the container.
  4. Start the dashboard component again.
The rest of the Flume stack (gateway, workers, ES, OpenBao) is left running.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if restartUIFlag {
			return runFrontendUIRestart()
		}
		client := ui.NewFlumeClient()
		if _, err := client.Post("/api/settings/restart-services", nil); err != nil {
			return fmt.Errorf("failed to restart services: %w", err)
		}
		fmt.Println(ui.SuccessBlue("Services restarting..."))
		return nil
	},
}

func showConfig() error {
	client := ui.NewFlumeClient()

	llm, err := client.Get("/api/settings/llm")
	if err != nil {
		return fmt.Errorf("could not fetch LLM settings: %w", err)
	}

	// Non-fatal: warn and continue with partial data if these endpoints fail.
	repos, err := client.Get("/api/settings/repos")
	if err != nil {
		log.Warn("Could not fetch repo settings", "error", err)
	}
	sys, err := client.Get("/api/settings/system")
	if err != nil {
		log.Warn("Could not fetch system settings", "error", err)
	}

	if configJSON {
		out := map[string]any{"llm": llm, "repos": repos, "system": sys}
		b, err := json.MarshalIndent(out, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to serialize config as JSON: %w", err)
		}
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen("  FLUME CONFIGURATION  "))
	fmt.Println()

	// LLM section.
	fmt.Println(ui.WarningGold(" LLM PROVIDER"))
	if llm != nil {
		printConfigField("Provider", stringValFromKeys(llm, "—", "provider"))
		printConfigField("Model", stringValFromKeys(llm, "—", "model"))
		printConfigField("Base URL", stringValFromKeys(llm, "—", "baseUrl", "base_url"))
		apiKey := stringValFromKeys(llm, "", "apiKey", "api_key")
		printConfigField("API Key", maskSecret(apiKey))
	}
	fmt.Println()

	// Repo section.
	fmt.Println(ui.WarningGold(" REPOSITORY"))
	if repos != nil {
		printConfigField("Type", stringValFromKeys(repos, "—", "type", "repoType"))
		printConfigField("Org/User", stringValFromKeys(repos, "—", "org", "adoOrg"))
		tok := stringValFromKeys(repos, "", "token", "githubToken", "adoToken")
		printConfigField("Token", maskSecret(tok))
	} else {
		fmt.Println(ui.WarningGold("  Repo settings unavailable."))
	}
	fmt.Println()

	// System section.
	fmt.Println(ui.WarningGold(" SYSTEM"))
	esURL := "—"
	if sys != nil {
		esURL = stringValFromKeys(sys, "—", "es_url", "esUrl")
	} else {
		fmt.Println(ui.WarningGold("  System settings unavailable."))
	}
	printConfigField("ES URL", esURL)
	printConfigField("Dashboard", fmt.Sprintf("%s (%s)", client.BaseURL, ui.StatusBadge("healthy")))
	fmt.Println()
	return nil
}

func printConfigField(label, value string) {
	fmt.Printf("  %-12s: %s\n", label, value)
}

// ─── UI / Frontend Restart Implementation ───────────────────────────────────

func runFrontendUIRestart() error {
	fmt.Println(ui.CyberGradient("⚡ Flume frontend rebuild + dashboard restart"))

	pkgDir, err := findFrontendPackageDir()
	if err != nil {
		return fmt.Errorf("could not locate frontend source (src/frontend/src/package.json): %w\nRun this command from inside the flume source checkout, or set FLUME_ROOT.", err)
	}
	distDir := filepath.Join(filepath.Dir(pkgDir), "dist")
	distAbs, _ := filepath.Abs(distDir)

	fmt.Printf("  Frontend source: %s\n", pkgDir)
	fmt.Printf("  Target dist:     %s\n", distAbs)

	// 1. Build the frontend (Vite produces hashed bundles + updated index.html)
	fmt.Println(ui.WarningGold("Building frontend (npm run build)..."))
	buildCmd := exec.Command("npm", "run", "build")
	buildCmd.Dir = pkgDir
	buildCmd.Stdout = os.Stdout
	buildCmd.Stderr = os.Stderr
	if err := buildCmd.Run(); err != nil {
		return fmt.Errorf("frontend build failed: %w", err)
	}
	fmt.Println(ui.SuccessBlue("✔ Frontend build complete."))

	// 2. Decide environment and perform stop + (re)start of dashboard only.
	client := ui.NewFlumeClient()
	dockerMode := isDockerDashboardActive()

	if dockerMode {
		fmt.Println(ui.CyberGradient("Docker mode detected — stopping dashboard container..."))
		// Best-effort stop (container may already be stopped).
		stopCmd := exec.Command("docker", "compose", "stop", "dashboard")
		stopCmd.Stdout = os.Stdout
		stopCmd.Stderr = os.Stderr
		_ = stopCmd.Run()

		fmt.Println(ui.CyberGradient("Copying fresh dist/ into container..."))
		cpCmd := exec.Command("docker", "cp", distAbs+"/.", "flume-dashboard:/app/frontend/dist/")
		cpCmd.Stdout = os.Stdout
		cpCmd.Stderr = os.Stderr
		if err := cpCmd.Run(); err != nil {
			fmt.Println(ui.WarningGold("docker cp warning (continuing): " + err.Error()))
		}

		fmt.Println(ui.CyberGradient("Starting dashboard container..."))
		startCmd := exec.Command("docker", "compose", "start", "dashboard")
		startCmd.Stdout = os.Stdout
		startCmd.Stderr = os.Stderr
		if err := startCmd.Run(); err != nil {
			return fmt.Errorf("failed to start dashboard container: %w", err)
		}
	} else {
		fmt.Println(ui.CyberGradient("Local/native mode — signaling running dashboard to stop..."))

		// Ask the running dashboard (if any) to gracefully exit via its HTTP server.
		// This uses the new /api/settings/restart-dashboard handler which calls Shutdown.
		_, _ = client.Post("/api/settings/restart-dashboard", nil)

		// Give it a moment to drain.
		time.Sleep(400 * time.Millisecond)

		fmt.Println(ui.CyberGradient("Starting dashboard with fresh UI assets..."))

		// Launch `flume dashboard` as a background child with the correct StaticRoot
		// so the SPA is served at the normal dashboard port.
		dashCmd := exec.Command("flume", "dashboard")
		dashCmd.Env = append(os.Environ(), "FLUME_STATIC_ROOT="+distAbs)
		dashCmd.Stdout = os.Stdout
		dashCmd.Stderr = os.Stderr

		if err := dashCmd.Start(); err != nil {
			return fmt.Errorf("failed to start 'flume dashboard': %w (is 'flume' in your PATH?)", err)
		}
		if dashCmd.Process != nil {
			fmt.Printf("  Dashboard launched (pid %d)\n", dashCmd.Process.Pid)
		}
	}

	// 3. Wait for the dashboard to report healthy.
	if err := waitForDashboardHealthy(25 * time.Second); err != nil {
		return fmt.Errorf("dashboard did not become healthy in time: %w", err)
	}

	fmt.Println(ui.SuccessBlue("✅ Dashboard restarted with updated frontend."))
	fmt.Println(ui.WarningGold("   If the browser shows stale JS/CSS, do a hard refresh (⌘/Ctrl + Shift + R)."))
	return nil
}

// findFrontendPackageDir walks upward from the current working directory (and a
// few other candidate locations) until it finds the Vite package.json that
// contains the frontend build scripts.
func findFrontendPackageDir() (string, error) {
	candidates := []string{}

	if root := strings.TrimSpace(os.Getenv("FLUME_ROOT")); root != "" {
		candidates = append(candidates, root)
	}

	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates, cwd)
		// Walk up a few levels
		dir := cwd
		for i := 0; i < 6; i++ {
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			candidates = append(candidates, parent)
			dir = parent
		}
	}

	if exe, err := os.Executable(); err == nil {
		exeDir := filepath.Dir(exe)
		candidates = append(candidates, exeDir, filepath.Dir(exeDir))
	}

	seen := map[string]bool{}
	for _, c := range candidates {
		abs, err := filepath.Abs(c)
		if err != nil {
			continue
		}
		if seen[abs] {
			continue
		}
		seen[abs] = true

		// package.json lives at src/frontend/src/package.json
		pkg := filepath.Join(abs, "src", "frontend", "src", "package.json")
		if _, err := os.Stat(pkg); err == nil {
			return filepath.Dir(pkg), nil // return the dir containing package.json
		}
	}
	return "", fmt.Errorf("frontend package.json not found in search path")
}

// isDockerDashboardActive returns true when the compose-managed dashboard
// appears to be reachable (via the internal DNS name the client also uses).
func isDockerDashboardActive() bool {
	probe := &http.Client{Timeout: 180 * time.Millisecond}
	// Same probe the FlumeClient uses.
	if resp, err := probe.Get("http://flume-dashboard:8765/api/health"); err == nil {
		resp.Body.Close()
		return resp.StatusCode == http.StatusOK
	}
	// Fallback: ask docker compose for the container
	cmd := exec.Command("docker", "compose", "ps", "-q", "dashboard")
	out, err := cmd.Output()
	if err == nil && len(strings.TrimSpace(string(out))) > 0 {
		return true
	}
	return false
}

// waitForDashboardHealthy polls common dashboard locations until /api/health
// returns 200 or the timeout is exceeded.
func waitForDashboardHealthy(timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	urls := []string{
		"http://localhost:8765/api/health",
		"http://127.0.0.1:8765/api/health",
		"http://flume-dashboard:8765/api/health",
	}

	for time.Now().Before(deadline) {
		for _, u := range urls {
			client := &http.Client{Timeout: 400 * time.Millisecond}
			if resp, err := client.Get(u); err == nil {
				resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					return nil
				}
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	return fmt.Errorf("timed out after %s", timeout)
}

func init() {
	ConfigCmd.Flags().BoolVarP(&configJSON, "json", "j", false, "Output raw JSON")
	ConfigCmd.AddCommand(configShowCmd)
	ConfigCmd.AddCommand(configProvidersCmd)
	ConfigCmd.AddCommand(configSetProviderCmd)
	ConfigCmd.AddCommand(configSetESURLCmd)
	ConfigCmd.AddCommand(configRestartCmd)
	configRestartCmd.Flags().BoolVarP(&restartUIFlag, "ui", "u", false, "Rebuild frontend and restart only the dashboard (leaves workers/gateway running)")
}
