package commands

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
	"github.com/spf13/cobra"
)

var (
	tasksStatusFilter  string
	tasksProjectFilter string
	tasksJSON          bool
)

var TasksCmd = &cobra.Command{
	Use:   "tasks",
	Short: "Inspect and manage Flume agent tasks",
	Long: `List, filter, inspect, and transition tasks in the Flume agent pipeline.
Run 'flume tasks <task_id>' to see a detailed task card.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if len(args) > 0 {
			return showTask(args[0])
		}
		return listTasks()
	},
}

// history sub-command
var tasksHistoryCmd = &cobra.Command{
	Use:   "history <task_id>",
	Short: "Show the agent log history for a task",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		return showTaskHistory(args[0])
	},
}

// diff sub-command
var tasksDiffCmd = &cobra.Command{
	Use:   "diff <task_id>",
	Short: "Show the git diff for a task's branch",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		return showTaskDiff(args[0])
	},
}

// transition sub-command
var tasksTransitionCmd = &cobra.Command{
	Use:   "transition <task_id> <state>",
	Short: "Move a task to a new state (ready, in_progress, blocked, done)",
	Args:  cobra.ExactArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		return transitionTask(args[0], args[1])
	},
}

// stop-all sub-command
var tasksStopAllCmd = &cobra.Command{
	Use:   "stop-all",
	Short: "Stop all currently running tasks",
	RunE: func(cmd *cobra.Command, args []string) error {
		client := ui.NewFlumeClient()
		result, err := client.Post("/api/tasks/stop-all", nil)
		if err != nil {
			return fmt.Errorf("failed to stop tasks: %w", err)
		}
		log.Info("All tasks stopped", "result", result)
		fmt.Println(ui.SuccessBlue("All running tasks stopped."))
		return nil
	},
}

func listTasks() error {
	client := ui.NewFlumeClient()
	state, err := client.Get("/api/system-state")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	tasks := extractTasks(state)

	if tasksJSON {
		b, _ := json.MarshalIndent(tasks, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	headers := []string{"Task ID", "Title", "Status", "Project", "Model", "Branch"}
	var rows [][]string
	for _, t := range tasks {
		status := stringVal(t, "status", "—")
		if tasksStatusFilter != "" && !strings.EqualFold(status, tasksStatusFilter) {
			continue
		}
		project := stringVal(t, "project_id", "—")
		if tasksProjectFilter != "" && !strings.EqualFold(project, tasksProjectFilter) {
			continue
		}
		id := truncate(stringVal(t, "task_id", stringVal(t, "_id", "—")), 18)
		title := truncate(stringVal(t, "title", stringVal(t, "description", "—")), 40)
		model := truncate(stringVal(t, "model", "—"), 20)
		branch := truncate(stringVal(t, "branch", "—"), 30)
		rows = append(rows, []string{id, title, ui.StatusBadge(status), project, model, branch})
	}

	if len(rows) == 0 {
		fmt.Println(ui.WarningGold("No tasks found matching filters."))
		return nil
	}
	fmt.Println(ui.NeonGreen(fmt.Sprintf("  TASKS (%d)  ", len(rows))))
	fmt.Print(ui.RenderTable(headers, rows))
	return nil
}

func showTask(taskID string) error {
	client := ui.NewFlumeClient()
	state, err := client.Get("/api/system-state")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	tasks := extractTasks(state)
	var task map[string]any
	for _, t := range tasks {
		if stringVal(t, "task_id", stringVal(t, "_id", "")) == taskID {
			task = t
			break
		}
	}
	if task == nil {
		return fmt.Errorf("task '%s' not found", taskID)
	}

	if tasksJSON {
		b, _ := json.MarshalIndent(task, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen(fmt.Sprintf("  TASK: %s  ", taskID)))
	fields := []struct{ k, label string }{
		{"title", "Title"},
		{"status", "Status"},
		{"project_id", "Project"},
		{"branch", "Branch"},
		{"model", "Model"},
		{"description", "Description"},
		{"updated_at", "Updated"},
	}
	for _, f := range fields {
		v := stringVal(task, f.k, "")
		if v == "" {
			continue
		}
		if f.k == "status" {
			v = ui.StatusBadge(v)
		}
		fmt.Printf("  %-12s: %s\n", f.label, v)
	}
	// Last agent note.
	if notes, ok := task["agent_log"].([]any); ok && len(notes) > 0 {
		last := notes[len(notes)-1]
		if nm, ok := last.(map[string]any); ok {
			fmt.Printf("  %-12s: %s\n", "Agent Note", stringVal(nm, "note", fmt.Sprintf("%v", last)))
		}
	}
	fmt.Println()
	return nil
}

func showTaskHistory(taskID string) error {
	client := ui.NewFlumeClient()
	data, err := client.Get("/api/tasks/" + taskID + "/history")
	if err != nil {
		return fmt.Errorf("failed to fetch history: %w", err)
	}

	if tasksJSON {
		b, _ := json.MarshalIndent(data, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen(fmt.Sprintf("  HISTORY: %s  ", taskID)))
	entries, _ := data["history"].([]any)
	if len(entries) == 0 {
		entries, _ = data["agent_log"].([]any)
	}
	for _, e := range entries {
		em, ok := e.(map[string]any)
		if !ok {
			continue
		}
		ts := truncate(stringVal(em, "timestamp", stringVal(em, "time", "?")), 19)
		role := fmt.Sprintf("%-14s", stringVal(em, "role", stringVal(em, "agent", "?")))
		note := stringVal(em, "note", stringVal(em, "message", fmt.Sprintf("%v", e)))
		fmt.Printf("  [%s] %s → %s\n", ts, ui.StatusBadge(role), note)
	}
	return nil
}

func showTaskDiff(taskID string) error {
	client := ui.NewFlumeClient()
	raw, err := client.GetRaw("/api/tasks/" + taskID + "/diff")
	if err != nil {
		return fmt.Errorf("failed to fetch diff: %w", err)
	}

	for _, line := range strings.Split(string(raw), "\n") {
		switch {
		case strings.HasPrefix(line, "+++") || strings.HasPrefix(line, "---"):
			fmt.Println(ui.WarningGold(line))
		case strings.HasPrefix(line, "+"):
			fmt.Println(ui.SuccessBlue(line))
		case strings.HasPrefix(line, "-"):
			fmt.Println(ui.ErrorRed(line))
		case strings.HasPrefix(line, "@@"):
			fmt.Println(ui.WarningGold(line))
		default:
			fmt.Println(line)
		}
	}
	return nil
}

func transitionTask(taskID, state string) error {
	validStates := map[string]bool{"ready": true, "in_progress": true, "blocked": true, "done": true, "planned": true}
	if !validStates[strings.ToLower(state)] {
		return fmt.Errorf("invalid state '%s'. Valid: ready, in_progress, blocked, done, planned", state)
	}
	client := ui.NewFlumeClient()
	result, err := client.Post("/api/tasks/"+taskID+"/transition", map[string]string{"status": state})
	if err != nil {
		return fmt.Errorf("transition failed: %w", err)
	}
	log.Debug("Transition result", "result", result)
	fmt.Println(ui.SuccessBlue(fmt.Sprintf("Task '%s' transitioned to '%s'.", taskID, state)))
	return nil
}

func extractTasks(state map[string]any) []map[string]any {
	var out []map[string]any
	if state == nil {
		return out
	}
	if arr, ok := state["tasks"].([]any); ok {
		for _, t := range arr {
			if m, ok := t.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}



func init() {
	TasksCmd.Flags().StringVarP(&tasksStatusFilter, "status", "s", "", "Filter by status (ready, in_progress, blocked, done, planned)")
	TasksCmd.Flags().StringVarP(&tasksProjectFilter, "project", "p", "", "Filter by project ID")
	TasksCmd.Flags().BoolVarP(&tasksJSON, "json", "j", false, "Output raw JSON")

	TasksCmd.AddCommand(tasksHistoryCmd)
	TasksCmd.AddCommand(tasksDiffCmd)
	TasksCmd.AddCommand(tasksTransitionCmd)
	TasksCmd.AddCommand(tasksStopAllCmd)
}
