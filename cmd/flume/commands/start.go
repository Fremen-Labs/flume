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

		log.Infof("Scanning systemic hardware arrays natively...")
		eco := orchestrator.PerformReconnaissance()
		fmt.Println(ui.NeonGreen(fmt.Sprintf("\n[SYSTEM RECON] Docker: %v | Elastic: %v | OpenBao: %v | Elastro: %v\n", eco.HasDocker, eco.HasElastic, eco.HasOpenBao, eco.HasElastro)))

		ports := orchestrator.CheckPortBinds([]int{8765, 8200, 9200})
		for port, inUse := range ports {
			if inUse {
				fmt.Println(ui.WarningGold(fmt.Sprintf("Port %d is actively bound! Gracefully mapping BYOB native override...", port)))
			}
		}

		envCfg := orchestrator.EnvConfig{
			Provider: ProviderFlag,
			APIKey:   KeyFlag,
		}

		if orchestrator.CheckExoActive() {
			fmt.Println(ui.SuccessBlue("Exo Mac MLX Inference active globally! Bypassing LLM prompt sequences."))
		} else if envCfg.Provider == "" || envCfg.APIKey == "" {
			fmt.Println(ui.WarningGold("Exo undetected globally. Escalate to User Auth Layer."))
			promptCfg, err := ui.RunInteractivePrompt()
			if err != nil {
				log.Fatal("Interactive Wizard aborted.", "error", err)
			}
			envCfg.Provider = promptCfg.Provider
			envCfg.APIKey = promptCfg.APIKey
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
				dash.Env = append(os.Environ(), "PYTHONPATH=src", "FLUME_NATIVE_MODE=1", "ES_URL=http://localhost:9200", "OPENBAO_ADDR=http://localhost:8200")
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
			fmt.Println(ui.WarningGold("Deploying Complete Docker Swarm Topology natively via os/exec..."))
			c := exec.Command("docker", "compose", "up", "-d")
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
	StartCmd.Flags().StringVarP(&ProviderFlag, "provider", "p", "", "Explicitly declare LLM Provider")
	StartCmd.Flags().StringVarP(&KeyFlag, "key", "k", "", "Explicitly declare LLM API Secret Key")
	StartCmd.Flags().BoolVarP(&NativeFlag, "native", "n", false, "Launch Flume utilizing OS-native architecture with Git Worktrees")
}
