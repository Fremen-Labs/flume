package skills

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"

	"github.com/Fremen-Labs/flume/src/gateway/skillslog"
)

// ─────────────────────────────────────────────────────────────────────────────
// Skill Registry — Thread-safe in-memory store for parsed skill definitions
// and their executable handlers.
//
// Supports atomic hot-reload: a new map is built from disk, then swapped in
// under a write lock.  Read operations use RLock for zero-contention serving.
//
// Logging follows the unified gateway pattern: slog + secret scrubbing.
// ─────────────────────────────────────────────────────────────────────────────

// SkillHandler is the interface every skill execution strategy must implement.
type SkillHandler interface {
	Execute(ctx context.Context, input json.RawMessage) (json.RawMessage, error)
}

// RegisteredSkill wraps a parsed definition with its runtime handler.
type RegisteredSkill struct {
	Definition SkillDefinition
	Handler    SkillHandler
}

// SkillRegistry is the thread-safe store for all loaded skills.
type SkillRegistry struct {
	mu     sync.RWMutex
	skills map[string]*RegisteredSkill
	dir    string // primary skills directory for reload
}

// NewSkillRegistry creates a new empty registry.
func NewSkillRegistry() *SkillRegistry {
	return &SkillRegistry{
		skills: make(map[string]*RegisteredSkill),
	}
}

// LoadAll discovers and parses all .skill.md files, generates handlers based
// on inception mode, and registers them.  This is the primary initialization
// path called at gateway startup.
func (r *SkillRegistry) LoadAll(ctx context.Context) error {
	log := skillslog.WithContext(ctx).With(slog.String("component", "skill_registry"))

	files, err := DiscoverSkillFiles(ctx)
	if err != nil {
		log.Error("skill discovery failed", slog.String("error", err.Error()))
		return fmt.Errorf("discover skills: %w", err)
	}

	newSkills := make(map[string]*RegisteredSkill)
	var parseErrors int

	for _, path := range files {
		def, err := ParseSkillFile(path)
		if err != nil {
			log.Warn("skipping invalid skill file",
				slog.String("path", path),
				slog.String("error", err.Error()),
			)
			parseErrors++
			continue
		}

		handler, err := GenerateHandler(ctx, def)
		if err != nil {
			log.Warn("failed to generate handler for skill",
				slog.String("name", def.Name),
				slog.String("inception", string(def.Inception)),
				slog.String("error", err.Error()),
			)
			parseErrors++
			continue
		}

		newSkills[def.Name] = &RegisteredSkill{
			Definition: *def,
			Handler:    handler,
		}

		log.Info("skill registered",
			slog.String("name", def.Name),
			slog.String("version", def.Version),
			slog.String("inception", string(def.Inception)),
		)
	}

	// Atomic swap under write lock
	r.mu.Lock()
	r.skills = newSkills
	r.mu.Unlock()

	log.Info("skill registry loaded",
		slog.Int("total_registered", len(newSkills)),
		slog.Int("parse_errors", parseErrors),
	)

	return nil
}

// Get retrieves a registered skill by name. Returns nil if not found.
func (r *SkillRegistry) Get(name string) *RegisteredSkill {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.skills[name]
}

// List returns all registered skill definitions (metadata only).
func (r *SkillRegistry) List() []SkillDefinition {
	r.mu.RLock()
	defer r.mu.RUnlock()

	defs := make([]SkillDefinition, 0, len(r.skills))
	for _, rs := range r.skills {
		defs = append(defs, rs.Definition)
	}
	return defs
}

// Reload performs a hot-reload: re-discovers and re-parses all skill files
// from disk, then atomically swaps the new registry in.
func (r *SkillRegistry) Reload(ctx context.Context) error {
	log := skillslog.WithContext(ctx).With(slog.String("component", "skill_registry"))
	log.Info("hot-reloading skill registry from disk")
	return r.LoadAll(ctx)
}

// Count returns the number of registered skills.
func (r *SkillRegistry) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.skills)
}
