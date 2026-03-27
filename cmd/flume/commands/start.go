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

var StartCmd = &cobra.Command{
	Use:   "start",
	Short: "Initiate Flume V3 Edge Orchestrator",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(ui.CyberGradient("Booting Flume Matrix..."))

		log.Infof("💾 Jacking into the local mainframe... Scanning global systemic hardware matrices 🔌")
		eco := orchestrator.PerformReconnaissance()
		fmt.Println(ui.NeonGreen(fmt.Sprintf("\n[SYSTEM RECON] Docker: %v | Elastic: %v | OpenBao: %v | Elastro: %v\n", eco.HasDocker, eco.HasElastic, eco.HasOpenBao, eco.HasElastro)))

		if err := orchestrator.EvaluateAndInstall(eco); err != nil {
			log.Fatal("Unmet Dependency Bounds", "error", err)
		}

		dashboardPort := "8765"
		vaultPort := "8200"
		esPort := "9200"

		ports := orchestrator.CheckPortBinds([]int{8765, 8200, 9200})
		for port, inUse := range ports {
			if inUse {
				newPort := ui.PromptForPort(port)
				if newPort == 0 {
					log.Fatal("Port exhaustion detected gracefully natively.")
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
			fmt.Println(ui.SuccessBlue("Exo Mac MLX Inference active globally! Bypassing LLM prompt sequences."))
		} else if envCfg.Provider == "" || envCfg.APIKey == "" {
			if NativeFlag {
				fmt.Println(ui.WarningGold("Exo undetected globally. Native orchestration bypassing structural UI credential traps natively."))
			} else {
				stat, _ := os.Stdin.Stat()
				isHeadless := (stat.Mode() & os.ModeCharDevice) == 0 || os.Getenv("CI") != "" || os.Getenv("NON_INTERACTIVE") == "1" || os.Getenv("FLUME_HEADLESS") == "1"
				if isHeadless {
					log.Fatal("Non-interactive terminal detected without an explicit Provider. Please pass -p [provider] natively to prevent pipeline hanging.")
				}
				fmt.Println(ui.WarningGold("Exo undetected globally. Escalate to User Auth Layer."))
				promptCfg, err := ui.RunInteractivePrompt()
				if err != nil {
					log.Fatal("Interactive Wizard aborted.", "error", err)
				}
				envCfg.Provider = promptCfg.Provider
				envCfg.APIKey = promptCfg.APIKey
			}
		}

		if err := orchestrator.GenerateEnv(envCfg); err != nil {
			log.Fatal("Failed to construct ecosystem environment", "error", err)
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
			fmt.Println(ui.WarningGold("🚀 Initiating hyper-threaded uplink... Deploying Docker Swarm Topology 💿"))
			c := exec.Command("docker", "compose", "up", "-d")
			c.Env = portEnvOverrides
			c.Stdout = os.Stdout
			c.Stderr = os.Stderr
			if err := c.Run(); err != nil {
				fmt.Println(ui.ErrorRed("Container topology boot failed: " + err.Error()))
				return
			}
		}

		orchestrator.AwaitOrchestration()
	},
}

func init() {
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", ui.SuccessBlue("Explicitly declare LLM Provider (openai, ollama, exo)"))
	StartCmd.Flags().StringVarP(&KeyFlag, "key", "k", "", ui.WarningGold("Explicitly declare LLM API Secret Key (Masked)"))
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, ui.CyberGradient("Launch Flume utilizing OS-native High-Performance Git Worktrees"))
}
