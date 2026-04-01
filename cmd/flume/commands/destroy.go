package commands

import (
	"bufio"
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

var destroyPurgeFlag bool

var DestroyCmd = &cobra.Command{
	Use:   "destroy",
	Short: "Annihilate the Flume Docker ecosystem locally",
	Long: `Stops and removes all Flume containers and volumes (openbao, workers, dashboard).

Use --purge to also remove Elasticsearch containers/volumes and all Flume Docker images.
This is a hard reset that requires explicit confirmation.`,
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println(ui.CyberGradient("Initiating Terminal Docker Annihilation Protocol..."))

		// --- Kill any native daemon PID ---
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

		// Supply a placeholder so compose doesn't warn about an unset OPENBAO_TOKEN variable.
		destroyEnv := append(os.Environ(), "OPENBAO_TOKEN=flume-dev-token")

		// --- Standard destroy: always include managed_elastic profile so ES containers/volumes are also stopped ---
		fmt.Println(ui.CyberGradient("Removing Flume containers and volumes (openbao, workers, dashboard, elasticsearch)..."))
		downArgs := []string{"compose", "--profile", "managed_elastic", "down", "-v"}
		c := exec.Command("docker", downArgs...)
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		c.Env = destroyEnv
		if err := c.Run(); err != nil {
			fmt.Println(ui.ErrorRed("Failed to execute docker compose down: " + err.Error()))
			return
		}

		// --- Purge mode: remove ALL Flume Docker images too ---
		if destroyPurgeFlag {
			if !confirmPurge() {
				fmt.Println(ui.WarningGold("Purge cancelled. Containers and volumes were already removed above."))
				runWorkspaceCleanup()
				fmt.Println(ui.SuccessBlue("Ecosystem Scuttled natively!"))
				return
			}

			fmt.Println(ui.WarningGold("⚡ PURGE MODE: Removing all Flume Docker images..."))
			flumeImages := []string{"flume-dashboard", "flume-worker"}
			for _, img := range flumeImages {
				rmImg := exec.Command("docker", "rmi", "-f", img)
				rmImg.Stdout = os.Stdout
				rmImg.Stderr = os.Stderr
				if err := rmImg.Run(); err != nil {
					fmt.Println(ui.WarningGold(fmt.Sprintf("Could not remove image %s (may not exist): %s", img, err.Error())))
				} else {
					fmt.Println(ui.SuccessBlue(fmt.Sprintf("Removed image: %s", img)))
				}
			}

			// Also remove dangling build cache layers associated with flume
			pruneCache := exec.Command("docker", "builder", "prune", "-f", "--filter", "label=com.docker.compose.project=flume")
			pruneCache.Stdout = os.Stdout
			pruneCache.Stderr = os.Stderr
			pruneCache.Run() // best-effort

			fmt.Println(ui.SuccessBlue("All Flume Docker images purged."))
		}

		runWorkspaceCleanup()
		fmt.Println(ui.SuccessBlue("Ecosystem Scuttled natively!"))
	},
}

// confirmPurge renders a branded danger prompt and reads y/yes to confirm.
func confirmPurge() bool {
	fmt.Println()
	fmt.Println(ui.ErrorRed("═══════════════════════════════════════════════════════════"))
	fmt.Println(ui.ErrorRed("  ⚠  HARD PURGE — POINT OF NO RETURN  ⚠"))
	fmt.Println(ui.ErrorRed("═══════════════════════════════════════════════════════════"))
	fmt.Println(ui.WarningGold("  This will permanently remove:"))
	fmt.Println(ui.WarningGold("    • All Flume Docker images (flume-dashboard, flume-worker)"))
	fmt.Println(ui.WarningGold("    • All associated build cache layers"))
	fmt.Println(ui.WarningGold("  The next `flume start` will perform a full image rebuild."))
	fmt.Println()
	fmt.Print(ui.NeonGreen("  Are you sure? Type 'yes' to confirm: "))

	reader := bufio.NewReader(os.Stdin)
	input, err := reader.ReadString('\n')
	if err != nil {
		return false
	}
	response := strings.ToLower(strings.TrimSpace(input))
	return response == "yes" || response == "y"
}

// runWorkspaceCleanup removes local git worktrees, state files, and .env artifacts.
func runWorkspaceCleanup() {
	fmt.Println(ui.CyberGradient("Pruning native OS-level parallel Git Worktrees and environment locks..."))

	// 1. Wipe out dynamically generated `.env` topology to avoid conflicts
	os.Remove(".env")

	// 2. Erase physical worktree directories
	os.RemoveAll(".flume/agents")

	// 3. Purge orphaned git worktree metadata natively
	pruneCmd := exec.Command("git", "worktree", "prune")
	pruneCmd.Run()

	// 4. Force delete residual logical branches mapped to agents
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

	// 5. Wipe LLM Credentials Split-Brain Native Caches
	homeDir, _ := os.UserHomeDir()
	os.RemoveAll(homeDir + "/.flume/workspace/worker-manager")
	os.RemoveAll(homeDir + "/.flume/workspace/worker_state.json")
}

func init() {
	DestroyCmd.Flags().BoolVarP(
		&destroyPurgeFlag, "purge", "P", false,
		"Hard-delete ALL Flume Docker images in addition to containers/volumes (prompts for confirmation)",
	)
}
