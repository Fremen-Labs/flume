package worker

import (
	"encoding/json"
	"log/slog"
	"os"

	"github.com/Fremen-Labs/flume/internal/config"
	ftypes "github.com/Fremen-Labs/flume/pkg/types"
)

// BuildWorkers constructs the worker roster from config and node capabilities.
// Derived from Python: orchestration/workers.py (206 LOC, 7 AST nodes).
//
// Worker construction follows a deterministic naming pattern:
//
//	{role}-{node_id}-{index}
//
// Each role gets WorkersPerRole workers (default: 1 per role).
// Execution host and model are resolved from agent-models settings in ES.
func BuildWorkers(cfg *config.Config, nodeID string, nodeCaps map[string]int) []ftypes.Worker {
	roles := []string{"implementer", "reviewer", "tester", "pm"}
	workersPerRole := cfg.WorkersPerRole
	if workersPerRole <= 0 {
		workersPerRole = 1
	}

	var workers []ftypes.Worker
	for _, role := range roles {
		for i := 0; i < workersPerRole; i++ {
			name := role
			if nodeID != "" {
				name = role + "-" + nodeID
			}
			if workersPerRole > 1 {
				name = role + "-" + nodeID + "-" + string(rune('0'+i))
			}
			workers = append(workers, ftypes.Worker{
				Name:          name,
				Role:          role,
				Status:        ftypes.WorkerStatusIdle,
				Model:         cfg.LLMModel,
				Provider:      cfg.LLMProvider,
				ExecutionHost: resolveExecutionHost(nodeID),
			})
		}
	}

	return workers
}

// resolveExecutionHost determines which node this worker runs on.
func resolveExecutionHost(nodeID string) string {
	if host := os.Getenv("EXECUTION_HOST"); host != "" {
		return host
	}
	if nodeID != "" {
		return nodeID
	}
	return "localhost"
}

// ApplyAgentModelsOverrides applies per-role model/provider overrides from ES config.
// Derived from Python: orchestration/workers.build_workers() agent models integration.
func ApplyAgentModelsOverrides(workers []ftypes.Worker, modelsDoc json.RawMessage, logger *slog.Logger) {
	if modelsDoc == nil {
		return
	}

	var models ftypes.AgentModelsDoc
	if err := json.Unmarshal(modelsDoc, &models); err != nil {
		logger.Warn("Failed to parse agent models config", slog.String("error", err.Error()))
		return
	}

	for i := range workers {
		role := workers[i].Role

		// Check for role-specific override first
		if roleSpec, ok := models.Roles[role]; ok {
			if roleSpec.Model != "" {
				workers[i].Model = roleSpec.Model
			}
			if roleSpec.Provider != "" {
				workers[i].Provider = roleSpec.Provider
			}
			if roleSpec.ExecutionHost != "" {
				workers[i].ExecutionHost = roleSpec.ExecutionHost
			}
			if roleSpec.CredentialID != "" {
				workers[i].CredentialID = roleSpec.CredentialID
			}
			continue
		}

		// Fall back to global config
		if models.Global.Model != "" {
			workers[i].Model = models.Global.Model
		}
		if models.Global.Provider != "" {
			workers[i].Provider = models.Global.Provider
		}
		if models.Global.ExecutionHost != "" {
			workers[i].ExecutionHost = models.Global.ExecutionHost
		}
		if models.Global.CredentialID != "" {
			workers[i].CredentialID = models.Global.CredentialID
		}
	}
}
