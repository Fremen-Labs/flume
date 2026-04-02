package ui

import (
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
