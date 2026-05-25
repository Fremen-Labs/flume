// oauth.go — OpenAI OAuth state management via environment + OpenBao.
//
// Direct port of Python: openai_oauth_state.py (6 AST nodes).
// OAuth state (access_token, refresh_token) is persisted to OpenBao KV-V2
// and synced to OPENAI_OAUTH_STATE_JSON env var for immediate worker use.
package secrets

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
)

// OAuth source identifiers — mirrors Python constants.
const (
	SourceEnv     = "env"
	SourceOpenBao = "openbao"
	SourceMissing = "missing"

	EnvOAuthStateJSON = "OPENAI_OAUTH_STATE_JSON"
	OpenBaoOAuthKey   = "FLUME_CRED___openai_oauth__"
)

// OAuthState represents the OpenAI OAuth token structure.
// Derived from Python: OpenAiOauthState(BaseModel).
type OAuthState struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token,omitempty"`
	ExpiresIn    int    `json:"expires_in,omitempty"`
	ClientID     string `json:"client_id,omitempty"`
	Access       string `json:"access,omitempty"`
	Refresh      string `json:"refresh,omitempty"`
	Expires      int64  `json:"expires,omitempty"`
}

// OAuthStore manages OpenAI OAuth state via env + OpenBao.
type OAuthStore struct {
	bao    *OpenBaoClient
	logger *slog.Logger
}

// NewOAuthStore creates a new OAuth store.
func NewOAuthStore(bao *OpenBaoClient, logger *slog.Logger) *OAuthStore {
	if logger == nil {
		logger = slog.Default()
	}
	return &OAuthStore{bao: bao, logger: logger}
}

// LoadState loads OAuth state from env or OpenBao.
// Derived from Python: load_state_from_env_or_file().
func (s *OAuthStore) LoadState(ctx context.Context) (*OAuthState, string) {
	// 1. Check OPENAI_OAUTH_STATE_JSON env var
	raw := strings.TrimSpace(os.Getenv(EnvOAuthStateJSON))
	if raw != "" {
		var state OAuthState
		if err := json.Unmarshal([]byte(raw), &state); err == nil {
			if state.AccessToken == "" && state.Access != "" {
				state.AccessToken = state.Access
			}
			if state.RefreshToken == "" && state.Refresh != "" {
				state.RefreshToken = state.Refresh
			}
			if state.AccessToken != "" {
				return &state, SourceEnv
			}
		}
		s.logger.Error("failed parsing OPENAI_OAUTH_STATE_JSON from env",
			slog.String("error", "invalid JSON or missing access_token"))
	}

	// 2. Check OpenBao
	if s.bao != nil {
		baoData, err := s.bao.KVGet(ctx, "flume/keys")
		if err == nil && baoData != nil {
			baoRaw, _ := baoData[OpenBaoOAuthKey].(string)
			baoRaw = strings.TrimSpace(baoRaw)
			if baoRaw != "" {
				var state OAuthState
				if err := json.Unmarshal([]byte(baoRaw), &state); err == nil {
					if state.AccessToken == "" && state.Access != "" {
						state.AccessToken = state.Access
					}
					if state.RefreshToken == "" && state.Refresh != "" {
						state.RefreshToken = state.Refresh
					}
					if state.AccessToken != "" {
						return &state, SourceOpenBao
					}
				}
				s.logger.Warn("failed parsing OpenAI OAuth state from OpenBao",
					slog.String("error", "invalid JSON or missing access_token"))
			}
		}
	}

	return nil, SourceMissing
}

// SaveState persists OAuth state to OpenBao and syncs to env.
// Derived from Python: save_state_to_env_or_file().
func (s *OAuthStore) SaveState(ctx context.Context, state *OAuthState) (string, error) {
	if state == nil || state.AccessToken == "" {
		return "", fmt.Errorf("invalid OAuth state: access_token is required")
	}

	raw, err := json.Marshal(state)
	if err != nil {
		return "", err
	}
	rawStr := string(raw)

	persistedOpenBao := false

	// Write to OpenBao
	if s.bao != nil {
		err := s.bao.KVPut(ctx, "flume/keys", map[string]interface{}{
			OpenBaoOAuthKey: rawStr,
		})
		if err != nil {
			s.logger.Error("failed to write OpenAI OAuth state to OpenBao",
				slog.String("error", err.Error()))
		} else {
			persistedOpenBao = true
		}
	}

	// Sync to env for immediate worker use
	os.Setenv(EnvOAuthStateJSON, rawStr)

	if persistedOpenBao {
		return SourceOpenBao, nil
	}
	return SourceEnv, nil
}
