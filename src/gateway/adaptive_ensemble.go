package gateway

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Adaptive Ensemble Sizing — VRAM-pressure-aware jury sizing.
//
// Running N parallel Ollama requests on a Mac laptop causes VRAM thrashing for
// models ≥ 14B. This module estimates the safe ensemble size at request time:
//
//  1. Estimate per-call VRAM cost from the model name tag (":7b" → 4.5 GB)
//  2. Query Ollama /api/ps for live VRAM already in use
//  3. Derive headroom and clamp ensemble size to what fits in FLUME_SYSTEM_MEMORY_GB
//
// FLUME_ENSEMBLE_NO_ADAPTIVE=1 disables this entirely and uses the configured size.
// ─────────────────────────────────────────────────────────────────────────────

// modelParamBrackets maps known parameter-count suffixes to estimated VRAM (GB)
// at Q4_K_M quantisation (Ollama default). Formula: params_B × 0.5 + 1 GB KV cache.
var modelParamBrackets = []struct {
	suffix string
	vramGB float64
}{
	{":0.5b", 1.0},
	{":1b", 1.0},
	{":1.5b", 1.5},
	{":2b", 1.5},
	{":3b", 2.0},
	{":3.8b", 2.5},
	{":4b", 3.0},
	{":7b", 4.5},
	{":8b", 5.0},
	{":9b", 5.5},
	{":13b", 8.0},
	{":14b", 9.0},
	{":26b", 14.0},
	{":27b", 14.5},
	{":32b", 20.0},
	{":34b", 20.0},
	{":70b", 40.0},
	{":72b", 42.0},
	{":110b", 65.0},
}

// ModelVRAMEstimateGB returns a conservative VRAM estimate (GB) for a model.
// Parses the size suffix from the model name (e.g. "qwen2.5-coder:7b" → 4.5 GB).
// Returns 5.0 GB when no suffix is recognised (safe default for unknown 7–8B models).
func ModelVRAMEstimateGB(model string) float64 {
	m := strings.ToLower(strings.TrimSpace(model))
	for _, b := range modelParamBrackets {
		if strings.Contains(m, b.suffix) {
			return b.vramGB
		}
	}
	return 5.0
}

// OllamaVRAMInfo is the subset of /api/ps used for pressure sensing.
type OllamaVRAMInfo struct {
	// TotalUsedGB is the sum of size_vram for all currently loaded models.
	TotalUsedGB float64
	// ModelCount is the number of currently loaded models.
	ModelCount int
}

// QueryOllamaVRAM fetches live VRAM usage from Ollama /api/ps.
// Returns zero values when Ollama is unreachable or the endpoint is absent.
func QueryOllamaVRAM(ollamaBaseURL string) OllamaVRAMInfo {
	url := strings.TrimRight(ollamaBaseURL, "/") + "/api/ps"
	client := &http.Client{Timeout: 3 * time.Second}

	resp, err := client.Get(url)
	if err != nil {
		return OllamaVRAMInfo{}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return OllamaVRAMInfo{}
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 32*1024))
	if err != nil {
		return OllamaVRAMInfo{}
	}

	var psResp struct {
		Models []struct {
			SizeVRAM int64 `json:"size_vram"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &psResp); err != nil {
		return OllamaVRAMInfo{}
	}

	var totalBytes int64
	for _, m := range psResp.Models {
		totalBytes += m.SizeVRAM
	}

	return OllamaVRAMInfo{
		TotalUsedGB: float64(totalBytes) / (1 << 30),
		ModelCount:  len(psResp.Models),
	}
}

// systemMemoryGB returns the operator-configured total unified-memory (GB).
// Default 16 GB — conservative for MacBook Air / base Mac Mini.
func systemMemoryGB() float64 {
	if v := strings.TrimSpace(os.Getenv("FLUME_SYSTEM_MEMORY_GB")); v != "" {
		if gb, err := strconv.ParseFloat(v, 64); err == nil && gb > 0 {
			return gb
		}
	}
	return 16.0
}

// AdaptiveEnsembleSize computes the safe jury size for the current hardware
// state. It queries Ollama /api/ps for live VRAM usage, estimates the per-call
// cost for model, and returns the largest size ≤ configuredSize that fits
// within available memory.
//
// Returns 1 (single-call fallback) when fewer than 2 parallel calls fit.
// When adaptive sizing is disabled (FLUME_ENSEMBLE_NO_ADAPTIVE=1), returns
// configuredSize unchanged.
func AdaptiveEnsembleSize(model string, configuredSize int, ollamaBaseURL string) int {
	if configuredSize <= 1 {
		return configuredSize
	}
	if strings.TrimSpace(os.Getenv("FLUME_ENSEMBLE_NO_ADAPTIVE")) == "1" {
		return configuredSize
	}

	log := Log()

	perCallGB := ModelVRAMEstimateGB(model)
	totalMemGB := systemMemoryGB()

	info := QueryOllamaVRAM(ollamaBaseURL)
	// Reserve 20% of total for OS + non-model overhead.
	reservedGB := totalMemGB * 0.20
	alreadyUsedGB := info.TotalUsedGB
	// The running model itself is already loaded (counted in alreadyUsedGB),
	// so each *additional* parallel call needs one more slot's worth of KV cache
	// and compute buffers. We estimate that as perCallGB × 0.6 (KV cache + buffers,
	// not the weights themselves which are shared in Ollama's memory layout).
	perExtraSlotGB := perCallGB * 0.60

	availableGB := totalMemGB - reservedGB - alreadyUsedGB
	if availableGB < perExtraSlotGB {
		// Not enough headroom for even a second parallel call.
		log.Warn("ensemble pressure: insufficient VRAM headroom, degrading to single call",
			slog.String("model", model),
			slog.Float64("per_call_gb", perCallGB),
			slog.Float64("available_gb", availableGB),
			slog.Float64("total_used_gb", alreadyUsedGB),
		)
		return 1
	}

	// How many extra slots fit?
	extraSlots := int(availableGB / perExtraSlotGB)
	// +1 because the base call is already "in" available budget
	safeSize := extraSlots + 1
	if safeSize > configuredSize {
		safeSize = configuredSize
	}
	if safeSize < 1 {
		safeSize = 1
	}

	if safeSize < configuredSize {
		log.Info("ensemble pressure: adaptive size clamped",
			slog.String("model", model),
			slog.Int("configured", configuredSize),
			slog.Int("adaptive", safeSize),
			slog.Float64("available_gb", availableGB),
			slog.Float64("per_extra_slot_gb", perExtraSlotGB),
		)
	}

	return safeSize
}
