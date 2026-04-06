package commands

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"

	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

var upgradeWorkersFlag string
var upgradeSkipBinaryFlag bool

var UpgradeCmd = &cobra.Command{
	Use:   "upgrade",
	Short: "Upgrade Flume to the latest release without credential re-entry or data loss",
	Long: `Checks GitHub Releases for the latest version, updates the flume binary if needed,
rebuilds Docker images, and performs a rolling restart that preserves all Elasticsearch
data and OpenBao credentials.

Credentials are restored from ~/.flume/credentials.enc (written by flume start).
If the snapshot is missing, upgrade falls back to prompting for credentials.

Worker count options:
  (default)        2 workers
  --workers 4      explicit count
  --workers auto   auto-detect from CPU cores and available RAM`,
	RunE: func(cmd *cobra.Command, args []string) error {
		fmt.Println(ui.CyberGradient("═══════════════════════════════════════"))
		fmt.Println(ui.CyberGradient("  Flume Upgrade System"))
		fmt.Println(ui.CyberGradient("═══════════════════════════════════════"))

		// ── Phase 1: Version check ────────────────────────────────────────────
		fmt.Print(ui.WarningGold("  Checking latest release... "))
		latest, assetURL, vErr := orchestrator.LatestRelease("Fremen-Labs/flume")
		if vErr != nil {
			// Non-fatal: network may be unavailable, proceed with image rebuild only
			log.Warn("Version check failed — skipping binary update", "error", vErr)
			fmt.Println(ui.WarningGold("skipped (offline)"))
		} else if orchestrator.CompareVersions(orchestrator.CurrentVersion, latest) {
			fmt.Println(ui.SuccessBlue(fmt.Sprintf("update available  %s → %s", orchestrator.CurrentVersion, latest)))
			if !upgradeSkipBinaryFlag && assetURL != "" {
				fmt.Print(ui.WarningGold(fmt.Sprintf("  Downloading %s... ", latest)))
				if err := orchestrator.SelfUpdate(assetURL); err != nil {
					// Non-fatal: continue with image rebuild even if binary update fails
					log.Warn("Binary self-update failed", "error", err)
					fmt.Println(ui.WarningGold("failed (will retry next upgrade)"))
				} else {
					fmt.Println(ui.SuccessBlue("✓"))
					fmt.Println(ui.CyberGradient("  Binary updated. The new version will take effect on next command run."))
				}
			}
		} else {
			fmt.Println(ui.SuccessBlue(fmt.Sprintf("already on latest (%s)  ✓", orchestrator.CurrentVersion)))
		}

		// ── Phase 2: Load credential snapshot ────────────────────────────────
		fmt.Print(ui.WarningGold("  Loading credential snapshot... "))
		envCfg, err := orchestrator.LoadCredentials()
		if err != nil {
			fmt.Println(ui.WarningGold("not found"))
			fmt.Println(ui.WarningGold("  No credential snapshot at ~/.flume/credentials.enc"))
			fmt.Println(ui.WarningGold("  Run 'flume start' first to create one, or enter credentials now:"))
			fmt.Println()
			// Fall back to interactive prompt (reuse start flow)
			return runUpgradeFallback(upgradeWorkersFlag)
		}
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("✓  (provider: %s · model: %s)", envCfg.Provider, envCfg.Model)))

		// ── Phase 3: Resolve worker count ─────────────────────────────────────
		workerCount := orchestrator.ResolveWorkerCount(upgradeWorkersFlag)
		fmt.Println(ui.CyberGradient(fmt.Sprintf("  Worker count: %s", orchestrator.WorkerCountDescription(upgradeWorkersFlag))))

		// ── Phase 4: Rebuild images ───────────────────────────────────────────
		fmt.Println(ui.WarningGold("  Rebuilding dashboard and worker images..."))
		buildArgs := []string{"compose", "--profile", "managed_elastic", "build", "dashboard", "worker"}
		buildCmd := exec.Command("docker", buildArgs...)
		buildCmd.Stdout = os.Stdout
		buildCmd.Stderr = os.Stderr
		buildCmd.Env = os.Environ()
		if err := buildCmd.Run(); err != nil {
			return fmt.Errorf("image build failed: %w", err)
		}

		// ── Phase 5: Rolling stop (ES + OpenBao stay running) ─────────────────
		fmt.Print(ui.WarningGold("  Stopping dashboard and workers (ES + OpenBao unaffected)... "))
		stopServices := buildStopServiceNames(workerCount)
		stopArgs := append([]string{"compose", "stop"}, stopServices...)
		stopCmd := exec.Command("docker", stopArgs...)
		stopCmd.Env = append(os.Environ(), "OPENBAO_TOKEN=flume-dev-token")
		var stopBuf bytes.Buffer
		stopCmd.Stderr = &stopBuf
		if err := stopCmd.Run(); err != nil {
			// Non-fatal: containers may already be stopped
			log.Warn("Stop had warnings", "output", stopBuf.String())
		}
		fmt.Println(ui.SuccessBlue("✓"))

		// ── Phase 6: Re-provision AppRole (OpenBao KV data preserved) ─────────
		fmt.Print(ui.WarningGold("  Re-provisioning AppRole credentials... "))
		vaultPort := "8200"
		secretID, vaultErr := orchestrator.DeployVaultTopology(vaultPort, envCfg)
		if vaultErr != nil {
			return fmt.Errorf("vault provisioning failed: %w", vaultErr)
		}
		fmt.Println(ui.SuccessBlue("✓"))

		// ── Phase 7: Start upgraded containers ────────────────────────────────
		fmt.Println(ui.CyberGradient(fmt.Sprintf("  Starting upgraded containers (%d workers)...", workerCount)))

		generatedEnv := orchestrator.GenerateEnv(envCfg)
		fullEnv := append(os.Environ(), generatedEnv...)
		fullEnv = append(fullEnv, "BAO_SECRET_ID="+secretID)

		upServices := orchestrator.BuildWorkerServiceNames(workerCount)
		upArgs := append([]string{"compose", "--profile", "managed_elastic", "up", "-d", "--wait"}, upServices...)
		upCmd := exec.Command("docker", upArgs...)
		upCmd.Env = fullEnv

		var upOut, upErr bytes.Buffer
		upCmd.Stdout = io.MultiWriter(os.Stdout, &upOut)
		upCmd.Stderr = io.MultiWriter(os.Stderr, &upErr)
		if err := upCmd.Run(); err != nil {
			combined := upOut.String() + "\n" + upErr.String()
			log.Error("Container startup failed", "output", strings.TrimSpace(combined))
			return fmt.Errorf("container startup failed: %w", err)
		}

		// ── Phase 8: Health check ─────────────────────────────────────────────
		if err := orchestrator.AwaitOrchestration(); err != nil {
			log.Error("Health check failed after upgrade. Run 'flume doctor' for diagnostics.")
			return err
		}

		// Refresh credential snapshot timestamp
		_ = orchestrator.SaveCredentials(envCfg)

		// ── Pruning builder cache to avoid Docker VM disk exhaustion ──────────
		pruneCmd := exec.Command("docker", "builder", "prune", "-f")
		pruneCmd.Stdout = os.Stdout
		pruneCmd.Stderr = os.Stderr
		pruneCmd.Run() // best-effort

		fmt.Println()
		fmt.Println(ui.SuccessBlue(fmt.Sprintf("  ✓ Upgrade complete.  Flume %s running on http://localhost:8765", latest)))
		fmt.Println(ui.CyberGradient("    ES data preserved · Credentials preserved · Build cache pruned"))
		return nil
	},
}

// buildStopServiceNames returns the service names to stop (excludes elasticsearch + openbao).
func buildStopServiceNames(workerCount int) []string {
	services := []string{"dashboard"}
	for i := 1; i <= workerCount; i++ {
		services = append(services, fmt.Sprintf("worker-%d", i))
	}
	return services
}

// runUpgradeFallback falls through to a condensed interactive credential prompt
// when no snapshot exists, then performs the normal upgrade flow.
func runUpgradeFallback(workersFlag string) error {
	log.Info("Falling back to interactive credential entry...")
	promptCfg, err := ui.RunInteractivePrompt(orchestrator.CheckExoActive())
	if err != nil {
		return fmt.Errorf("interactive prompt aborted: %w", err)
	}

	envCfg := orchestrator.EnvConfig{
		Provider:    promptCfg.Provider,
		APIKey:      promptCfg.APIKey,
		Model:       promptCfg.Model,
		RepoType:    promptCfg.RepoType,
		GithubToken: promptCfg.GithubToken,
		ADOOrg:      promptCfg.ADOOrg,
		ADOProject:  promptCfg.ADOProject,
		ADOToken:    promptCfg.ADOToken,
	}
	if promptCfg.Provider == "ollama" {
		if promptCfg.Host == "" {
			promptCfg.Host = "127.0.0.1"
		}
		envCfg.Host = promptCfg.Host
		envCfg.BaseURL = fmt.Sprintf("http://%s:11434", promptCfg.Host)
		envCfg.LocalOllamaBaseURL = fmt.Sprintf("http://%s:11434/v1", promptCfg.Host)
	}

	// Save snapshot so future upgrades are seamless
	if err := orchestrator.SaveCredentials(envCfg); err != nil {
		log.Warn("Failed to save credential snapshot", "error", err)
	}

	// Now do a normal flume start sequence since this is effectively a first run
	log.Info("Credentials captured. Running full start sequence...")
	return runStartSequence(envCfg, workersFlag)
}

// runStartSequence runs the docker compose up + vault provisioning sequence
// used by both fallback upgrade and start command.
func runStartSequence(envCfg orchestrator.EnvConfig, workersFlag string) error {
	adminToken, err := orchestrator.GenerateAdminToken()
	if err != nil {
		return err
	}
	envCfg.AdminToken = adminToken

	generatedEnv := orchestrator.GenerateEnv(envCfg)
	fullEnv := append(os.Environ(), generatedEnv...)

	// Boot data grid first
	dataArgs := []string{"compose", "--profile", "managed_elastic", "up", "-d", "--wait", "elasticsearch", "openbao"}
	dataCmd := exec.Command("docker", dataArgs...)
	dataCmd.Env = fullEnv
	dataCmd.Stdout = os.Stdout
	dataCmd.Stderr = os.Stderr
	if err := dataCmd.Run(); err != nil {
		return fmt.Errorf("data grid boot failed: %w", err)
	}

	secretID, vErr := orchestrator.DeployVaultTopology("8200", envCfg)
	if vErr != nil {
		return vErr
	}
	fullEnv = append(fullEnv, "BAO_SECRET_ID="+secretID)

	workerCount := orchestrator.ResolveWorkerCount(workersFlag)
	upServices := orchestrator.BuildWorkerServiceNames(workerCount)
	upArgs := append([]string{"compose", "--profile", "managed_elastic", "up", "-d", "--wait"}, upServices...)
	upCmd := exec.Command("docker", upArgs...)
	upCmd.Env = fullEnv
	upCmd.Stdout = os.Stdout
	upCmd.Stderr = os.Stderr
	if err := upCmd.Run(); err != nil {
		return fmt.Errorf("container topology boot failed: %w", err)
	}

	return orchestrator.AwaitOrchestration()
}

func init() {
	UpgradeCmd.Flags().StringVar(&upgradeWorkersFlag, "workers", "",
		`Number of workers to run. Options: "2" (default), "4", "auto" (detect from hardware)`)
	UpgradeCmd.Flags().BoolVar(&upgradeSkipBinaryFlag, "skip-binary-update", false,
		"Rebuild images only — do not download a new flume binary even if a newer release exists")
}
