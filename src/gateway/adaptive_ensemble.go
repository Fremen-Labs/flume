package gateway

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
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

// systemReservedMemoryFactor is the fraction of total unified memory reserved
// for the operating system, non-model processes, and GPU driver overhead.
// Empirically tuned for Apple Silicon (macOS uses ~15-20% for OS footprint).
const systemReservedMemoryFactor = 0.20

// perSlotKVShareFactor is the fraction of a model's estimated VRAM footprint
// that each additional parallel inference slot requires beyond the first.
// Model weights are shared by Ollama's memory layout; each extra slot only
// needs its own KV cache and compute buffers, not a full model copy.
const perSlotKVShareFactor = 0.60

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
// Returns zero-value OllamaVRAMInfo (TotalUsedGB=0, ModelCount=0) when Ollama
// is unreachable or the endpoint is absent — callers should treat a zero return
// as "assume no models loaded" for conservative sizing.
//
// All failure paths are logged at Warn level with structured context so that
// operators can distinguish transient network errors from persistent misconfiguration.
func QueryOllamaVRAM(ollamaBaseURL string) OllamaVRAMInfo {
	log := Log()
	psURL, err := url.JoinPath(ollamaBaseURL, "/api/ps")
	if err != nil {
		log.Warn("vram_sense: invalid ollama base URL — cannot construct /api/ps endpoint",
			slog.String("base_url", ollamaBaseURL),
			slog.String("error", err.Error()),
		)
		return OllamaVRAMInfo{}
	}
	client := &http.Client{Timeout: 3 * time.Second}

	resp, err := client.Get(psURL)
	if err != nil {
		log.Warn("vram_sense: failed to connect to ollama /api/ps — assuming 0 VRAM in use",
			slog.String("url", psURL),
			slog.String("error", err.Error()),
		)
		return OllamaVRAMInfo{}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Warn("vram_sense: unexpected HTTP status from ollama /api/ps — assuming 0 VRAM in use",
			slog.String("url", psURL),
			slog.Int("status_code", resp.StatusCode),
		)
		return OllamaVRAMInfo{}
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 32*1024))
	if err != nil {
		log.Warn("vram_sense: failed to read ollama /api/ps response body — assuming 0 VRAM in use",
			slog.String("url", psURL),
			slog.String("error", err.Error()),
		)
		return OllamaVRAMInfo{}
	}

	var psResp struct {
		Models []struct {
			SizeVRAM int64 `json:"size_vram"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &psResp); err != nil {
		log.Warn("vram_sense: failed to parse ollama /api/ps JSON — assuming 0 VRAM in use",
			slog.String("url", psURL),
			slog.String("error", err.Error()),
			slog.Int("body_bytes", len(body)),
		)
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
// Logs a warning when FLUME_SYSTEM_MEMORY_GB is set but cannot be parsed so
// that operators are immediately aware of configuration typos (e.g. "16G").
func systemMemoryGB() float64 {
	const defaultGB = 16.0
	if v := strings.TrimSpace(os.Getenv("FLUME_SYSTEM_MEMORY_GB")); v != "" {
		gb, err := strconv.ParseFloat(v, 64)
		if err != nil || gb <= 0 {
			Log().Warn("adaptive_ensemble: FLUME_SYSTEM_MEMORY_GB is invalid — using default",
				slog.String("provided_value", v),
				slog.Float64("default_gb", defaultGB),
				slog.String("hint", "value must be a positive number, e.g. 16 or 64"),
			)
			return defaultGB
		}
		return gb
	}
	return defaultGB
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
	// Reserve a fraction of total memory for OS and non-model overhead.
	reservedGB := totalMemGB * systemReservedMemoryFactor
	alreadyUsedGB := info.TotalUsedGB
	// The running model's weights are shared by Ollama; each additional parallel
	// call only needs its own KV cache and compute buffers (perSlotKVShareFactor).
	perExtraSlotGB := perCallGB * perSlotKVShareFactor

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
