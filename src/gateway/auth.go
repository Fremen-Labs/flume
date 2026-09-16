package gateway

import (
	"crypto/subtle"
	"net/http"
	"os"
	"strings"
)

func adminToken() string {
	return strings.TrimSpace(os.Getenv("FLUME_ADMIN_TOKEN"))
}

func requestAdminToken(r *http.Request) string {
	if t := strings.TrimSpace(r.Header.Get("X-Flume-Token")); t != "" {
		return t
	}
	auth := strings.TrimSpace(r.Header.Get("Authorization"))
	if strings.HasPrefix(strings.ToLower(auth), "bearer ") {
		return strings.TrimSpace(auth[7:])
	}
	return ""
}

func tokenMatch(got, want string) bool {
	if want == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

// requiresAdminAuth is true for spend/mutation routes when an admin token is configured.
func requiresAdminAuth(r *http.Request) bool {
	if adminToken() == "" {
		return false
	}
	if r.Method == http.MethodOptions || r.Method == http.MethodHead {
		return false
	}
	path := r.URL.Path
	switch path {
	case "/health", "/livez", "/readyz":
		return false
	}
	if r.Method == http.MethodGet {
		return false
	}
	return true
}

func (s *Server) authorize(w http.ResponseWriter, r *http.Request) bool {
	if !requiresAdminAuth(r) {
		return true
	}
	if tokenMatch(requestAdminToken(r), adminToken()) {
		return true
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = w.Write([]byte(`{"error":"unauthorized"}`))
	return false
}
