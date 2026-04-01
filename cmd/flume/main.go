package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/Fremen-Labs/flume/cmd/flume/commands"
	"github.com/Fremen-Labs/flume/cmd/flume/ui"
	"github.com/spf13/cobra"
)

var rootCmd = &cobra.Command{
	Use:   "flume",
	Short: "Autonomous Engineering Frontier - V3 Lipgloss Edge",
}

func init() {
	rootCmd.AddCommand(commands.StartCmd)
	rootCmd.AddCommand(commands.DestroyCmd)
	rootCmd.AddCommand(commands.DoctorCmd)
	rootCmd.AddCommand(commands.DispatchCmd)
}

func main() {
	// Execute the 90s SciFi Artifact Banner physically BEFORE TTY interception!
	fmt.Println(ui.SciFiLogo())

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
