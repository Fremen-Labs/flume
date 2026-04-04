package agents

import (
	"context"
	"fmt"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
)

// DeployWorker provisions a parallel agent worker slot.
//
// AP-5C (K8s Readiness): Git worktrees have been replaced by ephemeral
// shallow clones managed by the Python worker-manager. The Go CLI no longer
// creates local .flume/agents/ worktrees, which were incompatible with
// ephemeral Kubernetes pods (no shared filesystem between pods).
//
// The Python ensure_task_branch() function now handles per-task isolation
// by cloning into a tmpdir (FLUME_CLONE_DEPTH shallow clone) and deleting
// it via teardown_task_clone() after the task completes.
func DeployWorker(ctx context.Context, id int, taskID string) error {
	fmt.Println(ui.SuccessBlue(fmt.Sprintf("Provisioning Parallel Agent Slot [%d] for task %s", id, taskID)))
	// Ephemeral clone is created by the Python worker when it picks up the task.
	// No local worktree directory is created here.
	return nil
}
