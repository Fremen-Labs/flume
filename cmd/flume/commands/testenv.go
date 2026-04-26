package commands

import (
	"encoding/json"
	"fmt"

	"github.com/Fremen-Labs/flume/cmd/flume/orchestrator"
	"github.com/spf13/cobra"
)

var TestEnvCmd = &cobra.Command{
	Use:    "_testenv",
	Short:  "Internal: Output Flume infrastructure credentials",
	Long:   `Decrypts and outputs the Flume infrastructure credentials in JSON format. For use by integration test suites.`,
	Hidden: true, // Hide this command from general users
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg, err := orchestrator.LoadCredentials()
		if err != nil {
			return fmt.Errorf("failed to load credentials: %w", err)
		}

		b, err := json.MarshalIndent(cfg, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to encode credentials to json: %w", err)
		}

		fmt.Println(string(b))
		return nil
	},
}
