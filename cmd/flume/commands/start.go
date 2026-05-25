package commands

import (
	"bytes"
	"crypto/tls"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/services"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/spf13/cobra"
)

var (
	ProviderFlag       string
	NativeFlag         bool
	WorkersFlag        string
	ConfigFlag         string
	PlannerTimeoutFlag int
)

func isHeadlessEnv(getenv func(string) string, getstat func() (os.FileInfo, error)) bool {
	stat, err := getstat()
	if err == nil && (stat.Mode()&os.ModeCharDevice) == 0 {
		return true
	}
	return getenv("CI") != "" || getenv("NON_INTERACTIVE") == "1" || getenv("FLUME_HEADLESS") == "1"
}

var StartCmd = &cobra.Command{
	Use:   "start",
	Short: "Initiate Flume V3 Edge Orchestrator",
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx := cmd.Context()
		fmt.Println(ui.BootPhase("Booting Flume orchestrator..."))

		fmt.Println(ui.BootPhase("Scanning system dependencies..."))
		eco := orchestrator.PerformReconnaissance()
		fmt.Print(ui.ReconReport(map[string]bool{
			"Docker":        eco.HasDocker,
			"Elasticsearch": eco.HasElastic,
			"OpenBao":       eco.HasOpenBao,
			"Elastro":       eco.HasElastro,
		}))

		if err := orchestrator.EvaluateAndInstall(eco); err != nil {
			fmt.Println(ui.ErrorRed(fmt.Sprintf("Dependency check failed: %v", err)))
			return err
		}

		dashboardPort := "8765"
		vaultPort := "8200"
		esPort := "9200"

		ports := orchestrator.CheckPortBinds([]int{8765, 8200, 9200})
		for port, inUse := range ports {
			if inUse {
				newPort := ui.PromptForPort(port)
				if newPort == 0 {
					return fmt.Errorf("port conflict on %d — aborting", port)
				}
				if port == 8765 {
					dashboardPort = fmt.Sprintf("%d", newPort)
				} else if port == 8200 {
					vaultPort = fmt.Sprintf("%d", newPort)
				} else if port == 9200 {
					esPort = fmt.Sprintf("%d", newPort)
				}
			}
		}

		portEnvOverrides := append(os.Environ(),
			"DASHBOARD_PORT="+dashboardPort,
			"VAULT_PORT="+vaultPort,
			"ES_PORT="+esPort,
		)

		if PlannerTimeoutFlag > 0 {
			portEnvOverrides = append(portEnvOverrides,
				fmt.Sprintf("FLUME_PLANNER_TIMEOUT_SECONDS=%d", PlannerTimeoutFlag))
		}

		envCfg := orchestrator.EnvConfig{
			Provider:           ProviderFlag,
			APIKey:             os.Getenv("FLUME_API_KEY"),
			BaseURL:            strings.TrimSpace(os.Getenv("LLM_BASE_URL")),
			LocalOllamaBaseURL: strings.TrimSpace(os.Getenv("LOCAL_OLLAMA_BASE_URL")),
			Host:               strings.TrimSpace(os.Getenv("LLM_HOST")),
			Model:              strings.TrimSpace(os.Getenv("LLM_MODEL")),
			IsNative:           NativeFlag,
		}

		adminToken, tErr := orchestrator.GenerateAdminToken()
		if tErr != nil {
			fmt.Println(ui.ErrorRed(fmt.Sprintf("Failed to generate admin token: %v", tErr)))
			return tErr
		}
		envCfg.AdminToken = adminToken

		esPass, esErr := orchestrator.GenerateElasticPassword()
		if esErr != nil {
			fmt.Println(ui.ErrorRed(fmt.Sprintf("Failed to generate Elasticsearch password: %v", esErr)))
			return esErr
		}
		envCfg.ElasticPassword = esPass
		os.Setenv("FLUME_ELASTIC_PASSWORD", esPass)

		if ConfigFlag != "" {
			var err error
			fmt.Println(ui.BootPhase(fmt.Sprintf("Loading mesh config: %s", ConfigFlag)))
			envCfg, err = parseMeshConfig(ConfigFlag)
			if err != nil {
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Failed to parse mesh config: %v", err)))
				return err
			}
			envCfg.IsNative = NativeFlag
		} else if envCfg.Provider == "" {
			if isHeadlessEnv(os.Getenv, os.Stdin.Stat) {
				return fmt.Errorf("non-interactive terminal detected without a provider — pass -p [provider] or --config")
			} else {
				promptCfg, err := ui.RunInteractivePrompt(orchestrator.CheckExoActive())
				if err != nil {
					fmt.Println(ui.ErrorRed("Setup wizard cancelled."))
					return err
				}
				envCfg.Provider = promptCfg.Provider
				envCfg.APIKey = promptCfg.APIKey
				envCfg.Model = promptCfg.Model
				envCfg.ExternalElastic = promptCfg.ExternalElastic
				envCfg.ESUrl = promptCfg.ElasticURL
				envCfg.RepoType = promptCfg.RepoType
				envCfg.GithubToken = promptCfg.GithubToken
				envCfg.ADOOrg = promptCfg.ADOOrg
				envCfg.ADOToken = promptCfg.ADOToken

				if promptCfg.Provider == "ollama" {
					if promptCfg.Host == "" {
						promptCfg.Host = "127.0.0.1"
					}
					envCfg.Host = promptCfg.Host
					envCfg.BaseURL = fmt.Sprintf("http://%s:11434", promptCfg.Host)
					envCfg.LocalOllamaBaseURL = fmt.Sprintf("http://%s:11434/v1", promptCfg.Host)
					envCfg.APIKey = ""
				}

				// Map wizard-collected nodes to EnvConfig.
				for _, cp := range promptCfg.CloudProviders {
					envCfg.CloudProviders = append(envCfg.CloudProviders, orchestrator.CloudProviderEntry{
						Provider: cp.Provider,
						Model:    cp.Model,
						APIKey:   cp.APIKey,
					})
				}
				
				for _, n := range promptCfg.Nodes {
					mem := 0.0
					if n.MemoryGB != "" {
						if v, err := strconv.ParseFloat(n.MemoryGB, 64); err == nil {
							mem = v
						}
					}
					port := n.Port
					if port == "" {
						port = "11434"
					}
					envCfg.Nodes = append(envCfg.Nodes, orchestrator.NodeConfigEntry{
						ID:       n.ID,
						Host:     n.Host,
						Port:     port,
						ModelTag: n.ModelTag,
						MemoryGB: mem,
					})
				}
			}
		}

		generatedEnv := orchestrator.GenerateEnv(envCfg)

		if NativeFlag {
			fmt.Println(ui.BootPhase("Starting native high-performance subsystems..."))

			dockerArgs := []string{"compose"}
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "--profile", "managed_elastic")
			}
			dockerArgs = append(dockerArgs, "up", "-d", "--wait")
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "elasticsearch")
			}
			dockerArgs = append(dockerArgs, "openbao")

			c := exec.CommandContext(ctx, "docker", dockerArgs...)
			c.Env = append(os.Environ(), generatedEnv...)

			var outBuf, errBuf bytes.Buffer
			c.Stdout = io.MultiWriter(os.Stdout, &outBuf)
			c.Stderr = io.MultiWriter(os.Stderr, &errBuf)

			err := c.Run()
			if err != nil {
				combinedOutput := outBuf.String() + "\n" + errBuf.String()
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Data grid boot failed: %v", err)))
				_ = combinedOutput
				return err
			}

			esScheme := "https"
			probeURL := fmt.Sprintf("https://localhost:%s/_cluster/health", esPort)
			if probeReq, err := http.NewRequestWithContext(ctx, "GET", probeURL, nil); err == nil {
				probeClient := &http.Client{
					Timeout: 2 * time.Second,
					Transport: &http.Transport{
						TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
					},
				}
				if probeResp, probeErr := probeClient.Do(probeReq); probeErr != nil {
					if strings.Contains(probeErr.Error(), "wrong version number") || strings.Contains(probeErr.Error(), "http:") {
						esScheme = "http"
					}
				} else {
					probeResp.Body.Close()
				}
			}
			esUrl := esScheme + "://localhost:" + esPort
			if envCfg.ExternalElastic && envCfg.ESUrl != "" {
				esUrl = envCfg.ESUrl
			}

			secID, rootToken, vErr := orchestrator.DeployVaultTopology(ctx, vaultPort, esUrl, envCfg)
			if vErr != nil {
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Vault deployment failed: %v", vErr)))
				return vErr
			}
			generatedEnv = append(generatedEnv, "BAO_SECRET_ID="+secID)
			generatedEnv = append(generatedEnv, "OPENBAO_TOKEN="+rootToken)

			// Bootstrap ALL ES indices before any application containers start.
			// Must run after OpenBao is deployed (some indices store credential metadata).
			if err := orchestrator.BootstrapElasticsearch(ctx, esUrl, ""); err != nil {
				fmt.Println(ui.WarningGold(fmt.Sprintf("ES index bootstrap: %v (non-fatal)", err)))
			}

			// Seed non-sensitive LLM config into ES so Settings page reads correctly on first load.
			if err := orchestrator.SeedLLMConfig(ctx, esUrl, "", envCfg); err != nil {
				fmt.Println(ui.WarningGold(fmt.Sprintf("LLM config seed: %v (non-fatal)", err)))
			}

			// Phase 5: Start all application services in-process.
			// Replaces Python subprocess (uv run src/dashboard/server.py)
			// and Go worker goroutines with unified Supervisor.
			services.SetEnvForServices(generatedEnv)
			services.SetEnvForServices(portEnvOverrides)
			os.Setenv("FLUME_NATIVE_MODE", "1")
			os.Setenv("ES_URL", esUrl)
			os.Setenv("OPENBAO_ADDR", "http://localhost:"+vaultPort)

			logger := slog.Default()
			cfg := config.Load(ctx, logger)
			sup := services.NewSupervisor(cfg, logger)

			// Start services — blocks until ctx cancelled or fatal error
			go func() {
				if err := sup.StartAll(ctx); err != nil {
					fmt.Println(ui.ErrorRed(fmt.Sprintf("Service mesh error: %v", err)))
				}
			}()
		} else {
			fmt.Println(ui.BootPhase("Deploying Docker topology..."))

			dockerArgs := []string{"compose"}
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "--profile", "managed_elastic")
			}
			dockerArgs = append(dockerArgs, "up", "-d", "--build", "--wait")
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "elasticsearch")
			}
			dockerArgs = append(dockerArgs, "openbao")

			c := exec.CommandContext(ctx, "docker", dockerArgs...)
			fullEnv := append(os.Environ(), portEnvOverrides...)
			fullEnv = append(fullEnv, generatedEnv...)
			c.Env = fullEnv

			var outBuf, errBuf bytes.Buffer
			c.Stdout = io.MultiWriter(os.Stdout, &outBuf)
			c.Stderr = io.MultiWriter(os.Stderr, &errBuf)

			err := c.Run()
			if err != nil {
				combinedOutput := outBuf.String() + "\n" + errBuf.String()
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Data grid boot failed: %v", err)))
				_ = combinedOutput
				return err
			}

			esScheme := "https"
			probeURL := fmt.Sprintf("https://localhost:%s/_cluster/health", esPort)
			if probeReq, err := http.NewRequestWithContext(ctx, "GET", probeURL, nil); err == nil {
				probeClient := &http.Client{
					Timeout: 2 * time.Second,
					Transport: &http.Transport{
						TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
					},
				}
				if probeResp, probeErr := probeClient.Do(probeReq); probeErr != nil {
					if strings.Contains(probeErr.Error(), "wrong version number") || strings.Contains(probeErr.Error(), "http:") {
						esScheme = "http"
					}
				} else {
					probeResp.Body.Close()
				}
			}
			esUrl := esScheme + "://localhost:" + esPort
			if envCfg.ExternalElastic && envCfg.ESUrl != "" {
				esUrl = envCfg.ESUrl
			}

			esUrlDocker := esScheme + "://elasticsearch:9200"
			if envCfg.ExternalElastic && envCfg.ESUrl != "" {
				esUrlDocker = envCfg.ESUrl
			}
			fullEnv = append(fullEnv, "ES_URL="+esUrlDocker)

			secID, rootToken, vErr := orchestrator.DeployVaultTopology(ctx, vaultPort, esUrl, envCfg)
			if vErr != nil {
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Vault deployment failed: %v", vErr)))
				return vErr
			}
			fullEnv = append(fullEnv, "BAO_SECRET_ID="+secID)
			// Inject the root token so containers can authenticate to OpenBao.
			// docker-compose.yml uses ${OPENBAO_TOKEN} — this must be in the
			// subprocess env for the variable substitution to resolve correctly.
			fullEnv = append(fullEnv, "OPENBAO_TOKEN="+rootToken)

			// Bootstrap ALL ES indices before any application containers start.
			// Must run after OpenBao is deployed (some indices store credential metadata).
			// Use localhost since we're still on the host machine at this point in boot.
			if err := orchestrator.BootstrapElasticsearch(ctx, esUrl, ""); err != nil {
				fmt.Println(ui.WarningGold(fmt.Sprintf("ES index bootstrap: %v (non-fatal)", err)))
			}

			// Seed non-sensitive LLM config into ES so Settings page reads correctly on first load.
			if err := orchestrator.SeedLLMConfig(ctx, esUrl, "", envCfg); err != nil {
				fmt.Println(ui.WarningGold(fmt.Sprintf("LLM config seed: %v (non-fatal)", err)))
			}

			// Start application containers (dashboard, gateway, workers).
			// Workers scale via FLUME_WORKER_COUNT env var → compose deploy.replicas.
			workerCount := orchestrator.ResolveWorkerCount(WorkersFlag)
			fullEnv = append(fullEnv,
				fmt.Sprintf("FLUME_WORKER_COUNT=%d", workerCount),
			)

			appServices := orchestrator.BuildWorkerServiceNames(workerCount)
			appArgs := []string{"compose"}
			if !envCfg.ExternalElastic {
				appArgs = append(appArgs, "--profile", "managed_elastic")
			}
			appArgs = append(appArgs, "up", "-d", "--build", "--wait")
			appArgs = append(appArgs, appServices...)

			appCmd := exec.CommandContext(ctx, "docker", appArgs...)
			appCmd.Env = fullEnv
			appCmd.Stdout = io.MultiWriter(os.Stdout, &outBuf)
			appCmd.Stderr = io.MultiWriter(os.Stderr, &errBuf)

			if err := appCmd.Run(); err != nil {
				combinedOutput := outBuf.String() + "\n" + errBuf.String()
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Application container boot failed: %v", err)))
				_ = combinedOutput // logged at debug level
				return err
			}
			fmt.Println(ui.SuccessBlue("Application containers started."))
		}

		if err := orchestrator.AwaitOrchestration(); err != nil {
			return err
		}

		// ── Seed node mesh from primary Ollama host + wizard entries ────────
		gatewayPort := "8090" // default gateway port
		gatewayURL := fmt.Sprintf("http://localhost:%s", gatewayPort)

		var seedEntries []orchestrator.NodeSeedEntry

		// Only seed the primary host if they actually configured one, which is indicated by Host != "" or model != ""
		// Wait, if it's external, Host might be "" but we don't want to seed an empty host. 
		if (envCfg.Provider == "ollama" || envCfg.Provider == "exo") || (envCfg.Host != "") {
			// Always register the primary Ollama host so it appears on the Node Mesh page.
			primaryHost := envCfg.Host
			if primaryHost == "" {
				primaryHost = "127.0.0.1"
			}
			// In Docker mode, the HealthChecker runs inside the gateway container
			// where 127.0.0.1 is the container's own loopback — not the host.
			// Rewrite local addresses to host.docker.internal so probes reach
			// the host machine's Ollama instance.
			if !envCfg.IsNative && (primaryHost == "127.0.0.1" || primaryHost == "localhost") {
				primaryHost = "host.docker.internal"
			}
			
			// Extract port if provided, otherwise default to 11434
			hostPort := "11434"
			if strings.Contains(primaryHost, ":") {
				parts := strings.Split(primaryHost, ":")
				primaryHost = parts[0]
				hostPort = parts[1]
			}
			
			primaryEntry := orchestrator.NodeSeedEntry{
				ID:       "primary",
				Host:     fmt.Sprintf("%s:%s", primaryHost, hostPort),
				ModelTag: envCfg.Model,
			}
			// ReasoningScore and MaxContext are left at zero — the health checker
			// dynamically derives them from POST /api/show within 15 seconds.
			seedEntries = append(seedEntries, primaryEntry)
		}

		// Append any additional nodes collected during the interactive wizard.
		for _, n := range envCfg.Nodes {
			entry := orchestrator.NodeSeedEntry{
				ID:       n.ID,
				Host:     fmt.Sprintf("%s:%s", n.Host, n.Port),
				ModelTag: n.ModelTag,
			}
			if n.MemoryGB > 0 {
				entry.Capabilities.MemoryGB = n.MemoryGB
			}
			seedEntries = append(seedEntries, entry)
		}

		if len(seedEntries) > 0 {
			if err := orchestrator.SeedNodes(ctx, gatewayURL, seedEntries); err != nil {
				fmt.Println(ui.WarningGold(fmt.Sprintf("Node mesh seeding: %v (non-fatal)", err)))
			} else {
				fmt.Println(ui.SuccessBlue(fmt.Sprintf("Node mesh seeded: %d node(s)", len(seedEntries))))
			}
		}

		// ── Deployment summary ────────────────────────────────────────────────
		workerCount := 3 // default Docker / native worker spawn count
		if w := strings.TrimSpace(WorkersFlag); w != "" && w != "auto" {
			if n, err := strconv.Atoi(w); err == nil && n > 0 {
				workerCount = n
			}
		}
		ui.PrintDeploymentSummary(ui.DeploymentSummary{
			NativeMode:      NativeFlag,
			AdminToken:      envCfg.AdminToken,
			ElasticPassword: envCfg.ElasticPassword,
			Provider:        envCfg.Provider,
			Model:           envCfg.Model,
			OllamaHost:      envCfg.Host,
			DashboardPort:   dashboardPort,
			ElasticPort:     esPort,
			VaultPort:       vaultPort,
			ExternalElastic: envCfg.ExternalElastic,
			ElasticURL:      envCfg.ESUrl,
			RepoType:        envCfg.RepoType,
			ADOOrg:          envCfg.ADOOrg,
			WorkerCount:     workerCount,
			HasAPIKey:       strings.TrimSpace(envCfg.APIKey) != "",
			HasGithubToken:  envCfg.GithubToken != "",
			HasADOToken:     envCfg.ADOToken != "",
			VaultDeployed:   true,
		})

		return nil
	},
}

func init() {
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", "Explicitly declare LLM Provider (openai, ollama, exo)")
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, "Launch Flume utilizing OS-native High-Performance Git Worktrees")
	StartCmd.Flags().StringVar(&WorkersFlag, "workers", "", `Number of workers: "2" (default), "4", "auto" (detect from hardware)`)
	StartCmd.Flags().StringVarP(&ConfigFlag, "config", "c", "", "Path to flume-mesh.yml IaC document for programmatic booting")
	StartCmd.Flags().IntVar(&PlannerTimeoutFlag, "planner-timeout", 0, "Override the LLM planner timeout in seconds (default: 300). Increase for slow local models.")
}
