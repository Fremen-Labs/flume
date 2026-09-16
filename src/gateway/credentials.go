package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
)

type credentialUpsertRequest struct {
	Action    string `json:"action"`
	ID        string `json:"id,omitempty"`
	Label     string `json:"label,omitempty"`
	Provider  string `json:"provider,omitempty"`
	APIKey    string `json:"apiKey,omitempty"`
	APIKeyAlt string `json:"api_key,omitempty"`
	BaseURL   string `json:"baseUrl,omitempty"`
}

func (s *Server) handleCredentials(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}
	var req credentialUpsertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"invalid JSON body"}`, http.StatusBadRequest)
		return
	}
	action := strings.ToLower(strings.TrimSpace(req.Action))
	if action == "" {
		action = "upsert"
	}
	switch action {
	case "upsert":
		s.upsertCredential(w, r.Context(), req)
	case "delete":
		s.deleteCredentialMeta(w, r.Context(), strings.TrimSpace(req.ID))
	default:
		http.Error(w, `{"error":"unsupported action"}`, http.StatusBadRequest)
	}
}

func (s *Server) upsertCredential(w http.ResponseWriter, ctx context.Context, req credentialUpsertRequest) {
	log := WithContext(ctx)
	apiKey := strings.TrimSpace(req.APIKey)
	if apiKey == "" {
		apiKey = strings.TrimSpace(req.APIKeyAlt)
	}
	provider := strings.ToLower(strings.TrimSpace(req.Provider))
	if provider == "" || apiKey == "" {
		http.Error(w, `{"error":"provider and apiKey are required"}`, http.StatusBadRequest)
		return
	}
	id := strings.TrimSpace(req.ID)
	if id == "" {
		id = "cred-" + shortID()
	}
	label := strings.TrimSpace(req.Label)
	if label == "" {
		label = provider + " key"
	}

	if s.secrets == nil {
		http.Error(w, `{"error":"secret store is not configured"}`, http.StatusServiceUnavailable)
		return
	}
	path := fmt.Sprintf("secret/data/flume/llm_credentials/%s", id)
	if err := s.secrets.WriteKV(ctx, path, map[string]string{"api_key": apiKey}); err != nil {
		log.Error("credential upsert: openbao write failed", slog.String("error", err.Error()))
		http.Error(w, `{"error":"failed to store secret"}`, http.StatusBadGateway)
		return
	}

	meta := CredentialMeta{
		ID:       id,
		Label:    label,
		Provider: provider,
		BaseURL:  strings.TrimSpace(req.BaseURL),
		HasKey:   true,
	}
	if err := s.persistCredentialMeta(ctx, meta); err != nil {
		log.Error("credential upsert: metadata persist failed", slog.String("error", err.Error()))
		http.Error(w, `{"error":"secret stored but metadata persist failed"}`, http.StatusInternalServerError)
		return
	}

	s.config.mu.Lock()
	if s.config.Credentials == nil {
		s.config.Credentials = make(map[string]CredentialMeta)
	}
	s.config.Credentials[id] = meta
	s.config.mu.Unlock()

	log.Info("credential upserted", slog.String("credential_id", id), slog.String("provider", provider))
	s.writeJSON(w, http.StatusOK, map[string]interface{}{
		"success":       true,
		"credential_id": id,
	})
}

func (s *Server) deleteCredentialMeta(w http.ResponseWriter, ctx context.Context, id string) {
	if id == "" {
		http.Error(w, `{"error":"id is required"}`, http.StatusBadRequest)
		return
	}
	s.config.mu.Lock()
	delete(s.config.Credentials, id)
	remaining := make([]CredentialMeta, 0, len(s.config.Credentials))
	for _, m := range s.config.Credentials {
		remaining = append(remaining, m)
	}
	s.config.mu.Unlock()
	if err := s.writeCredentialList(ctx, remaining); err != nil {
		http.Error(w, `{"error":"failed to persist metadata"}`, http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
}

func (s *Server) persistCredentialMeta(ctx context.Context, meta CredentialMeta) error {
	s.config.mu.RLock()
	list := make([]CredentialMeta, 0, len(s.config.Credentials)+1)
	replaced := false
	for _, existing := range s.config.Credentials {
		if existing.ID == meta.ID {
			list = append(list, meta)
			replaced = true
			continue
		}
		list = append(list, existing)
	}
	s.config.mu.RUnlock()
	if !replaced {
		list = append(list, meta)
	}
	return s.writeCredentialList(ctx, list)
}

func (s *Server) writeCredentialList(ctx context.Context, list []CredentialMeta) error {
	if s.config == nil || s.config.esURL == "" {
		return fmt.Errorf("elasticsearch is not configured")
	}
	body, err := json.Marshal(map[string]interface{}{"credentials": list})
	if err != nil {
		return err
	}
	return s.config.esPut(ctx, "/flume-llm-credentials/_doc/singleton", body)
}
