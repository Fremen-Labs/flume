package orchestrator

// workers.go — Worker count resolution.
//
// Supports three modes:
//   - ""     → default (2 workers)
//   - "N"    → explicit count (e.g. "4")
//   - "auto" → hardware-based heuristic using available CPU cores and RAM

import (
	"fmt"
	"runtime"
	"strconv"
	"strings"

	"github.com/charmbracelet/log"
)

const defaultWorkerCount = 2
const minWorkerCount = 1
const maxWorkerCount = 16

// ramPerWorkerGiB is the minimum RAM (in GiB) reserved per worker.
// Workers run LLM inference tasks and need headroom for model context.
const ramPerWorkerGiB = 8

// ResolveWorkerCount interprets the --workers flag value:
//
//	""     → 2 (default)
//	"auto" → CPU/RAM heuristic (see autoWorkerCount)
//	"N"    → explicit integer N (clamped to [1, 16])
func ResolveWorkerCount(flag string) int {
	flag = strings.TrimSpace(strings.ToLower(flag))
	switch flag {
	case "", "default":
		return defaultWorkerCount
	case "auto":
		count := autoWorkerCount()
		log.Infof("Auto worker count resolved: %d", count)
		return count
	default:
		n, err := strconv.Atoi(flag)
		if err != nil || n < minWorkerCount {
			log.Warnf("Invalid --workers value %q, using default (%d)", flag, defaultWorkerCount)
			return defaultWorkerCount
		}
		if n > maxWorkerCount {
			log.Warnf("--workers %d exceeds maximum (%d), clamping", n, maxWorkerCount)
			return maxWorkerCount
		}
		return n
	}
}

// autoWorkerCount determines the optimal worker count from available hardware.
// Strategy:
//
//  1. CPU budget: NumCPU / 2  (leave half for system processes + LLM serve)
//  2. RAM budget: totalRAM_GiB / ramPerWorkerGiB
//  3. Take the minimum, clamped to [minWorkerCount, maxWorkerCount]
func autoWorkerCount() int {
	cpuBudget := runtime.NumCPU() / 2
	if cpuBudget < 1 {
		cpuBudget = 1
	}

	ramGiB := totalRAMGiB()
	ramBudget := ramGiB / ramPerWorkerGiB
	if ramBudget < 1 {
		ramBudget = 1
	}

	log.Infof("Auto-detecting worker count — CPU cores: %d → budget: %d | RAM: %d GiB → budget: %d",
		runtime.NumCPU(), cpuBudget, ramGiB, ramBudget)

	count := cpuBudget
	if ramBudget < count {
		count = ramBudget
	}
	if count < minWorkerCount {
		count = minWorkerCount
	}
	if count > maxWorkerCount {
		count = maxWorkerCount
	}
	return count
}

// BuildWorkerServiceNames returns the docker compose service names for N workers.
// e.g. N=3 → ["dashboard", "worker-1", "worker-2", "worker-3"]
func BuildWorkerServiceNames(workerCount int) []string {
	services := []string{"dashboard"}
	for i := 1; i <= workerCount; i++ {
		services = append(services, fmt.Sprintf("worker-%d", i))
	}
	return services
}

// WorkerCountDescription returns a human-readable description of the count source.
func WorkerCountDescription(flag string) string {
	flag = strings.TrimSpace(strings.ToLower(flag))
	switch flag {
	case "", "default":
		return fmt.Sprintf("%d (default)", defaultWorkerCount)
	case "auto":
		count := autoWorkerCount()
		return fmt.Sprintf("%d (auto-detected from hardware)", count)
	default:
		n, err := strconv.Atoi(flag)
		if err != nil {
			return fmt.Sprintf("%d (default — invalid flag %q)", defaultWorkerCount, flag)
		}
		return fmt.Sprintf("%d (explicit)", n)
	}
}
