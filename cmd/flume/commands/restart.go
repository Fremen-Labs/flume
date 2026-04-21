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
	"github.com/spf13/cobra"
)

var (
	restartQuick     bool
	restartWithData  bool
	restartNoHealth  bool
)

// RestartCmd reloads Flume application containers so they pick up updated host .env,
// compose overrides, and mounted config. Plain `docker compose restart` does not
// reload environment variables from the host; we use `up --force-recreate` by default.
var RestartCmd = &cobra.Command{
	Use:   "restart",
	Short: "Recreate or restart Flume containers to reload environment and settings",
	Long: strings.TrimSpace(`
Restart Flume services using Docker Compose from the current directory
(where docker-compose.yml / compose.yaml lives).

By default this runs "docker compose up -d --no-build --force-recreate" for the
gateway, dashboard, and worker services so processes reload environment variables
from .env and compose (a plain "compose restart" does not reload host env).

Examples:
  flume restart                    # Recreate app containers; reload .env
  flume restart --quick            # Fast process restart only (no env reload)
  flume restart --with-data        # Also recreate elasticsearch + openbao
  flume restart --no-health-wait   # Skip waiting for dashboard /health

Run this from your Flume repository root, or set COMPOSE_FILE / COMPOSE_PROJECT_NAME.`),
	RunE: runRestart,
}

func init() {
	RestartCmd.Flags().BoolVar(&restartQuick, "quick", false, "Only restart processes (faster; does not apply new host .env values)")
	RestartCmd.Flags().BoolVar(&restartWithData, "with-data", false, "Also recreate elasticsearch and openbao (slower; brief data-plane interruption)")
	RestartCmd.Flags().BoolVar(&restartNoHealth, "no-health-wait", false, "Do not wait for dashboard /api/health after recreate")
}

func runRestart(cmd *cobra.Command, args []string) error {
	ctx := cmd.Context()

	if _, err := exec.LookPath("docker"); err != nil {
		return fmt.Errorf("docker not found in PATH — install Docker or recreate containers manually")
	}

	cwd, err := os.Getwd()
	if err != nil {
		return fmt.Errorf("get working directory: %w", err)
	}
	if !composeFilePresent(cwd) {
		return fmt.Errorf("no docker-compose.yml or compose.yaml in %s — cd to your Flume repository root first", cwd)
	}

	var services []string
	if restartWithData {
		services = []string{"elasticsearch", "openbao", "gateway", "dashboard", "worker"}
	} else {
		services = []string{"gateway", "dashboard", "worker"}
	}

	var dockerArgs []string
	if restartQuick {
		dockerArgs = append(dockerArgs, "compose", "restart")
		dockerArgs = append(dockerArgs, services...)
		fmt.Println(ui.WarningGold("Quick restart (process only — host .env changes may not apply until recreate)."))
	} else {
		dockerArgs = append(dockerArgs, "compose")
		if restartWithData {
			dockerArgs = append(dockerArgs, "--profile", "managed_elastic")
		}
		dockerArgs = append(dockerArgs, "up", "-d", "--no-build", "--force-recreate")
		dockerArgs = append(dockerArgs, services...)
		fmt.Println(ui.WarningGold("Recreating containers to reload environment (gateway, dashboard, worker)."))
	}

	fmt.Println(ui.Dim(fmt.Sprintf("  → docker %s", strings.Join(dockerArgs, " "))))

	c := exec.CommandContext(ctx, "docker", dockerArgs...)
	c.Dir = cwd
	c.Env = composeEnvForRestart()

	var stderr bytes.Buffer
	c.Stdout = os.Stdout
	c.Stderr = io.MultiWriter(os.Stderr, &stderr)
	if err := c.Run(); err != nil {
		return fmt.Errorf("docker failed: %w\n%s", err, strings.TrimSpace(stderr.String()))
	}

	fmt.Println(ui.SuccessBlue("Flume services updated."))

	if restartNoHealth {
		return nil
	}
	fmt.Println(ui.WarningGold("Waiting for dashboard health…"))
	if err := orchestrator.AwaitOrchestration(); err != nil {
		fmt.Println(ui.WarningGold(fmt.Sprintf("Health check did not pass in time: %v", err)))
		fmt.Println(ui.Dim("  Tip: run `flume doctor` or check `docker compose ps`."))
		return nil
	}
	return nil
}

func composeFilePresent(dir string) bool {
	for _, name := range []string{"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} {
		p := dir + string(os.PathSeparator) + name
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return true
		}
	}
	return false
}

func composeEnvForRestart() []string {
	env := os.Environ()
	if os.Getenv("OPENBAO_TOKEN") == "" {
		// Match upgrade/start: dev stacks often use the default token for compose substitution.
		env = append(env, "OPENBAO_TOKEN=flume-dev-token")
	}
	return env
}
