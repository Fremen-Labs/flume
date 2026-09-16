package gateway

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRequiresAdminAuth(t *testing.T) {
	t.Setenv("FLUME_ADMIN_TOKEN", "secret")
	req := httptest.NewRequest(http.MethodPost, "/v1/chat", nil)
	if !requiresAdminAuth(req) {
		t.Fatal("POST /v1/chat must require auth when token is set")
	}
	get := httptest.NewRequest(http.MethodGet, "/api/stack", nil)
	if requiresAdminAuth(get) {
		t.Fatal("GET /api/stack must remain public")
	}
	health := httptest.NewRequest(http.MethodGet, "/health", nil)
	if requiresAdminAuth(health) {
		t.Fatal("/health must remain public")
	}
}

func TestAuthorizeRejectsBadToken(t *testing.T) {
	t.Setenv("FLUME_ADMIN_TOKEN", "secret")
	s := &Server{}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat", nil)
	rr := httptest.NewRecorder()
	if s.authorize(rr, req) {
		t.Fatal("expected unauthorized")
	}
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status %d", rr.Code)
	}
	req.Header.Set("X-Flume-Token", "secret")
	rr = httptest.NewRecorder()
	if !s.authorize(rr, req) {
		t.Fatal("expected authorized")
	}
}
