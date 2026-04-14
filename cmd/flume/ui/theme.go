package ui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

var (
	// Neon 90s Matrix Colors
	colorNeonGreen   = lipgloss.Color("#39FF14")
	colorCyberPurple = lipgloss.Color("#B026FF")
	colorTerminalRed = lipgloss.Color("#FF003C")
	colorSciFiBlue   = lipgloss.Color("#00F0FF")
	colorGhostWhite  = lipgloss.Color("#E0E0E0")
	colorHackerGold  = lipgloss.Color("#FFD700")

	// Core Render Engines
	styleHeader = lipgloss.NewStyle().
			Bold(true).
			Foreground(colorNeonGreen).
			BorderStyle(lipgloss.DoubleBorder()).
			BorderForeground(colorCyberPurple).
			Padding(1, 2)

	styleSuccess  = lipgloss.NewStyle().Foreground(colorSciFiBlue).Bold(true)
	styleWarning  = lipgloss.NewStyle().Foreground(colorHackerGold).Bold(true)
	styleError    = lipgloss.NewStyle().Foreground(colorTerminalRed).Bold(true).Blink(true)
	styleGradient = lipgloss.NewStyle().Foreground(colorNeonGreen).Background(lipgloss.Color("#000000"))

	styleLogo = lipgloss.NewStyle().
			Foreground(colorNeonGreen).
			Bold(true).
			Margin(1, 0, 1, 0)
)

// SciFiLogo returns the Flume Logo rendered with 90s Cyberpunk styling
func SciFiLogo() string {
	logo := `
  ███████╗██╗     ██╗   ██╗███╗   ████╗███████╗
  ██╔════╝██║     ██║   ██║████╗ ████║██╔════╝
  █████╗  ██║     ██║   ██║██╔████╔██║█████╗  
  ██╔══╝  ██║     ██║   ██║██║╚██╔╝██║██╔══╝  
  ██║     ███████╗╚██████╔╝██║ ╚═╝ ██║███████╗
  ╚═╝     ╚══════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝
      :: AUTONOMOUS ENGINEERING FRONTIER :: `
	return styleLogo.Render(logo)
}

// CyberGradient returns string rendered with the SciFi gradient
func CyberGradient(str string) string {
	return styleGradient.Render(str)
}

func NeonGreen(str string) string {
	return styleHeader.Render(str)
}

func SuccessBlue(str string) string {
	return styleSuccess.Render("✔ " + str)
}

func WarningGold(str string) string {
	return styleWarning.Render("⚠ " + str)
}

func ErrorRed(str string) string {
	return styleError.Render("✖ " + str)
}

// StatusBadge returns a lipgloss-coloured badge for a task/worker/infra status string.
func StatusBadge(status string) string {
	s := strings.ToLower(status)
	switch {
	case s == "active" || s == "running" || s == "healthy" || s == "in_progress":
		return lipgloss.NewStyle().Foreground(colorNeonGreen).Bold(true).Render("● " + status)
	case s == "done" || s == "complete" || s == "merged":
		return lipgloss.NewStyle().Foreground(colorSciFiBlue).Bold(true).Render("✔ " + status)
	case s == "blocked" || s == "error" || s == "failed":
		return lipgloss.NewStyle().Foreground(colorTerminalRed).Bold(true).Render("✖ " + status)
	case s == "planned" || s == "ready":
		return lipgloss.NewStyle().Foreground(colorHackerGold).Bold(true).Render("◆ " + status)
	default:
		return lipgloss.NewStyle().Foreground(colorGhostWhite).Render("· " + status)
	}
}

// RenderTable renders a header row + data rows as a padded plain-text table.
func RenderTable(headers []string, rows [][]string) string {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = len(h)
	}
	for _, row := range rows {
		for i := range headers {
			if i < len(row) && len(row[i]) > widths[i] {
				widths[i] = len(row[i])
			}
		}
	}

	headerStyle := lipgloss.NewStyle().Foreground(colorHackerGold).Bold(true)
	cellStyle := lipgloss.NewStyle().Foreground(colorGhostWhite)
	sepStyle := lipgloss.NewStyle().Foreground(colorCyberPurple)

	var sb strings.Builder
	sep := "  "
	for _, w := range widths {
		sep += strings.Repeat("─", w+2) + "┬"
	}
	sep = strings.TrimRight(sep, "┬")
	sb.WriteString(sepStyle.Render(sep) + "\n")
	sb.WriteString("  ")
	for i, h := range headers {
		sb.WriteString(headerStyle.Width(widths[i] + 2).Render(h))
	}
	sb.WriteString("\n" + sepStyle.Render(sep) + "\n")
	for _, row := range rows {
		sb.WriteString("  ")
		for i := range headers {
			cell := ""
			if i < len(row) {
				cell = row[i]
			}
			sb.WriteString(cellStyle.Width(widths[i] + 2).Render(cell))
		}
		sb.WriteString("\n")
	}
	sb.WriteString(sepStyle.Render(sep) + "\n")
	return sb.String()
}

// ─────────────────────────────────────────────────────────────────────────────
// DeploymentSummary holds the non-secret facts the CLI knows after a successful
// flume start. Secrets (API keys, tokens) are deliberately excluded.
// ─────────────────────────────────────────────────────────────────────────────

// DeploymentSummary is the input to PrintDeploymentSummary.
type DeploymentSummary struct {
	// Deployment mode
	NativeMode bool

	// LLM configuration
	Provider   string
	Model      string
	OllamaHost string // only set for ollama/exo

	// Infrastructure ports
	DashboardPort   string
	ElasticPort     string
	VaultPort       string
	ExternalElastic bool
	ElasticURL      string

	// Source control
	RepoType string // "github", "ado", or ""
	ADOOrg   string

	// Workers
	WorkerCount int

	// Secret presence flags — no secret value is ever stored here
	HasAPIKey      bool
	HasGithubToken bool
	HasADOToken    bool
	VaultDeployed  bool
}

// PrintDeploymentSummary renders a beautiful, secrets-free summary panel to
// stdout using lipgloss. Call this at the end of a successful `flume start`.
func PrintDeploymentSummary(s DeploymentSummary) {
	dimStyle    := lipgloss.NewStyle().Foreground(lipgloss.Color("#6C7A89"))
	labelStyle  := lipgloss.NewStyle().Foreground(colorHackerGold).Bold(true).Width(26)
	valueStyle  := lipgloss.NewStyle().Foreground(colorGhostWhite)
	secureStyle := lipgloss.NewStyle().Foreground(colorNeonGreen).Bold(true)
	sectionStyle := lipgloss.NewStyle().Foreground(colorCyberPurple).Bold(true)

	divider := dimStyle.Render(strings.Repeat("─", 60))

	row := func(label, value string) string {
		return "  " + labelStyle.Render(label) + valueStyle.Render(value)
	}
	secureRow := func(label string) string {
		return "  " + labelStyle.Render(label) + secureStyle.Render("•••••••• [stored in OpenBao KMS]")
	}
	checkMark := func(b bool) string {
		if b {
			return secureStyle.Render("✔  configured")
		}
		return dimStyle.Render("–  not configured")
	}

	var lines []string
	add := func(l string) { lines = append(lines, l) }

	add("")
	add(SciFiLogo())
	add(divider)

	// ── Security / OpenBao header ─────────────────────────────────────────────
	if s.VaultDeployed {
		vaultHeader := lipgloss.NewStyle().
			Foreground(colorNeonGreen).
			Bold(true).
			BorderStyle(lipgloss.RoundedBorder()).
			BorderForeground(colorNeonGreen).
			Padding(0, 2).
			Render("🔐  OPENBAO NATIVE KMS DEPLOYED SUCCESSFULLY  🔐")
		add("")
		add("  " + vaultHeader)
		add("")
	}
	add(divider)

	// ── Deployment mode ───────────────────────────────────────────────────────
	add(sectionStyle.Render("  DEPLOYMENT"))
	add(divider)
	mode := "Docker Compose (containerised)"
	if s.NativeMode {
		mode = "Native OS (high-performance worktrees)"
	}
	add(row("Mode", mode))

	// ── LLM ──────────────────────────────────────────────────────────────────
	add("")
	add(sectionStyle.Render("  LLM PROVIDER"))
	add(divider)
	add(row("Provider", s.Provider))
	if s.Model != "" {
		add(row("Model Constraint", s.Model))
	} else {
		add(row("Model Constraint", dimStyle.Render("auto (provider default)")))
	}
	if s.OllamaHost != "" {
		add(row("Ollama Host", s.OllamaHost))
	}
	if s.HasAPIKey {
		add(secureRow("API Secret"))
	}

	// ── Infrastructure ────────────────────────────────────────────────────────
	add("")
	add(sectionStyle.Render("  INFRASTRUCTURE"))
	add(divider)
	add(row("Dashboard URL", "http://localhost:"+s.DashboardPort))
	add(row("Elasticsearch", "http://localhost:"+s.ElasticPort))
	add(row("OpenBao KMS", "http://localhost:"+s.VaultPort))
	if s.ExternalElastic && s.ElasticURL != "" {
		add(row("External ES URL", s.ElasticURL))
	}

	// ── Workers ───────────────────────────────────────────────────────────────
	add("")
	add(sectionStyle.Render("  WORKERS"))
	add(divider)
	workerDots := strings.Repeat("◆ ", s.WorkerCount)
	add(row("Agent Workers", secureStyle.Render(workerDots)+valueStyle.Render(strings.TrimSpace("×"+strings.Repeat(" 1", s.WorkerCount)))))
	add(row("Worker Count", valueStyle.Render(strings.Repeat("1", 0)+fmt.Sprintf("%d workers active", s.WorkerCount))))

	// ── Source Control ────────────────────────────────────────────────────────
	add("")
	add(sectionStyle.Render("  SOURCE CONTROL"))
	add(divider)
	switch s.RepoType {
	case "github":
		add(row("Provider", "GitHub"))
		add(row("Token", checkMark(s.HasGithubToken)))
	case "ado":
		add(row("Provider", "Azure DevOps"))
		add(row("Organization", s.ADOOrg))
		add(row("PAT", checkMark(s.HasADOToken)))
	default:
		add("  " + dimStyle.Render("Not configured — add credentials via Dashboard → Settings → Repositories"))
	}

	// ── Security posture ──────────────────────────────────────────────────────
	add("")
	add(sectionStyle.Render("  SECURITY POSTURE"))
	add(divider)
	add("  " + secureStyle.Render("✔") + "  Secrets stored exclusively in OpenBao KV-V2 — never written to disk.")
	add("  " + secureStyle.Render("✔") + "  Elasticsearch API key scoped to Flume role (least-privilege).")
	add("  " + secureStyle.Render("✔") + "  Admin token generated via crypto/rand (256-bit entropy).")
	add("  " + secureStyle.Render("✔") + "  API keys masked during input — never echo'd to terminal.")

	// ── Footer ────────────────────────────────────────────────────────────────
	add("")
	add(divider)
	add("  " + styleSuccess.Render("Flume is live. Open your browser to start orchestrating agents."))
	add("  " + dimStyle.Render("→  Dashboard:  http://localhost:"+s.DashboardPort))
	add("  " + dimStyle.Render("→  Docs:       https://flume.fremen.dev"))
	add("")
	add(divider)
	add("")

	for _, l := range lines {
		println(l)
	}
}

