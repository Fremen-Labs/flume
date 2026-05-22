package e2e

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestCLICompilation verifies that the Flume Go CLI compiles perfectly
// and that it behaves correctly inside a clean Black-Box environment.
func TestCLICompilation(t *testing.T) {
	// 1. Setup isolated throwaway workspace to protect state
	tempDir := t.TempDir()
	binaryPath := filepath.Join(tempDir, "flume")

	// 2. Gate: Attempt compilation
	// We run `go build` targeting the root main.go
	buildCmd := exec.Command("go", "build", "-o", binaryPath, "../../")
	var buildErr bytes.Buffer
	buildCmd.Stderr = &buildErr

	err := buildCmd.Run()
	if err != nil {
		t.Fatalf("Failed to compile flume binary: %v\nStderr: %s", err, buildErr.String())
	}

	// 3. Execution Verification Pattern
	t.Run("Version Flag", func(t *testing.T) {
		cmd := exec.Command(binaryPath, "--version")
		var out bytes.Buffer
		cmd.Stdout = &out
		if err := cmd.Run(); err != nil {
			t.Errorf("Failed to run --version: %v", err)
		}
		if out.Len() == 0 {
			t.Errorf("Expected output from --version flag, got empty stdout")
		}
	})

	t.Run("Help Output", func(t *testing.T) {
		cmd := exec.Command(binaryPath, "help")
		var out bytes.Buffer
		cmd.Stdout = &out
		if err := cmd.Run(); err != nil {
			t.Errorf("Failed to run 'help' command: %v", err)
		}
		if !bytes.Contains(out.Bytes(), []byte("flume")) {
			t.Errorf("Help text did not contain 'flume'. Output: %s", out.String())
		}
	})
	
	// Add deeper black-box structural commands here that don't trigger heavy side-effects
	// (e.g. testing wizard configuration flags via standard input buffers)
}
