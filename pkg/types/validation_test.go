package types

import "testing"

func TestValidateTransition_Valid(t *testing.T) {
	tests := []struct {
		from TaskStatus
		to   TaskStatus
	}{
		{TaskStatusPlanned, TaskStatusReady},
		{TaskStatusPlanned, TaskStatusBlocked},
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
	// Empty current defaults to planned
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



func TestTaskStateMachine_EnforceTransition_Shadow(t *testing.T) {
	sm := NewTaskStateMachine(true) // shadow
	err := sm.EnforceTransition(TaskStatusPlanned, TaskStatusDone)
	if err == nil {
		t.Error("expected violation error even in shadow")
	}
}

func TestTaskStateMachine_EnforceTransition_Valid(t *testing.T) {
	sm := DefaultTaskStateMachine
	if err := sm.EnforceTransition(TaskStatusReady, TaskStatusRunning); err != nil {
		t.Errorf("valid transition should pass enforce: %v", err)
	}
}

func TestToComplexityBucket(t *testing.T) {
	tests := map[int]ComplexityBucket{
		1:  ComplexityBucketLow,
		3:  ComplexityBucketLow,
		4:  ComplexityBucketMedium,
		6:  ComplexityBucketMedium,
		7:  ComplexityBucketHigh,
		10: ComplexityBucketHigh,
		0:  ComplexityBucketLow,
	}
	for score, want := range tests {
		if got := ToComplexityBucket(score); got != want {
			t.Errorf("ToComplexityBucket(%d)=%s want %s", score, got, want)
		}
	}
}

func TestTask_ComplexityFields(t *testing.T) {
	task := Task{
		ID:               "t1",
		Status:           TaskStatusPlanned,
		Complexity:       8,
		ComplexityReason: "cross cutting api+ui+db",
		ComplexityBucket: ComplexityBucketHigh,
	}
	if task.Complexity != 8 || task.ComplexityBucket != ComplexityBucketHigh {
		t.Error("complexity fields not set on Task")
	}
}
