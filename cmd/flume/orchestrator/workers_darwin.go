// This file is part of the workers package and provides RAM detection for macOS.
package orchestrator

import (
	"os/exec"
	"strconv"
	"strings"
)

// totalRAMGiB returns the total system RAM in GiB on macOS via sysctl.
func totalRAMGiB() int {
	out, err := exec.Command("sysctl", "-n", "hw.memsize").Output()
	if err != nil {
		return 8 // safe fallback
	}
	bytes, err := strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
	if err != nil {
		return 8
	}
	return int(bytes / (1024 * 1024 * 1024))
}
