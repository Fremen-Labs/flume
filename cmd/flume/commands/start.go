package commands

import (
	"fmt"
	"os"
	"os/exec"
	"sync"

	"github.com/spf13/cobra"
	"github.com/charmbracelet/log"
	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/Fremen-Labs/flume/cmd/flume/agents"
)

var (
	ProviderFlag string
	KeyFlag      string
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
		log.Info(fmt.Sprintf("[SYSTEM RECON] Docker: %v | Elastic: %v | OpenBao: %v | Elastro: %v", eco.HasDocker, eco.HasElastic, eco.HasOpenBao, eco.HasElastro))

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
			Provider: ProviderFlag,
			APIKey:   KeyFlag,
		}

		if orchestrator.CheckExoActive() {
			log.Info("Exo Mac MLX Inference active globally! Bypassing LLM prompt sequences.")
		} else if envCfg.Provider == "" || envCfg.APIKey == "" {
			if NativeFlag {
				log.Warn("Exo undetected globally. Native orchestration bypassing structural UI credential traps natively.")
			} else {
				if isHeadlessEnv(os.Getenv, os.Stdin.Stat) {
					log.Error("Non-interactive terminal detected without an explicit Provider. Please pass -p [provider] natively to prevent pipeline hanging.")
					return fmt.Errorf("headless tty pseudo-hang prevented")
				}
				log.Warn("Exo undetected globally. Escalate to User Auth Layer.")
				promptCfg, err := ui.RunInteractivePrompt()
				if err != nil {
					log.Error("Interactive Wizard aborted.", "error", err)
					return err
				}
				envCfg.Provider = promptCfg.Provider
				envCfg.APIKey = promptCfg.APIKey
			}
		}

		if err := orchestrator.GenerateEnv(envCfg); err != nil {
			log.Error("Failed to construct ecosystem environment", "error", err)
			return err
		}

		if NativeFlag {
			log.Info("Executing Flume High Performance Native Subsystems.")
			c := exec.Command("docker", "compose", "up", "-d", "elasticsearch", "openbao")
			c.Stdout = os.Stdout
			c.Stderr = os.Stderr
			if err := c.Run(); err != nil {
				log.Error("Data grid boot failed", "error", err)
			}

			go func() {
				log.Info("Spawning FastAPI Dashboard daemon natively...")
				dash := exec.Command("uv", "run", "src/dashboard/server.py")
				
				dashEnv := append(portEnvOverrides, "PYTHONPATH=src", "FLUME_NATIVE_MODE=1", "ES_URL=http://localhost:"+esPort, "OPENBAO_ADDR=http://localhost:"+vaultPort)
				if envCfg.Provider != "" {
					dashEnv = append(dashEnv, "LLM_PROVIDER="+envCfg.Provider)
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
		} else {
			log.Warn("🚀 Initiating hyper-threaded uplink... Deploying Docker Swarm Topology 💿")
			c := exec.Command("docker", "compose", "up", "-d")
			c.Env = portEnvOverrides
			c.Stdout = os.Stdout
			c.Stderr = os.Stderr
			if err := c.Run(); err != nil {
				log.Error("Container topology boot failed", "error", err)
				return err
			}
		}

		orchestrator.AwaitOrchestration()
		return nil
	},
}

func init() {
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", "Explicitly declare LLM Provider (openai, ollama, exo)")
	StartCmd.Flags().StringVarP(&KeyFlag, "key", "k", "", "Explicitly declare LLM API Secret Key (Masked)")
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, "Launch Flume utilizing OS-native High-Performance Git Worktrees")
}
