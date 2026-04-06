// This file is part of the workers package and provides RAM detection for Linux.
package orchestrator

import "syscall"

// totalRAMGiB returns the total system RAM in GiB on Linux via sysinfo syscall.
func totalRAMGiB() int {
	var info syscall.Sysinfo_t
	if err := syscall.Sysinfo(&info); err != nil {
		return 8 // safe fallback
	}
	// Totalram is in bytes; Units multiplier may apply on older kernels
	bytes := info.Totalram * uint64(info.Unit)
	return int(bytes / (1024 * 1024 * 1024))
}
