package agents

import (
	"context"
	"fmt"
	"os/exec"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

// DeployWorker provisions an isolated OS-level parallel Git Worktree!
// This strictly maps to Google's engineering standards by utilizing context-aware process executions.
func DeployWorker(ctx context.Context, id int, taskID string) error {
	branch := fmt.Sprintf("agent-worker-%d-%s", id, taskID)
	dir := fmt.Sprintf(".flume/agents/%s", branch)

	fmt.Println(ui.SuccessBlue(fmt.Sprintf("Provisioning Parallel Agent [%d] -> %s", id, dir)))
	
	cmd := exec.CommandContext(ctx, "git", "worktree", "add", "-b", branch, dir, "main")
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("git worktree synthesis failed: %w", err)
	}

	return nil
}
