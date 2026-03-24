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
		
		c := exec.Command("docker", "compose", "down", "-v")
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		if err := c.Run(); err != nil {
			fmt.Println(ui.ErrorRed("Failed to execute docker compose down: " + err.Error()))
			return
		}

		fmt.Println(ui.SuccessBlue("Ecosystem Scuttled natively!"))
	},
}
