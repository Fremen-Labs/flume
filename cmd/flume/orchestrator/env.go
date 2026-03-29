package orchestrator

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"

	"github.com/charmbracelet/log"
)

type EnvConfig struct {
	Provider   string
	APIKey     string
	AdminToken string
}

// GenerateAdminToken creates a 256-bit cryptographically secure token.
func GenerateAdminToken() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return "flume_adm_" + hex.EncodeToString(bytes), nil
}

// GenerateEnv dynamically overrides the interactive terminal requirements mapping templates securely using os/fs.
func GenerateEnv(config EnvConfig) error {
	log.Info("Constructing `.env` topology dynamically natively via os/fs...")

	content := `# ------------------------------------------
# Flume Docker Orchestrator Topology Grid
# ------------------------------------------
DASHBOARD_PORT=8765
VAULT_TOKEN=flume-dev-token
OPENBAO_TOKEN=flume-dev-token

# ------------------------------------------
# LLM Inference (Ephemeral CLI Overrides)
# ------------------------------------------
`
	if config.AdminToken != "" {
		content += fmt.Sprintf("FLUME_ADMIN_TOKEN=%s\n", config.AdminToken)
	}

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
