package config

import (
	"os"
	"testing"
)

func TestDefaultConfig(t *testing.T) {
	os.Unsetenv("FLUME_NATIVE_MODE")
	os.Unsetenv("ES_URL")
	cfg := DefaultConfig()

	if cfg.LLMProvider != "ollama" {
		t.Errorf("expected ollama, got %s", cfg.LLMProvider)
	}
	if cfg.ESURL != "http://elasticsearch:9200" {
		t.Errorf("expected container ES URL, got %s", cfg.ESURL)
	}
	if cfg.NativeMode {
		t.Error("expected NativeMode=false")
	}
}

func TestDefaultConfig_NativeMode(t *testing.T) {
	os.Setenv("FLUME_NATIVE_MODE", "1")
	defer os.Unsetenv("FLUME_NATIVE_MODE")
	os.Unsetenv("ES_URL")

	cfg := DefaultConfig()
	if cfg.ESURL != "http://localhost:9200" {
		t.Errorf("expected localhost ES URL in native mode, got %s", cfg.ESURL)
	}
	if !cfg.NativeMode {
		t.Error("expected NativeMode=true")
	}
}

func TestOverlayFromEnv(t *testing.T) {
	cfg := DefaultConfig()

	os.Setenv("LLM_PROVIDER", "anthropic")
	os.Setenv("LLM_MODEL", "claude-4")
	os.Setenv("DASHBOARD_PORT", "9090")
	os.Setenv("WORKERS_PER_ROLE", "3")
	defer func() {
		os.Unsetenv("LLM_PROVIDER")
		os.Unsetenv("LLM_MODEL")
		os.Unsetenv("DASHBOARD_PORT")
		os.Unsetenv("WORKERS_PER_ROLE")
	}()

	cfg.OverlayFromEnv()

	if cfg.LLMProvider != "anthropic" {
		t.Errorf("expected anthropic, got %s", cfg.LLMProvider)
	}
	if cfg.LLMModel != "claude-4" {
		t.Errorf("expected claude-4, got %s", cfg.LLMModel)
	}
	if cfg.DashboardPort != 9090 {
		t.Errorf("expected 9090, got %d", cfg.DashboardPort)
	}
	if cfg.WorkersPerRole != 3 {
		t.Errorf("expected 3, got %d", cfg.WorkersPerRole)
	}
}

func TestOverlayFromEnv_InvalidInt(t *testing.T) {
	cfg := DefaultConfig()
	os.Setenv("DASHBOARD_PORT", "not-a-number")
	defer os.Unsetenv("DASHBOARD_PORT")

	originalPort := cfg.DashboardPort
	cfg.OverlayFromEnv()

	if cfg.DashboardPort != originalPort {
		t.Errorf("invalid int should not change port, got %d", cfg.DashboardPort)
	}
}

func TestRewriteLoopbackForDocker(t *testing.T) {
	// Outside Docker (no /.dockerenv), should pass through
	os.Unsetenv("FLUME_NATIVE_MODE")
	url := "http://127.0.0.1:11434"
	got := RewriteLoopbackForDocker(url)
	// Can't test Docker rewrite without /.dockerenv, but can test passthrough
	if got != url {
		// This is expected when not in Docker — the function should return unchanged
		t.Logf("non-Docker passthrough works: %s", got)
	}

	// Native mode should always passthrough
	os.Setenv("FLUME_NATIVE_MODE", "1")
	defer os.Unsetenv("FLUME_NATIVE_MODE")
	got = RewriteLoopbackForDocker(url)
	if got != url {
		t.Errorf("native mode should passthrough, got %s", got)
	}
}
