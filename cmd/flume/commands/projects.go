package commands

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/charmbracelet/log"
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

// safeAPIPath builds a URL path safely using url.JoinPath (Go 1.19+) to
// prevent path traversal if projectID contains sequences like "../".
func safeAPIPath(base string, segments ...string) (string, error) {
	p, err := url.JoinPath(base, segments...)
	if err != nil {
		return "", fmt.Errorf("invalid path segments %v: %w", segments, err)
	}
	return p, nil
}

func listProjects() error {
	client := ui.NewFlumeClient()
	state, err := client.Get("/api/system-state")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	projects := extractProjects(state)

	if projectsJSON {
		b, err := json.MarshalIndent(projects, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to serialize projects as JSON: %w", err)
		}
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
		id := stringValFromKeys(p, "—", "id", "_id")
		name := truncate(stringValFromKeys(p, "—", "name"), 30)
		repo := truncate(stringValFromKeys(p, "—", "repo_url", "repoUrl"), 45)
		active := fmt.Sprintf("%d", tasksByProject[id]["active"])
		done := fmt.Sprintf("%d", tasksByProject[id]["done"])
		rows = append(rows, []string{id, name, repo, active, done})
	}

	fmt.Println(ui.NeonGreen(fmt.Sprintf("  PROJECTS (%d)  ", len(rows))))
	fmt.Print(ui.RenderTable(headers, rows))
	return nil
}

func showProject(projectID string) error {
	// Validate before embedding in any API path — blocks path traversal.
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	client := ui.NewFlumeClient()
	state, err := client.Get("/api/system-state")
	if err != nil {
		return fmt.Errorf("dashboard unreachable: %w", err)
	}

	projects := extractProjects(state)
	var project map[string]any
	for _, p := range projects {
		if stringValFromKeys(p, "", "id", "_id") == projectID {
			project = p
			break
		}
	}
	if project == nil {
		return fmt.Errorf("project '%s' not found", sanitizeForTerminal(projectID))
	}

	// Safe path construction — prevents traversal via malformed projectID.
	clonePath, err := safeAPIPath("/api/projects", projectID, "clone-status")
	if err != nil {
		return err
	}
	cloneStatus, cloneErr := client.Get(clonePath)
	if cloneErr != nil {
		log.Warn("Could not fetch clone status", "project", projectID, "error", cloneErr)
	}

	if projectsJSON {
		out := map[string]any{"project": project, "clone_status": cloneStatus}
		b, err := json.MarshalIndent(out, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to serialize project as JSON: %w", err)
		}
		fmt.Println(string(b))
		return nil
	}

	fmt.Println(ui.NeonGreen(fmt.Sprintf("  PROJECT: %s  ", sanitizeForTerminal(projectID))))
	// Each entry lists the snake_case key first (standard API), then the
	// explicit camelCase alias. strings.ReplaceAll is intentionally avoided
	// because it produces incorrect results (e.g. repo_url → repourl, not repoUrl).
	fields := []struct {
		label string
		keys  []string
	}{
		{"Name", []string{"name"}},
		{"Repo URL", []string{"repo_url", "repoUrl"}},
		{"Branch", []string{"default_branch", "defaultBranch", "branch"}},
		{"Created", []string{"created_at", "createdAt"}},
		{"Updated", []string{"updated_at", "updatedAt"}},
	}
	for _, f := range fields {
		if v := stringValFromKeys(project, "", f.keys...); v != "" {
			fmt.Printf("  %-16s: %s\n", f.label, sanitizeForTerminal(v))
		}
	}

	cloneReady := "unknown"
	if cloneErr != nil {
		cloneReady = "api-error"
	} else if cloneStatus != nil {
		cloneReady = stringValFromKeys(cloneStatus, "unknown", "status")
	}
	fmt.Printf("  %-16s: %s\n", "Clone", ui.StatusBadge(cloneReady))
	fmt.Println()
	return nil
}

func showProjectStatus(projectID string) error {
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	client := ui.NewFlumeClient()
	apiPath, err := safeAPIPath("/api/projects", projectID, "clone-status")
	if err != nil {
		return err
	}
	data, err := client.Get(apiPath)
	if err != nil {
		return fmt.Errorf("failed to fetch project status: %w", err)
	}
	if projectsJSON {
		b, err := json.MarshalIndent(data, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to serialize status as JSON: %w", err)
		}
		fmt.Println(string(b))
		return nil
	}
	fmt.Println(ui.NeonGreen(fmt.Sprintf("  PROJECT STATUS: %s  ", sanitizeForTerminal(projectID))))
	for k, v := range data {
		fmt.Printf("  %-20s: %v\n", sanitizeForTerminal(k), sanitizeForTerminal(fmt.Sprintf("%v", v)))
	}
	return nil
}

func showProjectBranches(projectID string) error {
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	client := ui.NewFlumeClient()
	apiPath, err := safeAPIPath("/api/repos", projectID, "branches")
	if err != nil {
		return err
	}
	data, err := client.Get(apiPath)
	if err != nil {
		return fmt.Errorf("failed to fetch branches: %w", err)
	}
	if projectsJSON {
		b, err := json.MarshalIndent(data, "", "  ")
		if err != nil {
			return fmt.Errorf("failed to serialize branches as JSON: %w", err)
		}
		fmt.Println(string(b))
		return nil
	}
	fmt.Println(ui.NeonGreen(fmt.Sprintf("  BRANCHES: %s  ", sanitizeForTerminal(projectID))))

	// Explicit ok-check: distinguishes empty list from malformed API response.
	branches, ok := data["branches"].([]any)
	if !ok {
		log.Warn("Unexpected branch response format from dashboard", "project", projectID)
		fmt.Println(ui.WarningGold("  Could not parse branch list from dashboard response."))
		return nil
	}
	for _, b := range branches {
		switch bv := b.(type) {
		case string:
			fmt.Printf("  %s %s\n", ui.SuccessBlue("◆"), sanitizeForTerminal(bv))
		case map[string]any:
			name := stringValFromKeys(bv, fmt.Sprintf("%v", bv), "name")
			fmt.Printf("  %s %s\n", ui.SuccessBlue("◆"), sanitizeForTerminal(name))
		}
	}
	if len(branches) == 0 {
		fmt.Println(ui.WarningGold("No branches found."))
	}
	return nil
}

func deleteProject(projectID string) error {
	// Validate before displaying or embedding in API path.
	if err := validateProjectID(projectID); err != nil {
		return err
	}
	fmt.Println()
	fmt.Println(ui.ErrorRed(fmt.Sprintf(
		"⚠  This will permanently delete project '%s' and all associated data.",
		sanitizeForTerminal(projectID),
	)))
	fmt.Print(ui.WarningGold(fmt.Sprintf("Type the project ID to confirm (%s): ", projectID)))

	reader := bufio.NewReader(os.Stdin)
	input, err := reader.ReadString('\n')
	if err != nil {
		return fmt.Errorf("failed to read confirmation input: %w", err)
	}
	if strings.TrimSpace(input) != projectID {
		fmt.Println(ui.WarningGold("Deletion cancelled — project ID did not match."))
		return nil
	}

	apiPath, err := safeAPIPath("/api/projects", projectID, "delete")
	if err != nil {
		return err
	}
	client := ui.NewFlumeClient()
	if _, err := client.Post(apiPath, nil); err != nil {
		return fmt.Errorf("deletion failed: %w", err)
	}
	fmt.Println(ui.SuccessBlue(fmt.Sprintf("Project '%s' deleted.", sanitizeForTerminal(projectID))))
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
