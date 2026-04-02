package commands

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

var projectsJSON bool

var ProjectsCmd = &cobra.Command{
	Use:   "projects",
	Short: "List and manage Flume projects",
	Long:  `View all projects, inspect clone status, list branches, or delete a project.`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if len(args) > 0 {
			return showProject(args[0])
		}
		return listProjects()
	},
}

var projectsStatusCmd = &cobra.Command{
	Use:   "status <project_id>",
	Short: "Show clone status and task summary for a project",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		return showProjectStatus(args[0])
	},
}

var projectsBranchesCmd = &cobra.Command{
	Use:   "branches <project_id>",
	Short: "List all branches for a project",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		return showProjectBranches(args[0])
	},
}

var projectsDeleteCmd = &cobra.Command{
	Use:   "delete <project_id>",
	Short: "Delete a project and all associated data (requires confirmation)",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		return deleteProject(args[0])
	},
}

func listProjects() error {
	client := ui.NewFlumeClient()
	state, err := client.Get("/api/system-state")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	projects := extractProjects(state)

	if projectsJSON {
		b, _ := json.MarshalIndent(projects, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	if len(projects) == 0 {
		fmt.Println(ui.WarningGold("No projects found. Create one via the Flume dashboard."))
		return nil
	}

	// Count tasks per project.
	tasksByProject := map[string]map[string]int{}
	if tasks, ok := state["tasks"].([]any); ok {
		for _, t := range tasks {
			tm, ok := t.(map[string]any)
			if !ok {
				continue
			}
			proj := stringVal(tm, "project_id", "")
			if proj == "" {
				continue
			}
			if _, exists := tasksByProject[proj]; !exists {
				tasksByProject[proj] = map[string]int{"active": 0, "done": 0}
			}
			status := strings.ToLower(stringVal(tm, "status", ""))
			if status == "done" || status == "merged" || status == "complete" {
				tasksByProject[proj]["done"]++
			} else if status != "" {
				tasksByProject[proj]["active"]++
			}
		}
	}

	headers := []string{"Project ID", "Name", "Repo URL", "Active Tasks", "Done Tasks"}
	var rows [][]string
	for _, p := range projects {
		id := stringVal(p, "id", stringVal(p, "_id", "—"))
		name := truncate(stringVal(p, "name", "—"), 30)
		repo := truncate(stringVal(p, "repo_url", stringVal(p, "repoUrl", "—")), 45)
		active := fmt.Sprintf("%d", tasksByProject[id]["active"])
		done := fmt.Sprintf("%d", tasksByProject[id]["done"])
		rows = append(rows, []string{id, name, repo, active, done})
	}

	fmt.Println(ui.NeonGreen(fmt.Sprintf("  PROJECTS (%d)  ", len(rows))))
	fmt.Print(ui.RenderTable(headers, rows))
	return nil
}

func showProject(projectID string) error {
	client := ui.NewFlumeClient()
	state, err := client.Get("/api/system-state")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	projects := extractProjects(state)
	var project map[string]any
	for _, p := range projects {
		if stringVal(p, "id", stringVal(p, "_id", "")) == projectID {
			project = p
			break
		}
	}
	if project == nil {
		return fmt.Errorf("project '%s' not found", projectID)
	}

	cloneStatus, _ := client.Get("/api/projects/" + projectID + "/clone-status")

	if projectsJSON {
		out := map[string]any{"project": project, "clone_status": cloneStatus}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen(fmt.Sprintf("  PROJECT: %s  ", projectID)))
	fields := []struct{ k, l string }{
		{"name", "Name"}, {"repo_url", "Repo URL"}, {"default_branch", "Branch"},
		{"created_at", "Created"}, {"updated_at", "Updated"},
	}
	for _, f := range fields {
		if v := stringVal(project, f.k, stringVal(project, strings.ReplaceAll(f.k, "_", ""), "")); v != "" {
			fmt.Printf("  %-16s: %s\n", f.l, v)
		}
	}
	cloneReady := "Unknown"
	if cloneStatus != nil {
		cloneReady = stringVal(cloneStatus, "status", "—")
	}
	fmt.Printf("  %-16s: %s\n", "Clone", ui.StatusBadge(cloneReady))
	fmt.Println()
	return nil
}

func showProjectStatus(projectID string) error {
	client := ui.NewFlumeClient()
	data, err := client.Get("/api/projects/" + projectID + "/clone-status")
	if err != nil {
		return fmt.Errorf("failed to fetch project status: %w", err)
	}
	if projectsJSON {
		b, _ := json.MarshalIndent(data, "", "  ")
		fmt.Println(string(b))
		return nil
	}
	fmt.Println(ui.NeonGreen(fmt.Sprintf("  PROJECT STATUS: %s  ", projectID)))
	for k, v := range data {
		fmt.Printf("  %-20s: %v\n", k, v)
	}
	return nil
}

func showProjectBranches(projectID string) error {
	client := ui.NewFlumeClient()
	data, err := client.Get("/api/repos/" + projectID + "/branches")
	if err != nil {
		return fmt.Errorf("failed to fetch branches: %w", err)
	}
	if projectsJSON {
		b, _ := json.MarshalIndent(data, "", "  ")
		fmt.Println(string(b))
		return nil
	}
	fmt.Println(ui.NeonGreen(fmt.Sprintf("  BRANCHES: %s  ", projectID)))
	branches, _ := data["branches"].([]any)
	for _, b := range branches {
		switch bv := b.(type) {
		case string:
			fmt.Printf("  %s %s\n", ui.SuccessBlue("◆"), bv)
		case map[string]any:
			name := stringVal(bv, "name", fmt.Sprintf("%v", bv))
			fmt.Printf("  %s %s\n", ui.SuccessBlue("◆"), name)
		}
	}
	if len(branches) == 0 {
		fmt.Println(ui.WarningGold("No branches found."))
	}
	return nil
}

func deleteProject(projectID string) error {
	// Require typing the project name to confirm.
	fmt.Println()
	fmt.Println(ui.ErrorRed(fmt.Sprintf("⚠  This will permanently delete project '%s' and all associated data.", projectID)))
	fmt.Print(ui.WarningGold(fmt.Sprintf("Type the project ID to confirm (%s): ", projectID)))
	reader := bufio.NewReader(os.Stdin)
	input, _ := reader.ReadString('\n')
	if strings.TrimSpace(input) != projectID {
		fmt.Println(ui.WarningGold("Deletion cancelled — project ID did not match."))
		return nil
	}
	client := ui.NewFlumeClient()
	result, err := client.Post("/api/projects/"+projectID+"/delete", nil)
	if err != nil {
		return fmt.Errorf("deletion failed: %w", err)
	}
	_ = result
	fmt.Println(ui.SuccessBlue(fmt.Sprintf("Project '%s' deleted.", projectID)))
	return nil
}

func extractProjects(state map[string]any) []map[string]any {
	var out []map[string]any
	if state == nil {
		return out
	}
	if arr, ok := state["projects"].([]any); ok {
		for _, p := range arr {
			if m, ok := p.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}

func init() {
	ProjectsCmd.Flags().BoolVarP(&projectsJSON, "json", "j", false, "Output raw JSON")
	ProjectsCmd.AddCommand(projectsStatusCmd)
	ProjectsCmd.AddCommand(projectsBranchesCmd)
	ProjectsCmd.AddCommand(projectsDeleteCmd)
}
