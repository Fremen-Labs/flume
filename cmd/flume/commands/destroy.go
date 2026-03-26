package commands

import (
	"fmt"
	"os"
	"os/exec"

	"github.com/spf13/cobra"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

var DestroyCmd = &cobra.Command{
	Use:   "destroy",
	Short: "Annihilate the Flume Docker ecosystem locally",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(ui.CyberGradient("Initiating Terminal Docker Annihilation Protocol..."))
		
		// Native API Bootloader Annihilation (Clearing ghost python servers hanging on 8765 bridging FLUME_NATIVE_MODE=1)
		exec.Command("sh", "-c", "kill -9 $(lsof -t -i:8765) 2>/dev/null || true").Run()
		exec.Command("sh", "-c", "pkill -9 -f 'src/dashboard/server.py' 2>/dev/null || true").Run()
		
		// Unbinding all native Swarm workers locking background Docker paths and memory boundaries
		exec.Command("sh", "-c", "pkill -9 -f 'src/worker-manager/manager.py' 2>/dev/null || true").Run()
		exec.Command("sh", "-c", "pkill -9 -f 'src/worker-manager/worker_handlers.py' 2>/dev/null || true").Run()
		
		// Clearing legacy Go binary orchestration bounds matching `flume start` cleanly shielding the concurrent `flume destroy` scope
		exec.Command("sh", "-c", "pkill -9 -f 'flume start' 2>/dev/null || true").Run()
		
		c := exec.Command("docker", "compose", "down", "-v")
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		if err := c.Run(); err != nil {
			fmt.Println(ui.ErrorRed("Failed to execute docker compose down: " + err.Error()))
			return
		}

		// Wipe LLM Credentials Split-Brain Native Caches
		homeDir, _ := os.UserHomeDir()
		os.RemoveAll(homeDir + "/.flume/workspace/worker-manager")
		os.RemoveAll(homeDir + "/.flume/workspace/worker_state.json")

		fmt.Println(ui.SuccessBlue("Ecosystem Scuttled natively!"))
	},
}
