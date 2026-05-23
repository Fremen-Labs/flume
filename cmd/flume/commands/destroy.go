package commands

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

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
		ctx := cmd.Context()
		fmt.Println(ui.CyberGradient("Destroying Flume ecosystem..."))

		// Phase 5: No PID file dance needed — services are goroutines
		// that terminate via context cancellation when the CLI exits.

		// Supply a placeholder so compose doesn't warn about an unset OPENBAO_TOKEN variable.
		destroyEnv := append(os.Environ(), "OPENBAO_TOKEN=flume-dev-token")

		// --- Standard destroy: stop ALL Flume containers ---
		// docker compose down removes all services defined in the compose file.
		fmt.Println(ui.CyberGradient("Removing Flume containers and volumes (openbao, workers, dashboard, elasticsearch)..."))
		downArgs := []string{"compose", "--profile", "managed_elastic", "down", "-v"}
		c := exec.CommandContext(ctx, "docker", downArgs...)
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		c.Env = destroyEnv
		if err := c.Run(); err != nil {
			fmt.Println(ui.ErrorRed("Failed to execute docker compose down: " + err.Error()))
			return
		}

		// --- Purge mode: clean Docker build artifacts ---
		if destroyPurgeFlag {
			if !confirmPurge() {
				fmt.Println(ui.WarningGold("Purge cancelled. Containers and volumes were already removed above."))
				runWorkspaceCleanup()
				fmt.Println(ui.SuccessBlue("Ecosystem Scuttled natively!"))
				return
			}

			fmt.Println(ui.WarningGold("⚡ PURGE MODE: Removing all Flume Docker images..."))
			// Remove the unified Go flume image (used by dashboard, gateway, worker services)
			for _, img := range []string{"flume-dashboard", "flume-gateway", "flume-worker"} {
				rmiCmd := exec.CommandContext(ctx, "docker", "rmi", img+":latest")
				rmiCmd.Stderr = os.Stderr
				if err := rmiCmd.Run(); err != nil {
					fmt.Println(ui.WarningGold(fmt.Sprintf("  Image %s not found (skipped)", img)))
				} else {
					fmt.Println(ui.SuccessBlue("✔ Removed image: " + img))
				}
			}
			fmt.Println(ui.SuccessBlue("✔ All Flume Docker images purged."))
		}

		// Always prune the full builder cache on every destroy (purge or not).
		// The old label-filtered prune missed most intermediate build layers
		// because they don't carry the compose project label. Accumulated cache
		// from repeated destroy/rebuild cycles filled the Docker VM disk (29.9GB
		// observed), causing Elasticsearch to fail on /tmp writes at startup.
		fmt.Println(ui.CyberGradient("Pruning Docker builder cache..."))
		pruneCache := exec.CommandContext(ctx, "docker", "builder", "prune", "-f")
		pruneCache.Stdout = os.Stdout
		pruneCache.Stderr = os.Stderr
		pruneCache.Run() // best-effort

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
	fmt.Println(ui.WarningGold("    • All Docker build cache layers"))
	fmt.Println(ui.WarningGold("    • All workspace state artifacts"))
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

// resolveWorkspaceRoot returns the Flume workspace root path.
//
// Resolution order:
//  1. FLUME_WORKSPACE env var (set by docker-compose / flume start)
//  2. Parent of the current working directory — matches the ..:/workspace
//     docker-compose bind-mount topology used in single-box deployments.
func resolveWorkspaceRoot() string {
	if ws := strings.TrimSpace(os.Getenv("FLUME_WORKSPACE")); ws != "" {
		return ws
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "."
	}
	return filepath.Dir(cwd)
}

// runWorkspaceCleanup removes state files and environment artifacts.
//
// AP-5C (K8s Readiness): Git worktree cleanup has been removed.
// Workers now use ephemeral shallow clones in /tmp which are self-cleaning.
// The .flume/agents/ directory and 'git worktree prune' are no longer needed.
//
// AP-13 (K8s Readiness): Sweep pre-AP-era filesystem anti-pattern artifacts
// that survive across destroy cycles via the ..:/workspace bind mount.
// These directories/files are no longer written by current code but accumulate
// on the host if not explicitly removed.
func runWorkspaceCleanup() {
	fmt.Println(ui.CyberGradient("Cleaning environment locks and state files..."))

	// 1. Wipe out dynamically generated `.env` topology to avoid conflicts
	os.Remove(".env")

	// 2. Wipe LLM Credentials Split-Brain Native Caches
	homeDir, _ := os.UserHomeDir()
	os.RemoveAll(homeDir + "/.flume/workspace/worker-manager")
	os.RemoveAll(homeDir + "/.flume/workspace/worker_state.json")

	// 3. AP-13: Sweep stale workspace-root filesystem anti-pattern artifacts.
	//    These were written by pre-AP-1 through pre-AP-9 code and are no longer
	//    produced by the running system, but persist across destroy cycles.
	ws := resolveWorkspaceRoot()
	staleArtifacts := []string{
		"logs",                   // AP-6: replaced by stdout
		"plan-sessions",          // AP-9: replaced by agent-plan-sessions ES index
		"repos",                  // legacy git cache — superseded by GitHostClient
		"worker-manager",         // AP-2: vestigial state directory
		"sequence_counters.json", // AP-1: replaced by flume-counters ES index
		"worker_state.json",      // AP-2: replaced by ES
		"planner-debug.log",      // AP-6: replaced by stdout
		"elastro.log",            // stale elastro library log
	}
	for _, name := range staleArtifacts {
		target := filepath.Join(ws, name)
		if info, err := os.Stat(target); err == nil {
			if err := os.RemoveAll(target); err == nil {
				_ = info // suppress unused warning
				fmt.Println(ui.SuccessBlue("  Purged stale artifact: " + target))
			} else {
				fmt.Println(ui.WarningGold("  Could not remove " + target + ": " + err.Error()))
			}
		}
	}

	// 4. AP-13: Sweep orphaned proj-* registration clone directories.
	//    AP-11 routes new registration clones to /tmp, but any proj-* dirs from
	//    pre-AP-11 container runs are stranded on the workspace bind-mount.
	//    ES (flume-projects index) is the source of truth for project data.
	entries, err := os.ReadDir(ws)
	if err == nil {
		for _, e := range entries {
			if strings.HasPrefix(e.Name(), "proj-") && e.IsDir() {
				target := filepath.Join(ws, e.Name())
				if err := os.RemoveAll(target); err == nil {
					fmt.Println(ui.SuccessBlue("  Purged orphaned clone: " + target))
				} else {
					fmt.Println(ui.WarningGold("  Could not remove " + target + ": " + err.Error()))
				}
			}
		}
	}
}

func init() {
	DestroyCmd.Flags().BoolVarP(
		&destroyPurgeFlag, "purge", "P", false,
		"Hard-delete ALL Flume Docker images in addition to containers/volumes (prompts for confirmation)",
	)
}

