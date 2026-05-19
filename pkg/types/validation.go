// Package types provides domain validation utilities.
//
// Derived from Python: TaskStateMachine (lifecycle/state_machine.py, 2 AST nodes)
package types

import "fmt"

// InvalidTransitionError is returned when an invalid task state change is attempted.
// Derived from Python: class InvalidTransitionError(Exception).
type InvalidTransitionError struct {
	Current TaskStatus
	Target  TaskStatus
	Allowed []TaskStatus
}

func (e *InvalidTransitionError) Error() string {
	return fmt.Sprintf("invalid state transition: %s -> %s (allowed: %v)",
		e.Current, e.Target, e.Allowed)
}

// ValidateTransition checks if a task state change is permitted by the FSM.
// Returns nil if valid, InvalidTransitionError if not.
//
// Mirrors Python: TaskStateMachine.validate_transition()
func ValidateTransition(current, target TaskStatus) error {
	if target == "" {
		return nil // no change requested
	}
	if current == "" {
		current = TaskStatusInbox
	}
	if current == target {
		return nil // self-transition is always valid
	}

	allowed, exists := ValidTransitions[current]
	if !exists {
		// Unknown current state — allow recovery (mirrors Python behavior)
		return nil
	}

	for _, a := range allowed {
		if a == target {
			return nil
		}
	}

	return &InvalidTransitionError{
		Current: current,
		Target:  target,
		Allowed: allowed,
	}
}

// NormalizeProvider canonicalizes a provider name using ProviderAliases.
// Derived from Python: normalize_provider_id() in llm_credentials_store.py.
func NormalizeProvider(pid string) string {
	if pid == "" {
		return pid
	}
	if alias, ok := ProviderAliases[pid]; ok {
		return alias
	}
	return pid
}
