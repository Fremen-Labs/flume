package commands

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/Fremen-Labs/flume/cmd/flume/agents"
	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

var (
	ProviderFlag string
	NativeFlag   bool
	WorkersFlag  string
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

			if saveErr := orchestrator.SaveCredentials(envCfg); saveErr != nil {
				log.Warn("Failed to save credential snapshot (flume upgrade will require re-entry)", "error", saveErr)
			} else {
				log.Info("Credential snapshot saved to ~/.flume/credentials.enc")
			}

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

			if saveErr := orchestrator.SaveCredentials(envCfg); saveErr != nil {
				log.Warn("Failed to save credential snapshot (flume upgrade will require re-entry)", "error", saveErr)
			} else {
				log.Info("Credential snapshot saved to ~/.flume/credentials.enc")
			}

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



		return nil
	},
}

func init() {
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", "Explicitly declare LLM Provider (openai, ollama, exo)")
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, "Launch Flume utilizing OS-native High-Performance Git Worktrees")
	StartCmd.Flags().StringVar(&WorkersFlag, "workers", "", `Number of workers: "2" (default), "4", "auto" (detect from hardware)`)
}
