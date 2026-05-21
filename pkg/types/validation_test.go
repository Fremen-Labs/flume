package types

import "testing"

func TestValidateTransition_Valid(t *testing.T) {
	tests := []struct {
		from TaskStatus
		to   TaskStatus
	}{
		{TaskStatusInbox, TaskStatusPlanned},
		{TaskStatusInbox, TaskStatusReady},
		{TaskStatusPlanned, TaskStatusReady},
		{TaskStatusReady, TaskStatusRunning},
		{TaskStatusRunning, TaskStatusReview},
		{TaskStatusRunning, TaskStatusDone},
		{TaskStatusRunning, TaskStatusBlocked},
		{TaskStatusReview, TaskStatusDone},
		{TaskStatusReview, TaskStatusRunning},
		{TaskStatusDone, TaskStatusArchived},
		{TaskStatusDone, TaskStatusReady},
		{TaskStatusBlocked, TaskStatusReady},
		{TaskStatusArchived, TaskStatusReady},
	}
	for _, tt := range tests {
		t.Run(string(tt.from)+"->"+string(tt.to), func(t *testing.T) {
			if err := ValidateTransition(tt.from, tt.to); err != nil {
				t.Errorf("expected valid, got: %v", err)
			}
		})
	}
}

func TestValidateTransition_Invalid(t *testing.T) {
	tests := []struct {
		from TaskStatus
		to   TaskStatus
	}{
		{TaskStatusInbox, TaskStatusDone},
		{TaskStatusInbox, TaskStatusRunning},
		{TaskStatusPlanned, TaskStatusDone},
		{TaskStatusReady, TaskStatusDone},
		{TaskStatusDone, TaskStatusRunning},
		{TaskStatusBlocked, TaskStatusDone},
	}
	for _, tt := range tests {
		t.Run(string(tt.from)+"->"+string(tt.to), func(t *testing.T) {
			err := ValidateTransition(tt.from, tt.to)
			if err == nil {
				t.Errorf("expected error for %s -> %s", tt.from, tt.to)
			}
			if _, ok := err.(*InvalidTransitionError); !ok {
				t.Errorf("expected InvalidTransitionError, got %T", err)
			}
		})
	}
}

func TestValidateTransition_SelfTransition(t *testing.T) {
	for status := range ValidTransitions {
		if err := ValidateTransition(status, status); err != nil {
			t.Errorf("self-transition %s should be valid, got: %v", status, err)
		}
	}
}

func TestValidateTransition_EmptyTarget(t *testing.T) {
	if err := ValidateTransition(TaskStatusRunning, ""); err != nil {
		t.Errorf("empty target should be valid, got: %v", err)
	}
}

func TestValidateTransition_EmptyCurrent(t *testing.T) {
	// Empty current defaults to inbox
	if err := ValidateTransition("", TaskStatusPlanned); err != nil {
		t.Errorf("empty current -> planned should be valid, got: %v", err)
	}
}

func TestNormalizeProvider(t *testing.T) {
	tests := map[string]string{
		"google":             "gemini",
		"google-ai":          "gemini",
		"google_ai":          "gemini",
		"googleaistudio":     "gemini",
		"generativelanguage": "gemini",
		"openai":             "openai",
		"anthropic":          "anthropic",
		"ollama":             "ollama",
		"":                   "",
	}
	for input, expected := range tests {
		t.Run(input, func(t *testing.T) {
			got := NormalizeProvider(input)
			if got != expected {
				t.Errorf("NormalizeProvider(%q) = %q, want %q", input, got, expected)
			}
		})
	}
}
