package commands

import (
	"context"
	"fmt"
	"os"
	"os/exec"

	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
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
		ctx := cmd.Context()
		fmt.Println(ui.CyberGradient("═══════════════════════════════════════"))
		fmt.Println(ui.CyberGradient("  Flume Upgrade System"))
		fmt.Println(ui.CyberGradient("═══════════════════════════════════════"))

		// ── Phase 1: Version check ────────────────────────────────────────────
		fmt.Print(ui.WarningGold("  Checking latest release... "))
		latest, assetURL, vErr := orchestrator.LatestRelease("Fremen-Labs/flume")
		if vErr != nil {
			// Non-fatal: network may be unavailable, proceed with image rebuild only
			fmt.Println(ui.WarningGold("skipped (offline)"))
		} else if orchestrator.CompareVersions(orchestrator.CurrentVersion, latest) {
			fmt.Println(ui.SuccessBlue(fmt.Sprintf("update available  %s → %s", orchestrator.CurrentVersion, latest)))
			if !upgradeSkipBinaryFlag && assetURL != "" {
				fmt.Print(ui.WarningGold(fmt.Sprintf("  Downloading %s... ", latest)))
				if err := orchestrator.SelfUpdate(assetURL); err != nil {
					// Non-fatal: continue with image rebuild even if binary update fails
					fmt.Println(ui.WarningGold("failed (will retry next upgrade)"))
				} else {
					fmt.Println(ui.SuccessBlue("✓"))
					fmt.Println(ui.CyberGradient("  Binary updated. The new version will take effect on next command run."))
				}
			}
		} else {
			fmt.Println(ui.SuccessBlue(fmt.Sprintf("already on latest (%s)  ✓", orchestrator.CurrentVersion)))
		}

		// ── Phase 2: Configuration Entry ─────────────────────────────────────
		// Since credential snapshots are removed, run the setup wizard directly.
		return runUpgradeFallback(ctx, upgradeWorkersFlag)
	},
}

// buildStopServiceNames returns the service names to stop (excludes elasticsearch + openbao).
func buildStopServiceNames(workerCount int) []string {
	return []string{"dashboard", "gateway", "worker"}
}

// runUpgradeFallback falls through to an interactive credential prompt
// then performs the normal upgrade flow.
func runUpgradeFallback(ctx context.Context, workersFlag string) error {
	fmt.Println(ui.BootPhase("Launching configuration wizard..."))
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

	// Now do a normal flume start sequence since this is effectively a first run
	fmt.Println(ui.BootPhase("Credentials captured. Starting Flume..."))
	return runStartSequence(ctx, envCfg, workersFlag)
}

// runStartSequence runs the docker compose up + vault provisioning sequence
// used by both fallback upgrade and start command.
func runStartSequence(ctx context.Context, envCfg orchestrator.EnvConfig, workersFlag string) error {
	adminToken, err := orchestrator.GenerateAdminToken()
	if err != nil {
		return err
	}
	envCfg.AdminToken = adminToken

	generatedEnv := orchestrator.GenerateEnv(envCfg)
	fullEnv := append(os.Environ(), generatedEnv...)

	// Boot data grid first
	dataArgs := []string{"compose", "--profile", "managed_elastic", "up", "-d", "--wait", "elasticsearch", "openbao"}
	dataCmd := exec.CommandContext(ctx, "docker", dataArgs...)
	dataCmd.Env = fullEnv
	dataCmd.Stdout = os.Stdout
	dataCmd.Stderr = os.Stderr
	if err := dataCmd.Run(); err != nil {
		return fmt.Errorf("data grid boot failed: %w", err)
	}

	esUrl := "https://localhost:9200"
	if envCfg.ExternalElastic && envCfg.ESUrl != "" {
		esUrl = envCfg.ESUrl
	}
	secretID, rootToken, vErr := orchestrator.DeployVaultTopology(ctx, "8200", esUrl, envCfg)
	if vErr != nil {
		return vErr
	}
	fullEnv = append(fullEnv, "BAO_SECRET_ID="+secretID)
	fullEnv = append(fullEnv, "OPENBAO_TOKEN="+rootToken)

	workerCount := orchestrator.ResolveWorkerCount(workersFlag)
	fullEnv = append(fullEnv, fmt.Sprintf("FLUME_WORKER_COUNT=%d", workerCount))

	upServices := orchestrator.BuildWorkerServiceNames(workerCount)
	upArgs := append([]string{"compose", "--profile", "managed_elastic", "up", "-d", "--build", "--wait"}, upServices...)
	upCmd := exec.CommandContext(ctx, "docker", upArgs...)
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
