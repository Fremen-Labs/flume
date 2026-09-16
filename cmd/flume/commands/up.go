package commands

import (
	"fmt"
	"os"
	"os/exec"

	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

var UpCmd = &cobra.Command{
	Use:   "up",
	Short: "Start Flume Core (OpenBao, Elasticsearch, Gateway, console)",
	Long: `Starts the four-service core stack with docker compose.

The agent mesh (dashboard + workers) is not started. Use:
  docker compose --profile full up
once that profile is explicitly needed.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx := cmd.Context()
		fmt.Println(ui.BootPhase("Starting Flume Core..."))

		c := exec.CommandContext(ctx, "docker", "compose", "up", "-d", "--build", "--wait")
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		c.Env = os.Environ()
		if err := c.Run(); err != nil {
			return fmt.Errorf("docker compose up: %w", err)
		}

		if err := orchestrator.AwaitOrchestration(); err != nil {
			return err
		}
		fmt.Println(ui.SuccessBlue("Flume Core is up at http://127.0.0.1:8765"))
		return nil
	},
}
