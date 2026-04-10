package skills

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"github.com/Fremen-Labs/flume/src/gateway/skillslog"
)

// ─────────────────────────────────────────────────────────────────────────────
// Skill Validator — Meta-Critic–style validation for .skill.md definitions.
//
// Checks structural completeness, schema validity, and adherence to Flume
// skill authoring standards.  Each check returns a severity and message:
//
//   ERROR   → Skill is broken and cannot be loaded
//   WARNING → Skill will work but has quality issues
//   INFO    → Suggestion for improvement
//
// Logging follows the unified gateway pattern: slog + secret scrubbing.
// ─────────────────────────────────────────────────────────────────────────────

// ValidationSeverity indicates how critical a validation finding is.
type ValidationSeverity string

const (
	SeverityError   ValidationSeverity = "ERROR"
	SeverityWarning ValidationSeverity = "WARNING"
	SeverityInfo    ValidationSeverity = "INFO"
)

// ValidationFinding represents a single validation result.
type ValidationFinding struct {
	Severity ValidationSeverity `json:"severity"`
	Rule     string             `json:"rule"`
	Message  string             `json:"message"`
}

// ValidationReport is the complete validation output for a skill.
type ValidationReport struct {
	SkillName string              `json:"skill_name"`
	SkillPath string              `json:"skill_path"`
	Valid     bool                `json:"valid"`
	Findings  []ValidationFinding `json:"findings"`
	Errors    int                 `json:"errors"`
	Warnings  int                 `json:"warnings"`
}

// ValidateSkill runs the full Meta-Critic validation suite against a parsed
// skill definition.  Returns a report with all findings.
func ValidateSkill(def *SkillDefinition) *ValidationReport {
	log := skillslog.Log().With(
		slog.String("component", "skill_validator"),
		slog.String("skill", def.Name),
	)

	report := &ValidationReport{
		SkillName: def.Name,
		SkillPath: def.SourcePath,
		Valid:     true,
	}

	// ── Required metadata checks ──────────────────────────────────────────
	if def.Name == "" {
		report.addError("meta-001", "Skill name is required in front-matter")
	}
	if def.Version == "" {
		report.addWarning("meta-002", "Version is missing; defaulting to unversioned. Specify version for reproducibility.")
	} else if !isValidSemver(def.Version) {
		report.addWarning("meta-003", fmt.Sprintf("Version %q does not follow semver (X.Y.Z). Consider using semantic versioning.", def.Version))
	}

	// ── Inception mode checks ─────────────────────────────────────────────
	switch def.Inception {
	case InceptionFull:
		if def.PromptTemplate != "" {
			report.addInfo("inception-001", "Full inception skills ignore the Prompt Template section. Remove it to avoid confusion.")
		}
	case InceptionHybrid:
		if def.PromptTemplate == "" {
			report.addWarning("inception-002", "Hybrid inception skills should define a Prompt Template for the LLM delegation step.")
		}
	case InceptionLLMOnly:
		if def.PromptTemplate == "" {
			report.addError("inception-003", "LLM-only inception skills require a Prompt Template — there is no Go logic fallback.")
		}
	}

	// ── Description checks ────────────────────────────────────────────────
	if def.Description == "" {
		report.addWarning("desc-001", "Description section is missing. Add a description for discoverability and documentation.")
	} else if len(def.Description) < 20 {
		report.addInfo("desc-002", "Description is very short. Consider adding more detail for better documentation.")
	}

	// ── Schema checks ─────────────────────────────────────────────────────
	if def.InputSchema == nil {
		report.addWarning("schema-001", "Input Schema is missing. Without a schema, input validation is skipped entirely.")
	} else {
		validateSchemaStructure("Input Schema", def.InputSchema, report)
	}

	if def.OutputSchema == nil {
		report.addWarning("schema-002", "Output Schema is missing. Without it, output cannot be verified.")
	} else {
		validateSchemaStructure("Output Schema", def.OutputSchema, report)
	}

	// ── Schema cross-validation ───────────────────────────────────────────
	if def.InputSchema != nil && def.PromptTemplate != "" {
		checkPromptSchemaAlignment(def, report)
	}

	// ── Validation Rules checks ───────────────────────────────────────────
	if len(def.ValidationRules) == 0 {
		report.addInfo("rules-001", "No Validation Rules defined. Adding rules enables Meta-Critic post-execution checks.")
	}
	for i, rule := range def.ValidationRules {
		if len(rule) < 10 {
			report.addInfo("rules-002", fmt.Sprintf("Validation rule #%d is very short (%q). Consider being more specific.", i+1, rule))
		}
	}

	// ── Ensemble & creativity checks ──────────────────────────────────────
	if def.EnsembleSize > 5 {
		report.addWarning("perf-001", fmt.Sprintf("Ensemble size %d is high. This will make %d parallel LLM calls per execution.", def.EnsembleSize, def.EnsembleSize))
	}
	if def.CreativityLevel > 2 {
		report.addWarning("perf-002", fmt.Sprintf("Creativity level %d is high. This increases temperature and may produce non-deterministic output.", def.CreativityLevel))
	}

	// ── Context Requirements checks ───────────────────────────────────────
	for i, ctx := range def.ContextReqs {
		if ctx.Index == "" {
			report.addError("ctx-001", fmt.Sprintf("Context requirement #%d is missing 'index' field.", i+1))
		}
		if ctx.Query == "" {
			report.addError("ctx-002", fmt.Sprintf("Context requirement #%d is missing 'query' field.", i+1))
		}
	}

	log.Info("skill validation complete",
		slog.String("skill", def.Name),
		slog.Bool("valid", report.Valid),
		slog.Int("errors", report.Errors),
		slog.Int("warnings", report.Warnings),
		slog.Int("total_findings", len(report.Findings)),
	)

	return report
}

// ─────────────────────────────────────────────────────────────────────────────
// Internal validation helpers
// ─────────────────────────────────────────────────────────────────────────────

// validateSchemaStructure checks that a JSON schema has the expected structure.
func validateSchemaStructure(name string, schema json.RawMessage, report *ValidationReport) {
	var s map[string]interface{}
	if err := json.Unmarshal(schema, &s); err != nil {
		report.addError("schema-100", fmt.Sprintf("%s is not valid JSON: %v", name, err))
		return
	}

	// Check for 'type' field
	if _, ok := s["type"]; !ok {
		report.addWarning("schema-101", fmt.Sprintf("%s is missing 'type' field. Add \"type\": \"object\" for proper validation.", name))
	}

	// Check for 'properties' when type is object
	if t, ok := s["type"].(string); ok && t == "object" {
		if _, ok := s["properties"]; !ok {
			report.addWarning("schema-102", fmt.Sprintf("%s declares type 'object' but has no 'properties'. Define at least one property.", name))
		}
	}

	// Check that 'required' references valid properties
	if required, ok := s["required"].([]interface{}); ok {
		props, hasProps := s["properties"].(map[string]interface{})
		if hasProps {
			for _, r := range required {
				if fieldName, ok := r.(string); ok {
					if _, exists := props[fieldName]; !exists {
						report.addError("schema-103", fmt.Sprintf("%s: required field %q is not defined in 'properties'.", name, fieldName))
					}
				}
			}
		}
	}
}

// checkPromptSchemaAlignment verifies that prompt template variables reference
// fields from the input schema.
func checkPromptSchemaAlignment(def *SkillDefinition, report *ValidationReport) {
	// Extract {variable} references from the prompt template
	vars := extractTemplateVars(def.PromptTemplate)
	if len(vars) == 0 {
		return
	}

	// Get schema properties
	var schema struct {
		Properties map[string]interface{} `json:"properties"`
	}
	if err := json.Unmarshal(def.InputSchema, &schema); err != nil || schema.Properties == nil {
		return
	}

	for _, v := range vars {
		if _, exists := schema.Properties[v]; !exists {
			report.addWarning("align-001", fmt.Sprintf("Prompt Template references {%s} but Input Schema has no property named %q.", v, v))
		}
	}
}

// extractTemplateVars finds all {variable} references in a prompt template.
func extractTemplateVars(tmpl string) []string {
	var vars []string
	seen := make(map[string]bool)
	i := 0
	for i < len(tmpl) {
		start := strings.IndexByte(tmpl[i:], '{')
		if start == -1 {
			break
		}
		start += i
		end := strings.IndexByte(tmpl[start:], '}')
		if end == -1 {
			break
		}
		end += start
		name := tmpl[start+1 : end]
		if name != "" && !seen[name] {
			vars = append(vars, name)
			seen[name] = true
		}
		i = end + 1
	}
	return vars
}

// isValidSemver does a basic check for X.Y.Z format.
func isValidSemver(v string) bool {
	parts := strings.Split(v, ".")
	if len(parts) < 2 || len(parts) > 3 {
		return false
	}
	for _, p := range parts {
		if p == "" {
			return false
		}
		for _, c := range p {
			if c < '0' || c > '9' {
				return false
			}
		}
	}
	return true
}

// ─────────────────────────────────────────────────────────────────────────────
// Report helpers
// ─────────────────────────────────────────────────────────────────────────────

func (r *ValidationReport) addError(rule, msg string) {
	r.Findings = append(r.Findings, ValidationFinding{
		Severity: SeverityError,
		Rule:     rule,
		Message:  msg,
	})
	r.Errors++
	r.Valid = false
}

func (r *ValidationReport) addWarning(rule, msg string) {
	r.Findings = append(r.Findings, ValidationFinding{
		Severity: SeverityWarning,
		Rule:     rule,
		Message:  msg,
	})
	r.Warnings++
}

func (r *ValidationReport) addInfo(rule, msg string) {
	r.Findings = append(r.Findings, ValidationFinding{
		Severity: SeverityInfo,
		Rule:     rule,
		Message:  msg,
	})
}
