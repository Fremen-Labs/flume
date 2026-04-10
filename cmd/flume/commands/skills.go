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
	Short: "Manage Inception Skills — list, compile, and reload Markdown-driven handlers",
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

func init() {
	skillsCompileCmd.Flags().Bool("all", false, "Compile all inception:full skills")
	SkillsCmd.AddCommand(skillsListCmd)
	SkillsCmd.AddCommand(skillsCompileCmd)
	SkillsCmd.AddCommand(skillsReloadCmd)
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
