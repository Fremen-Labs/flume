package commands

import (
	"fmt"
	"os"
	"os/exec"
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
		}

		existingPromptCfg := ui.PromptConfig{
			Provider: strings.TrimSpace(os.Getenv("LLM_PROVIDER")),
			APIKey:   strings.TrimSpace(os.Getenv("LLM_API_KEY")),
			Host:     strings.TrimSpace(os.Getenv("LLM_HOST")),
		}
		if existingPromptCfg.Provider == "" && envCfg.LocalOllamaBaseURL != "" {
			existingPromptCfg.Provider = "ollama"
		}

		if envCfg.Provider == "" {
			envCfg.Provider = existingPromptCfg.Provider
		}
		if envCfg.APIKey == "" {
			envCfg.APIKey = existingPromptCfg.APIKey
		}

		if orchestrator.CheckExoActive() {
			log.Info("Exo Mac MLX Inference active globally! Bypassing LLM prompt sequences.")
		} else if NativeFlag {
			log.Warn("Exo undetected globally. Native orchestration bypassing structural UI credential traps natively.")
		} else {
			if isHeadlessEnv(os.Getenv, os.Stdin.Stat) && envCfg.Provider == "" {
				log.Error("Non-interactive terminal detected without an explicit Provider. Please pass -p [provider] natively to prevent pipeline hanging.")
				return fmt.Errorf("headless tty pseudo-hang prevented")
			}

			if existingPromptCfg.Provider != "" {
				log.Warn("Exo undetected globally. Existing .env LLM configuration detected.")
				promptCfg, err := ui.RunInteractivePrompt(existingPromptCfg)
				if err != nil {
					log.Error("Interactive Wizard aborted.", "error", err)
					return err
				}
				envCfg.Provider = promptCfg.Provider
				envCfg.APIKey = promptCfg.APIKey
				if promptCfg.Provider == "ollama" {
					if promptCfg.Host == "" {
						promptCfg.Host = "127.0.0.1"
					}
					envCfg.Host = promptCfg.Host
					envCfg.BaseURL = fmt.Sprintf("http://%s:11434", promptCfg.Host)
					envCfg.LocalOllamaBaseURL = fmt.Sprintf("http://%s:11434/v1", promptCfg.Host)
					envCfg.APIKey = ""
				}
			} else if envCfg.Provider == "" || (envCfg.APIKey == "" && envCfg.Provider != "ollama") {
				log.Warn("Exo undetected globally. Escalate to User Auth Layer.")
				promptCfg, err := ui.RunInteractivePrompt()
				if err != nil {
					log.Error("Interactive Wizard aborted.", "error", err)
					return err
				}
				envCfg.Provider = promptCfg.Provider
				envCfg.APIKey = promptCfg.APIKey
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
		}

		if err := orchestrator.GenerateEnv(envCfg); err != nil {
			log.Error("Failed to construct ecosystem environment", "error", err)
			return err
		}

		if NativeFlag {
			log.Info("Executing Flume High Performance Native Subsystems.")
			c := exec.Command("docker", "compose", "up", "-d", "elasticsearch", "openbao")
			output, err := c.CombinedOutput()
			if err != nil {
				log.Error("Data grid boot failed", "error", err, "output", strings.TrimSpace(string(output)))
			} else {
				log.Info("Data grid bootstrapped successfully", "output", strings.TrimSpace(string(output)))
			}

			go func() {
				log.Info("Spawning FastAPI Dashboard daemon natively...")
				dash := exec.Command("uv", "run", "src/dashboard/server.py")

				dashEnv := append(portEnvOverrides, "PYTHONPATH=src", "FLUME_NATIVE_MODE=1", "ES_URL=http://localhost:"+esPort, "OPENBAO_ADDR=http://localhost:"+vaultPort)
				if envCfg.Provider != "" {
					dashEnv = append(dashEnv, "LLM_PROVIDER="+envCfg.Provider)
				}
				if envCfg.BaseURL != "" {
					dashEnv = append(dashEnv, "LLM_BASE_URL="+envCfg.BaseURL)
				}
				if envCfg.LocalOllamaBaseURL != "" {
					dashEnv = append(dashEnv, "LOCAL_OLLAMA_BASE_URL="+envCfg.LocalOllamaBaseURL)
				}
				if envCfg.Host != "" {
					dashEnv = append(dashEnv, "LLM_HOST="+envCfg.Host)
				}
				if envCfg.APIKey != "" {
					dashEnv = append(dashEnv, "LLM_API_KEY="+envCfg.APIKey)
				}
				dash.Env = dashEnv

				dash.Stdout = os.Stdout
				dash.Stderr = os.Stderr
				dash.Run()
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
			log.Info("Native Flume subsystems launched.")
			return nil
		}

		log.Warn("🚀 Initiating hyper-threaded uplink... Deploying Docker Swarm Topology 💿")
		c := exec.Command("docker", "compose", "up", "-d")
		c.Env = portEnvOverrides
		output, err := c.CombinedOutput()
		if err != nil {
			log.Error("Container topology boot failed", "error", err, "output", strings.TrimSpace(string(output)))
			return err
		}
		log.Info("Container Swarm bootstrapped successfully", "output", strings.TrimSpace(string(output)))
		log.Info("Flume services started in detached mode.", "dashboard", fmt.Sprintf("http://localhost:%s", dashboardPort))
		return nil
	},
}

func init() {
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", "Explicitly declare LLM Provider (openai, ollama, exo)")
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, "Launch Flume utilizing OS-native High-Performance Git Worktrees")
}
