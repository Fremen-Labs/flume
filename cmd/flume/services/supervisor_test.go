package services

import (
	"testing"
)

func TestSplitFirst(t *testing.T) {
	tests := []struct {
		input string
		sep   string
		key   string
		val   string
	}{
		{"FOO=bar", "=", "FOO", "bar"},
		{"ES_URL=https://localhost:9200", "=", "ES_URL", "https://localhost:9200"},
		{"EMPTY=", "=", "EMPTY", ""},
		{"NOSEP", "=", "NOSEP", ""},
	}

	for _, tt := range tests {
		parts := splitFirst(tt.input, tt.sep)
		if parts[0] != tt.key {
			t.Errorf("splitFirst(%q, %q)[0] = %q, want %q", tt.input, tt.sep, parts[0], tt.key)
		}
		if len(parts) == 2 && parts[1] != tt.val {
			t.Errorf("splitFirst(%q, %q)[1] = %q, want %q", tt.input, tt.sep, parts[1], tt.val)
		}
		if len(parts) == 1 && tt.val != "" {
			t.Errorf("splitFirst(%q, %q) returned 1 part, expected 2", tt.input, tt.sep)
		}
	}
}

func TestNewSupervisor(t *testing.T) {
	sup := NewSupervisor(nil, nil)
	if sup == nil {
		t.Fatal("NewSupervisor returned nil")
	}
	if sup.IsRunning() {
		t.Error("new supervisor should not be running")
	}
	if sup.GatewayURL() != "" {
		t.Error("new supervisor should have empty gateway URL")
	}
}
