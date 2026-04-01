package commands

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

var DestroyCmd = &cobra.Command{
	Use:   "destroy",
	Short: "Annihilate the Flume Docker ecosystem locally",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(ui.CyberGradient("Initiating Terminal Docker Annihilation Protocol..."))

		pidFile := filepath.Join(os.Getenv("HOME"), ".flume", "flume-daemon.pid")
		if pidBytes, err := os.ReadFile(pidFile); err == nil {
			if pid, parseErr := strconv.Atoi(string(pidBytes)); parseErr == nil {
				if process, pErr := os.FindProcess(pid); pErr == nil {
					if err := process.Signal(syscall.Signal(0)); err == nil {
						fmt.Println(ui.CyberGradient(fmt.Sprintf("Transmitting SIGTERM mapping to sub-orchestrator PID [%d] natively...", pid)))
						process.Signal(syscall.SIGTERM)
						time.Sleep(1 * time.Second)

						if checkErr := process.Signal(syscall.Signal(0)); checkErr == nil {
							fmt.Println(ui.WarningGold(fmt.Sprintf("PID [%d] did not respond to SIGTERM, escalating to SIGKILL...", pid)))
							process.Signal(syscall.SIGKILL)
							time.Sleep(1 * time.Second)
						}
					} else {
						fmt.Println(ui.WarningGold(fmt.Sprintf("Stale PID file detected. Sub-orchestrator PID [%d] is not running natively.", pid)))
					}
				}
			}
			os.Remove(pidFile)
		}

		c := exec.Command("docker", "compose", "down", "-v")
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		// Supply a placeholder so compose doesn't warn about an unset OPENBAO_TOKEN variable.
		destroyEnv := append(os.Environ(), "OPENBAO_TOKEN=flume-dev-token")
		c.Env = destroyEnv
		if err := c.Run(); err != nil {
			fmt.Println(ui.ErrorRed("Failed to execute docker compose down: " + err.Error()))
			return
		}

		fmt.Println(ui.CyberGradient("Pruning native OS-level parallel Git Worktrees and environment locks..."))
		
		// 1. Wipe out dynamically generated `.env` topology to avoid conflicts
		os.Remove(".env")
		
		// 2. Erase physical worktree directories
		os.RemoveAll(".flume/agents")
		
		// 3. Purge orphaned git worktree metadata natively
		pruneCmd := exec.Command("git", "worktree", "prune")
		pruneCmd.Run()
		
		// 4. Force delete residual logical branches map to agents
		out, err := exec.Command("git", "branch", "--list", "agent-worker-*").Output()
		if err == nil {
			branches := strings.Split(string(out), "\n")
			for _, b := range branches {
				b = strings.TrimSpace(b)
				b = strings.TrimPrefix(b, "* ")
				if b != "" {
					exec.Command("git", "branch", "-D", b).Run()
				}
			}
		}

		// Wipe LLM Credentials Split-Brain Native Caches
		homeDir, _ := os.UserHomeDir()
		os.RemoveAll(homeDir + "/.flume/workspace/worker-manager")
		os.RemoveAll(homeDir + "/.flume/workspace/worker_state.json")

		fmt.Println(ui.SuccessBlue("Ecosystem Scuttled natively!"))
	},
}
