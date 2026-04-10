package commands

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/Fremen-Labs/flume/src/gateway/skills"
	"github.com/Fremen-Labs/flume/src/gateway/skillslog"
	"github.com/spf13/cobra"
)

// ─────────────────────────────────────────────────────────────────────────────
// CLI Subcommands for Skill Management
//
//   flume skills list                    — List all discovered skills
//   flume skills compile <name>          — Compile one inception:full skill to Go
//   flume skills compile --all           — Compile all inception:full skills
//   flume skills reload                  — Hot-reload the running gateway
//
// Logging follows the unified gateway pattern: slog + secret scrubbing.
// ─────────────────────────────────────────────────────────────────────────────

var SkillsCmd = &cobra.Command{
	Use:   "skills",
	Short: "Manage Inception Skills — list, compile, validate, and reload Markdown-driven handlers",
}

var skillsListCmd = &cobra.Command{
	Use:   "list",
	Short: "Discover and display all .skill.md files",
	Run:   runSkillsList,
}

var skillsCompileCmd = &cobra.Command{
	Use:   "compile [skill-name]",
	Short: "Compile an inception:full skill to standalone Go source",
	Args:  cobra.MaximumNArgs(1),
	Run:   runSkillsCompile,
}

var skillsReloadCmd = &cobra.Command{
	Use:   "reload",
	Short: "Hot-reload the running gateway's skill registry",
	Run:   runSkillsReload,
}

var skillsValidateCmd = &cobra.Command{
	Use:   "validate [skill-name]",
	Short: "Validate skill definitions against schemas and Meta-Critic rules",
	Args:  cobra.MaximumNArgs(1),
	Run:   runSkillsValidate,
}

func init() {
	skillsCompileCmd.Flags().Bool("all", false, "Compile all inception:full skills")
	skillsValidateCmd.Flags().Bool("all", false, "Validate all discovered skills")
	skillsValidateCmd.Flags().Bool("json", false, "Output validation report as JSON")
	SkillsCmd.AddCommand(skillsListCmd)
	SkillsCmd.AddCommand(skillsCompileCmd)
	SkillsCmd.AddCommand(skillsReloadCmd)
	SkillsCmd.AddCommand(skillsValidateCmd)
}

func runSkillsList(cmd *cobra.Command, args []string) {
	log := skillslog.Log().With(slog.String("component", "cli_skills"))
	ctx := context.Background()

	files, err := skills.DiscoverSkillFiles(ctx)
	if err != nil {
		fmt.Println(ui.ErrorRed(fmt.Sprintf("Skill discovery failed: %v", err)))
		os.Exit(1)
	}

	if len(files) == 0 {
		fmt.Println(ui.NeonGreen("No .skill.md files found."))
		fmt.Println("Create skills in ./skills/ or set FLUME_SKILLS_DIR.")
		return
	}

	// Parse and display
	fmt.Println()
	fmt.Println(ui.NeonGreen("⚡ Flume Inception Skills"))
	fmt.Println(strings.Repeat("─", 70))
	fmt.Printf("  %-25s %-10s %-12s %-8s %s\n", "NAME", "VERSION", "INCEPTION", "ENSEMBLE", "PATH")
	fmt.Println(strings.Repeat("─", 70))

	var parsed int
	for _, path := range files {
		def, err := skills.ParseSkillFile(path)
		if err != nil {
			log.Warn("skipping invalid skill", slog.String("path", path), slog.String("error", err.Error()))
			fmt.Printf("  %-25s %s\n", "⚠ PARSE ERROR", err.Error())
			continue
		}
		parsed++
		fmt.Printf("  %-25s %-10s %-12s %-8d %s\n",
			def.Name,
			def.Version,
			string(def.Inception),
			def.EnsembleSize,
			def.SourcePath,
		)
	}

	fmt.Println(strings.Repeat("─", 70))
	fmt.Printf("  Total: %d skills discovered, %d parsed successfully\n\n", len(files), parsed)
}

func runSkillsCompile(cmd *cobra.Command, args []string) {
	log := skillslog.Log().With(slog.String("component", "cli_skills_compile"))
	ctx := context.Background()
	compileAll, _ := cmd.Flags().GetBool("all")

	files, err := skills.DiscoverSkillFiles(ctx)
	if err != nil {
		fmt.Println(ui.ErrorRed(fmt.Sprintf("Skill discovery failed: %v", err)))
		os.Exit(1)
	}

	// Determine the output directory (skills/ relative to CWD)
	outputDir, _ := os.Getwd()
	outputDir = outputDir + "/skills"

	var compiled int
	for _, path := range files {
		def, err := skills.ParseSkillFile(path)
		if err != nil {
			log.Warn("skipping invalid skill", slog.String("path", path))
			continue
		}

		// Filter: either compile a specific skill or all full-inception skills
		if !compileAll {
			if len(args) == 0 {
				fmt.Println(ui.ErrorRed("Specify a skill name or use --all"))
				os.Exit(1)
			}
			if def.Name != args[0] {
				continue
			}
		}

		if def.Inception != skills.InceptionFull {
			if !compileAll {
				fmt.Println(ui.ErrorRed(fmt.Sprintf("Skill %q has inception=%s, only 'full' can be compiled", def.Name, def.Inception)))
				os.Exit(1)
			}
			continue
		}

		outPath, err := skills.CompileToFile(def, outputDir)
		if err != nil {
			fmt.Println(ui.ErrorRed(fmt.Sprintf("Compile failed for %s: %v", def.Name, err)))
			continue
		}

		compiled++
		fmt.Println(ui.NeonGreen(fmt.Sprintf("✓ Compiled %s → %s", def.Name, outPath)))
	}

	if compiled == 0 {
		fmt.Println("No inception:full skills found to compile.")
	} else {
		fmt.Printf("\n%s %d skill(s) compiled successfully.\n", ui.NeonGreen("⚡"), compiled)
	}
}

func runSkillsReload(cmd *cobra.Command, args []string) {
	log := skillslog.Log().With(slog.String("component", "cli_skills_reload"))

	gwURL := os.Getenv("GATEWAY_URL")
	if gwURL == "" {
		gwURL = "http://localhost:8090"
	}

	url := strings.TrimRight(gwURL, "/") + "/skills/reload"
	log.Info("sending skill reload request", slog.String("url", url))

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Post(url, "application/json", nil)
	if err != nil {
		fmt.Println(ui.ErrorRed(fmt.Sprintf("Failed to reach gateway: %v", err)))
		os.Exit(1)
	}
	defer resp.Body.Close()

	var result map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result)

	if resp.StatusCode == http.StatusOK {
		skillCount := 0
		if sc, ok := result["skills"].(float64); ok {
			skillCount = int(sc)
		}
		fmt.Println(ui.NeonGreen(fmt.Sprintf("✓ Gateway skill registry reloaded (%d skills)", skillCount)))
	} else {
		errMsg := "unknown"
		if e, ok := result["error"].(string); ok {
			errMsg = e
		}
		fmt.Println(ui.ErrorRed(fmt.Sprintf("Reload failed: %s", errMsg)))
		os.Exit(1)
	}
}

func runSkillsValidate(cmd *cobra.Command, args []string) {
	log := skillslog.Log().With(slog.String("component", "cli_skills_validate"))
	ctx := context.Background()
	validateAll, _ := cmd.Flags().GetBool("all")
	jsonOutput, _ := cmd.Flags().GetBool("json")

	files, err := skills.DiscoverSkillFiles(ctx)
	if err != nil {
		fmt.Println(ui.ErrorRed(fmt.Sprintf("Skill discovery failed: %v", err)))
		os.Exit(1)
	}

	if len(files) == 0 {
		fmt.Println(ui.NeonGreen("No .skill.md files found."))
		return
	}

	// If no --all and no name, default to all
	if !validateAll && len(args) == 0 {
		validateAll = true
	}

	var allReports []*skills.ValidationReport
	var totalErrors, totalWarnings int

	for _, path := range files {
		def, err := skills.ParseSkillFile(path)
		if err != nil {
			log.Warn("skipping unparseable skill", slog.String("path", path), slog.String("error", err.Error()))
			if !jsonOutput {
				fmt.Printf("\n%s %s\n  Parse error: %s\n", ui.ErrorRed("✗"), path, err.Error())
			}
			totalErrors++
			continue
		}

		// Filter by name if specified
		if !validateAll && def.Name != args[0] {
			continue
		}

		report := skills.ValidateSkill(def)
		allReports = append(allReports, report)
		totalErrors += report.Errors
		totalWarnings += report.Warnings

		if !jsonOutput {
			printValidationReport(report)
		}
	}

	if jsonOutput {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		enc.Encode(map[string]interface{}{
			"reports":        allReports,
			"total_skills":   len(allReports),
			"total_errors":   totalErrors,
			"total_warnings": totalWarnings,
		})
	} else {
		fmt.Println(strings.Repeat("═", 70))
		if totalErrors > 0 {
			fmt.Println(ui.ErrorRed(fmt.Sprintf("✗ Validation failed: %d error(s), %d warning(s) across %d skill(s)",
				totalErrors, totalWarnings, len(allReports))))
			os.Exit(1)
		} else if totalWarnings > 0 {
			fmt.Printf("%s Validation passed with %d warning(s) across %d skill(s)\n",
				ui.NeonGreen("⚠"), totalWarnings, len(allReports))
		} else {
			fmt.Println(ui.NeonGreen(fmt.Sprintf("✓ All %d skill(s) passed validation", len(allReports))))
		}
	}
}

func printValidationReport(report *skills.ValidationReport) {
	statusIcon := ui.NeonGreen("✓")
	if !report.Valid {
		statusIcon = ui.ErrorRed("✗")
	} else if report.Warnings > 0 {
		statusIcon = "⚠"
	}

	fmt.Printf("\n%s %s (v%s)\n", statusIcon, report.SkillName,
		func() string {
			// Extract version from skill name context
			if report.SkillPath != "" {
				return report.SkillPath
			}
			return "unknown"
		}())
	fmt.Println(strings.Repeat("─", 70))

	for _, f := range report.Findings {
		severityTag := ""
		switch f.Severity {
		case skills.SeverityError:
			severityTag = ui.ErrorRed("ERROR")
		case skills.SeverityWarning:
			severityTag = "WARN "
		case skills.SeverityInfo:
			severityTag = "INFO "
		}
		fmt.Printf("  [%s] [%s] %s\n", severityTag, f.Rule, f.Message)
	}

	if len(report.Findings) == 0 {
		fmt.Println("  No issues found.")
	}
}
