package orchestrator

import (
	"fmt"
	"os"

	"github.com/charmbracelet/log"
)

type EnvConfig struct {
	Provider string
	APIKey   string
}

// GenerateEnv dynamically overrides the interactive terminal requirements mapping templates securely using os/fs.
func GenerateEnv(config EnvConfig) error {
	log.Info("Constructing `.env` topology dynamically natively via os/fs...")

	content := `# ------------------------------------------
# Flume Docker Orchestrator Topology Grid
# ------------------------------------------
DASHBOARD_PORT=8765

# ------------------------------------------
# LLM Inference (Ephemeral CLI Overrides)
# ------------------------------------------
`
	if config.Provider != "" {
		content += fmt.Sprintf("LLM_PROVIDER=%s\n", config.Provider)
	}
	if config.APIKey != "" {
		content += fmt.Sprintf("LLM_API_KEY=%s\n", config.APIKey)
	}

	err := os.WriteFile(".env", []byte(content), 0644)
	if err != nil {
		return fmt.Errorf("failed to explicitly write .env natively: %w", err)
	}
	
	log.Info("Successfully serialized `.env` topology into Swarm cache.", "isolation", "Workspace isolation protects UI configurations")
	return nil
}
