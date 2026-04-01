package commands

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/agents"
	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

var (
	ProviderFlag string
	NativeFlag   bool
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
		log.Info("Booting Flume Matrix...")

		log.Infof("💾 Jacking into the local mainframe... Scanning global systemic hardware matrices 🔌")
		eco := orchestrator.PerformReconnaissance()
		log.Info("[SYSTEM RECON]", "Docker", eco.HasDocker, "Elastic", eco.HasElastic, "OpenBao", eco.HasOpenBao, "Elastro", eco.HasElastro)

		if err := orchestrator.EvaluateAndInstall(eco); err != nil {
			log.Error("Unmet Dependency Bounds", "error", err)
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
					log.Error("Port exhaustion detected gracefully natively.")
					return fmt.Errorf("port exhaustion on %d", port)
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
			log.Error("Failed to seed explicit cryptographic bounds natively", "error", tErr)
			return tErr
		}
		envCfg.AdminToken = adminToken

		if isHeadlessEnv(os.Getenv, os.Stdin.Stat) && envCfg.Provider == "" {
			log.Error("Non-interactive terminal detected without an explicit Provider. Please pass -p [provider] natively to prevent pipeline hanging.")
			return fmt.Errorf("headless tty pseudo-hang prevented")
		}

		if !isHeadlessEnv(os.Getenv, os.Stdin.Stat) {
			log.Warn("Escalating to User Auth Layer.")
			promptCfg, err := ui.RunInteractivePrompt(orchestrator.CheckExoActive())
			if err != nil {
				log.Error("Interactive Wizard aborted.", "error", err)
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
			envCfg.ADOProject = promptCfg.ADOProject
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
		}

		generatedEnv := orchestrator.GenerateEnv(envCfg)

		if NativeFlag {
			log.Info("Executing Flume High Performance Native Subsystems.")

			dockerArgs := []string{"compose"}
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "--profile", "managed_elastic")
			}
			dockerArgs = append(dockerArgs, "up", "-d", "--wait")
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "elasticsearch")
			}
			dockerArgs = append(dockerArgs, "openbao")

			c := exec.Command("docker", dockerArgs...)
			c.Env = append(os.Environ(), generatedEnv...)

			var outBuf, errBuf bytes.Buffer
			c.Stdout = io.MultiWriter(os.Stdout, &outBuf)
			c.Stderr = io.MultiWriter(os.Stderr, &errBuf)

			err := c.Run()
			if err != nil {
				combinedOutput := outBuf.String() + "\n" + errBuf.String()
				log.Error("Data grid boot failed", "error", err, "output", strings.TrimSpace(combinedOutput))
				return err
			}

			secID, vErr := orchestrator.DeployVaultTopology(vaultPort, envCfg)
			if vErr != nil {
				log.Error("Failed to deploy Vault architecture natively", "err", vErr)
				return vErr
			}
			generatedEnv = append(generatedEnv, "BAO_SECRET_ID="+secID)

			go func() {
				log.Info("Spawning FastAPI Dashboard daemon natively...")
				dash := exec.Command("uv", "run", "src/dashboard/server.py")

				dashEnv := append(os.Environ(), portEnvOverrides...)
				dashEnv = append(dashEnv, "PYTHONPATH=src", "FLUME_NATIVE_MODE=1", "ES_URL=http://localhost:"+esPort, "OPENBAO_ADDR=http://localhost:"+vaultPort)
				dashEnv = append(dashEnv, generatedEnv...)
				dash.Env = dashEnv

				dash.Stdout = os.Stdout
				dash.Stderr = os.Stderr

				if err := dash.Start(); err != nil {
					log.Error("Failed to spawn Flume Dashboard natively", "err", err)
					return
				}

				homeDir, _ := os.UserHomeDir()
				pidFile := filepath.Join(homeDir, ".flume", "flume-daemon.pid")
				os.MkdirAll(filepath.Dir(pidFile), 0755)
				os.WriteFile(pidFile, []byte(strconv.Itoa(dash.Process.Pid)), 0644)

				dash.Wait()
			}()

			var wg sync.WaitGroup
			for i := 1; i <= 3; i++ {
				wg.Add(1)
				go func(id int) {
					defer wg.Done()
					agents.DeployWorker(cmd.Context(), id, "init")
				}(i)
			}
			wg.Wait()
		} else {
			log.Warn("🚀 Initiating hyper-threaded uplink... Deploying Docker Swarm Topology 💿")

			dockerArgs := []string{"compose"}
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "--profile", "managed_elastic")
			}
			dockerArgs = append(dockerArgs, "up", "-d", "--build", "--wait")
			if !envCfg.ExternalElastic {
				dockerArgs = append(dockerArgs, "elasticsearch")
			}
			dockerArgs = append(dockerArgs, "openbao")

			c := exec.Command("docker", dockerArgs...)
			fullEnv := append(os.Environ(), portEnvOverrides...)
			fullEnv = append(fullEnv, generatedEnv...)
			c.Env = fullEnv

			var outBuf, errBuf bytes.Buffer
			c.Stdout = io.MultiWriter(os.Stdout, &outBuf)
			c.Stderr = io.MultiWriter(os.Stderr, &errBuf)

			err := c.Run()
			if err != nil {
				combinedOutput := outBuf.String() + "\n" + errBuf.String()
				log.Error("Data grid boot failed", "error", err, "output", strings.TrimSpace(combinedOutput))
				return err
			}

			secID, vErr := orchestrator.DeployVaultTopology(vaultPort, envCfg)
			if vErr != nil {
				log.Error("Failed to deploy Vault architecture natively", "err", vErr)
				return vErr
			}
			fullEnv = append(fullEnv, "BAO_SECRET_ID="+secID)

			swArgs := []string{"compose"}
			if !envCfg.ExternalElastic {
				swArgs = append(swArgs, "--profile", "managed_elastic")
			}
			swArgs = append(swArgs, "up", "-d", "--build", "--wait", "dashboard", "worker")

			swC := exec.Command("docker", swArgs...)
			swC.Env = fullEnv
			swC.Stdout = os.Stdout
			swC.Stderr = os.Stderr
			if err := swC.Run(); err != nil {
				log.Error("Container topology boot failed", "error", err)
				return err
			}
			log.Info("Container Swarm bootstrapped successfully in detached mode.")
		}

		if err := orchestrator.AwaitOrchestration(); err != nil {
			return err
		}

		if eco.HasElastro {
			log.Info("Synchronizing Local AST Mapping for RAG Agents natively...")

			type ASTConfig struct {
				DashboardAPIURL string
				RetriesLimit    int
				InitialBackoff  int
			}

			cfg := ASTConfig{
				DashboardAPIURL: os.Getenv("FLUME_DASHBOARD_API_URL"),
				RetriesLimit:    5,
				InitialBackoff:  500,
			}
			if cfg.DashboardAPIURL == "" {
				cfg.DashboardAPIURL = fmt.Sprintf("http://localhost:%s", dashboardPort)
			}
			if r, err := strconv.Atoi(os.Getenv("FLUME_AST_SYNC_RETRIES")); err == nil && r > 0 {
				cfg.RetriesLimit = r
			}
			if b, err := strconv.Atoi(os.Getenv("FLUME_AST_SYNC_INITIAL_BACKOFF_MS")); err == nil && b > 0 {
				cfg.InitialBackoff = b
			}

			endpoint := fmt.Sprintf("%s/api/system/sync-ast", cfg.DashboardAPIURL)

			var res *http.Response
			var reqErr error

			astSyncClient := &http.Client{
				Timeout: 15 * time.Second,
			}

			backoff := time.Duration(cfg.InitialBackoff) * time.Millisecond
			for retries := 1; retries <= cfg.RetriesLimit; retries++ {
				req, err := http.NewRequestWithContext(cmd.Context(), "POST", endpoint, nil)
				if err != nil {
					return fmt.Errorf("failed to explicitly construct AST mapped REST request natively: %w", err)
				}
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("X-Flume-System-Token", envCfg.AdminToken)

				res, reqErr = astSyncClient.Do(req)
				if reqErr == nil && res.StatusCode == 200 {
					defer res.Body.Close()
					break
				}

				if res != nil {
					res.Body.Close()
				}
				if retries < cfg.RetriesLimit {
					log.Warn("AST synchronization handshake failed, retrying...",
						"attempt", fmt.Sprintf("%d/%d", retries, cfg.RetriesLimit),
						"retry_in", backoff.String(),
					)
					time.Sleep(backoff)
					backoff *= 2
				}
			}

			if reqErr != nil || (res != nil && res.StatusCode != 200) {
				statusCode := 0
				if res != nil {
					statusCode = res.StatusCode
				}
				log.Error("Failed to synchronize AST with the Flume dashboard", "error", reqErr, "http_status", statusCode)
				return fmt.Errorf("could not connect to the Flume dashboard to sync AST after 5 attempts. Please check the dashboard container logs for errors")
			}

			log.Info("Local AST Mapping Synchronized via Elastro Graph RAG Remote Decoupling.")
		}

		return nil
	},
}

func init() {
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", "Explicitly declare LLM Provider (openai, ollama, exo)")
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, "Launch Flume utilizing OS-native High-Performance Git Worktrees")
}
