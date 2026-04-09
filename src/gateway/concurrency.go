package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Adaptive Ollama Concurrency Limiter
//
// Detects the system's available resources at startup and dynamically sets
// the maximum number of concurrent Ollama inference requests. This prevents
// the invisible serialization problem where 16 agents queue behind one
// Ollama instance, each waiting 2+ minutes per tool call.
//
// Detection strategy:
//  1. Query Ollama /api/ps to see loaded models and their VRAM usage
//  2. Fall back to CPU core count / 4 (conservative: 1 inference per 4 cores)
//  3. Allow override via FLUME_OLLAMA_MAX_CONCURRENT env var
// ─────────────────────────────────────────────────────────────────────────────

// OllamaSemaphore controls concurrent access to the Ollama inference engine.
type OllamaSemaphore struct {
	sem     chan struct{}
	maxSlots int
	mu      sync.RWMutex
}

// NewOllamaSemaphore creates a semaphore with the given max concurrency.
func NewOllamaSemaphore(maxConcurrent int) *OllamaSemaphore {
	if maxConcurrent < 1 {
		maxConcurrent = 1
	}
	return &OllamaSemaphore{
		sem:      make(chan struct{}, maxConcurrent),
		maxSlots: maxConcurrent,
	}
}

// Acquire blocks until a slot is available or the context is cancelled.
// Returns true if acquired, false if context was cancelled.
func (s *OllamaSemaphore) Acquire(ctx context.Context) bool {
	select {
	case s.sem <- struct{}{}:
		return true
	case <-ctx.Done():
		return false
	}
}

// TryAcquire attempts to acquire without blocking.
// Returns true if acquired, false if all slots are full.
func (s *OllamaSemaphore) TryAcquire() bool {
	select {
	case s.sem <- struct{}{}:
		return true
	default:
		return false
	}
}

// Release returns a slot to the pool.
func (s *OllamaSemaphore) Release() {
	<-s.sem
}

// MaxSlots returns the configured max concurrency.
func (s *OllamaSemaphore) MaxSlots() int {
	return s.maxSlots
}

// ActiveSlots returns how many slots are currently in use.
func (s *OllamaSemaphore) ActiveSlots() int {
	return len(s.sem)
}

// DetectOllamaCapacity determines the optimal concurrency limit for Ollama.
func DetectOllamaCapacity(ollamaBaseURL string) int {
	log := Log()

	// 1. Check explicit env override
	if envMax := strings.TrimSpace(os.Getenv("FLUME_OLLAMA_MAX_CONCURRENT")); envMax != "" {
		if n, err := strconv.Atoi(envMax); err == nil && n > 0 {
			log.Info("ollama concurrency from env override",
				slog.Int("max_concurrent", n),
				slog.String("source", "FLUME_OLLAMA_MAX_CONCURRENT"),
			)
			return n
		}
	}

	// 2. Query Ollama /api/ps to detect loaded models
	maxFromOllama := detectFromOllamaPS(ollamaBaseURL)
	if maxFromOllama > 0 {
		log.Info("ollama concurrency from model detection",
			slog.Int("max_concurrent", maxFromOllama),
			slog.String("source", "ollama_api_ps"),
		)
		return maxFromOllama
	}

	// 3. Fall back to CPU-based heuristic
	cores := runtime.NumCPU()
	maxFromCPU := cores / 4
	if maxFromCPU < 1 {
		maxFromCPU = 1
	}
	if maxFromCPU > 4 {
		maxFromCPU = 4 // Cap at 4 even on beefy machines
	}

	log.Info("ollama concurrency from CPU heuristic",
		slog.Int("max_concurrent", maxFromCPU),
		slog.Int("cpu_cores", cores),
		slog.String("source", "cpu_heuristic"),
	)
	return maxFromCPU
}

// detectFromOllamaPS queries the Ollama /api/ps endpoint to determine how
// many model instances are loaded and their VRAM consumption.
func detectFromOllamaPS(baseURL string) int {
	url := strings.TrimRight(baseURL, "/") + "/api/ps"
	client := &http.Client{Timeout: 5 * time.Second}

	resp, err := client.Get(url)
	if err != nil {
		Log().Debug("could not reach ollama /api/ps",
			slog.String("error", err.Error()),
		)
		return 0
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 16*1024))
	if err != nil {
		return 0
	}

	var psResp struct {
		Models []struct {
			Name     string `json:"name"`
			Size     int64  `json:"size"`
			SizeVRAM int64  `json:"size_vram"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &psResp); err != nil {
		return 0
	}

	if len(psResp.Models) == 0 {
		// No models loaded yet — use conservative default
		return 2
	}

	// Ollama serializes inference for a single model. With one model loaded,
	// true parallelism is 1, but we allow 2-3 to keep the pipeline fed
	// (context switching between requests has minimal overhead for queueing).
	//
	// With multiple models loaded (rare in local dev), each can run concurrently.
	modelCount := len(psResp.Models)

	// For a single model: allow 2 concurrent requests (one active, one pre-queued)
	// For multiple models: allow up to modelCount * 2
	maxConcurrent := modelCount * 2
	if maxConcurrent > 8 {
		maxConcurrent = 8
	}

	Log().Info("ollama model detection",
		slog.Int("models_loaded", modelCount),
		slog.Int("max_concurrent", maxConcurrent),
		slog.String("model_names", fmt.Sprintf("%v", modelNames(psResp.Models))),
	)

	return maxConcurrent
}

func modelNames(models []struct {
	Name     string `json:"name"`
	Size     int64  `json:"size"`
	SizeVRAM int64  `json:"size_vram"`
}) []string {
	names := make([]string, len(models))
	for i, m := range models {
		names[i] = m.Name
	}
	return names
}
