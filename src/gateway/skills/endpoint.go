package skills

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/Fremen-Labs/flume/src/gateway/skillslog"
)

// ─────────────────────────────────────────────────────────────────────────────
// Skill Endpoints — HTTP handlers for skill execution, listing, and management.
//
// These are registered on the gateway mux and expose skills as first-class
// API resources:
//
//   POST /skills/execute/{skill_name}  — Execute a skill with JSON input
//   GET  /skills                       — List all registered skills
//   POST /skills/reload                — Hot-reload skills from disk
//
// Logging follows the unified gateway pattern: slog + secret scrubbing.
// ─────────────────────────────────────────────────────────────────────────────

// HandleSkillExecute looks up and executes a registered skill by name.
func HandleSkillExecute(registry *SkillRegistry) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		requestID := fmt.Sprintf("%08x", time.Now().UnixNano()&0xFFFFFFFF)

		// Extract skill name from URL path
		// Pattern: /skills/execute/{skill_name}
		path := strings.TrimPrefix(r.URL.Path, "/skills/execute/")
		skillName := strings.TrimSpace(path)

		log := skillslog.Log().With(
			slog.String("component", "skill_endpoint"),
			slog.String("request_id", requestID),
			slog.String("skill", skillName),
		)

		if skillName == "" {
			log.Warn("missing skill name in request")
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error":      "skill name is required in URL path",
				"request_id": requestID,
			})
			return
		}

		// Look up the skill
		registered := registry.Get(skillName)
		if registered == nil {
			log.Warn("skill not found", slog.String("skill", skillName))
			writeJSON(w, http.StatusNotFound, map[string]string{
				"error":      fmt.Sprintf("skill %q not found", skillName),
				"request_id": requestID,
			})
			return
		}

		// Cap body size (1MB)
		r.Body = http.MaxBytesReader(w, r.Body, 1<<20)

		// Read input
		var input json.RawMessage
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			log.Warn("invalid request body", slog.String("error", err.Error()))
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error":      "invalid JSON body: " + err.Error(),
				"request_id": requestID,
			})
			return
		}

		log.Info("executing skill",
			slog.String("version", registered.Definition.Version),
			slog.String("inception", string(registered.Definition.Inception)),
		)

		// Execute
		ctx := skillslog.ContextWithLogger(r.Context(), log)
		output, err := registered.Handler.Execute(ctx, input)
		if err != nil {
			log.Error("skill execution failed",
				slog.String("error", err.Error()),
				slog.Float64("duration_ms", msElapsed(start)),
			)
			writeJSON(w, http.StatusInternalServerError, map[string]string{
				"error":      err.Error(),
				"request_id": requestID,
			})
			return
		}

		log.Info("skill execution completed",
			slog.Int("output_bytes", len(output)),
			slog.Float64("duration_ms", msElapsed(start)),
		)

		// Write raw JSON output directly
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write(output)
	}
}

// HandleSkillsList returns metadata for all registered skills.
func HandleSkillsList(registry *SkillRegistry) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		log := skillslog.Log().With(slog.String("component", "skill_endpoint"))
		log.Debug("listing registered skills")

		defs := registry.List()
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"skills": defs,
			"total":  len(defs),
		})
	}
}

// HandleSkillsReload triggers a hot-reload of all skills from disk.
func HandleSkillsReload(registry *SkillRegistry) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		log := skillslog.Log().With(slog.String("component", "skill_endpoint"))
		log.Info("skill hot-reload triggered via API")

		ctx := skillslog.ContextWithLogger(r.Context(), log)
		if err := registry.Reload(ctx); err != nil {
			log.Error("hot-reload failed", slog.String("error", err.Error()))
			writeJSON(w, http.StatusInternalServerError, map[string]string{
				"error": "skill reload failed: " + err.Error(),
			})
			return
		}

		writeJSON(w, http.StatusOK, map[string]interface{}{
			"status": "ok",
			"skills": registry.Count(),
		})
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func msElapsed(start time.Time) float64 {
	return float64(time.Since(start).Microseconds()) / 1000.0
}
