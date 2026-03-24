package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/spf13/cobra"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/Fremen-Labs/flume/cmd/flume/commands"
)

var rootCmd = &cobra.Command{
	Use:   "flume",
	Short: "Autonomous Engineering Frontier - V3 Lipgloss Edge",
	Long:  ui.CyberGradient("Flume Autonomous Engineering Orchestrator V3\nBringing zero-dependency Native Edge speed with 90s SciFi aesthetic."),
}

func init() {
	rootCmd.AddCommand(commands.StartCmd)
	rootCmd.AddCommand(commands.DestroyCmd)
}

func main() {
	// Enforce strict Google Context cancellation mechanics
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Capture interrupt vectors natively ensuring worker groups die cleanly
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigs
		fmt.Println(ui.ErrorRed("\n[X] Hard Interrupt Caught! Scuttling Docker topologies & Native Daemons..."))
		cancel()
		os.Exit(1)
	}()

	if err := rootCmd.ExecuteContext(ctx); err != nil {
		fmt.Println(ui.ErrorRed(fmt.Sprintf("Fatal Orchestrator Failure: %v", err)))
		os.Exit(1)
	}
}
