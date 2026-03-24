package ui

import "github.com/charmbracelet/lipgloss"

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

	styleSuccess = lipgloss.NewStyle().Foreground(colorSciFiBlue).Bold(true)
	styleWarning = lipgloss.NewStyle().Foreground(colorHackerGold).Bold(true)
	styleError   = lipgloss.NewStyle().Foreground(colorTerminalRed).Bold(true).Blink(true)
	styleGradient = lipgloss.NewStyle().Foreground(colorNeonGreen).Background(lipgloss.Color("#000000"))
)

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
