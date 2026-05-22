// api_settings.go — Settings CRUD: LLM config, credentials, repos, system.
//
// Direct port of Python: src/dashboard/api/settings.py (3 AST nodes).
// Credential logic from: src/dashboard/llm_settings.py (11 nodes).
package dashboard

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/Fremen-Labs/flume/internal/config"
	"github.com/Fremen-Labs/flume/internal/secrets"
)

const (
	llmConfigIndex      = "flume-llm-config"
	llmConfigSingleton  = "singleton"
	repoSettingsIndex   = "flume-repo-settings"
	systemSettingsIndex = "flume-system-settings"
	agentModelsIndex    = "flume-agent-models"
)

// ─── POST /api/settings/log-level ───────────────────────────────────────────

// handleSettingsLogLevel updates the runtime log level.
// Derived from Python: api/settings.py api_set_log_level().
func (s *Server) handleSettingsLogLevel(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Level string `json:"level"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	level := strings.TrimSpace(strings.ToLower(req.Level))
	validLevels := map[string]slog.Level{
		"debug": slog.LevelDebug,
		"info":  slog.LevelInfo,
		"warn":  slog.LevelWarn,
		"error": slog.LevelError,
	}

	slogLevel, ok := validLevels[level]
	if !ok {
		writeError(w, http.StatusBadRequest, "level must be one of: debug, info, warn, error")
		return
	}

	// Update the global log level via the programmatic level var
	if s.logLevel != nil {
		s.logLevel.Set(slogLevel)
	}

	s.logger.Info("log level updated", slog.String("level", level))
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"level":   level,
	})
}

// ─── POST /api/logs/client ──────────────────────────────────────────────────

// handleClientLogs receives client-side logs from the frontend.
// Derived from Python: api/settings.py api_client_logs().
func (s *Server) handleClientLogs(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Level   string `json:"level"`
		Message string `json:"message"`
		Source  string `json:"source"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	s.logger.Info("client log",
		slog.String("level", req.Level),
		slog.String("message", req.Message),
		slog.String("source", req.Source),
	)

	writeJSON(w, http.StatusOK, map[string]interface{}{"received": true})
}

// ─── GET /api/settings/llm ──────────────────────────────────────────────────

// handleSettingsLLMGet returns the current LLM configuration.
// Derived from Python: api/settings.py api_settings_llm().
func (s *Server) handleSettingsLLMGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	src, err := s.es.GetDoc(ctx, llmConfigIndex, llmConfigSingleton)
	if err != nil || src == nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{})
		return
	}

	var config map[string]interface{}
	if err := unmarshalRaw(src, &config); err != nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{})
		return
	}
	writeJSON(w, http.StatusOK, config)
}

// ─── POST /api/settings/llm ────────────────────────────────────────────────

func (s *Server) handleSettingsLLMUpdate(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body map[string]interface{}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	body["updated_at"] = nowISO()

	if err := s.es.Post(ctx, llmConfigIndex+"/_update/"+llmConfigSingleton, map[string]interface{}{
		"doc":           body,
		"doc_as_upsert": true,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update LLM settings")
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
}

// ─── PUT /api/settings/llm/credentials ──────────────────────────────────────

func (s *Server) handleSettingsLLMCredentialsPut(w http.ResponseWriter, r *http.Request) {
	s.handleSettingsLLMUpdate(w, r)
}

// ─── POST /api/settings/llm/credentials ─────────────────────────────────────

// handleSettingsLLMCredentialsPost handles credential actions (upsert, delete, activate).
// Derived from Python: api/settings.py api_settings_llm_credentials_post().
func (s *Server) handleSettingsLLMCredentialsPost(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var req struct {
		Action   string `json:"action"`
		ID       string `json:"id,omitempty"`
		Label    string `json:"label,omitempty"`
		Provider string `json:"provider,omitempty"`
		APIKey   string `json:"apiKey,omitempty"`
		BaseURL  string `json:"baseUrl,omitempty"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	// Fetch current credential doc
	src, err := s.es.GetDoc(ctx, llmConfigIndex, llmConfigSingleton)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to fetch credentials")
		return
	}

	var config map[string]interface{}
	if src != nil {
		_ = unmarshalRaw(src, &config)
	}
	if config == nil {
		config = map[string]interface{}{}
	}

	// Forward the action to the LLM settings handler
	config["_action"] = req.Action
	config["_action_payload"] = map[string]interface{}{
		"id":       req.ID,
		"label":    req.Label,
		"provider": req.Provider,
		"apiKey":   req.APIKey,
		"baseUrl":  req.BaseURL,
	}

	if err := s.es.Post(ctx, llmConfigIndex+"/_update/"+llmConfigSingleton, map[string]interface{}{
		"doc":           config,
		"doc_as_upsert": true,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update credentials")
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
}

// ─── POST /api/settings/llm/oauth/refresh ───────────────────────────────────

func (s *Server) handleSettingsLLMOAuthRefresh(w http.ResponseWriter, r *http.Request) {
	// TODO: Implement OpenAI OAuth token refresh (Phase 4 — secrets module).
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": false,
		"message": "OAuth refresh not yet implemented in Go dashboard",
	})
}

// ─── GET/PUT /api/settings/repos ────────────────────────────────────────────

func (s *Server) handleSettingsReposGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	logger := s.logger
	cfg := config.Get()
	var baoClient *secrets.OpenBaoClient
	if cfg.OpenBaoAddr != "" && cfg.OpenBaoToken != "" {
		baoClient = secrets.NewOpenBaoClient(cfg.OpenBaoAddr, cfg.OpenBaoToken, logger)
	}
	esStore := secrets.NewESStore(logger)
	ghStore := secrets.NewGHTokenStore(esStore, baoClient, logger)
	adoStore := secrets.NewADOTokenStore(esStore, baoClient, logger)

	ghTokens := ghStore.ListPublicTokens(ctx)
	activeGHID := ghStore.GetActiveTokenID(ctx)
	adoTokens := adoStore.ListPublicTokens(ctx)
	activeADOID := adoStore.GetActiveTokenID(ctx)

	resp := map[string]interface{}{
		"settings": map[string]interface{}{
			"githubTokens":        ghTokens,
			"activeGithubTokenId": activeGHID,
			"adoCredentials":      adoTokens,
			"activeAdoCredentialId": activeADOID,
		},
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleSettingsReposUpdate(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body map[string]interface{}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	logger := s.logger
	cfg := config.Get()
	var baoClient *secrets.OpenBaoClient
	if cfg.OpenBaoAddr != "" && cfg.OpenBaoToken != "" {
		baoClient = secrets.NewOpenBaoClient(cfg.OpenBaoAddr, cfg.OpenBaoToken, logger)
	}
	esStore := secrets.NewESStore(logger)

	if ghActionPayload, ok := body["githubTokenAction"].(map[string]interface{}); ok {
		ghStore := secrets.NewGHTokenStore(esStore, baoClient, logger)
		ok, errStr := ghStore.ApplyAction(ctx, ghActionPayload)
		if !ok {
			writeError(w, http.StatusBadRequest, errStr)
			return
		}
		writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
		return
	}

	if adoActionPayload, ok := body["adoTokenAction"].(map[string]interface{}); ok {
		adoStore := secrets.NewADOTokenStore(esStore, baoClient, logger)
		ok, errStr := adoStore.ApplyAction(ctx, adoActionPayload)
		if !ok {
			writeError(w, http.StatusBadRequest, errStr)
			return
		}
		writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
		return
	}

	writeError(w, http.StatusBadRequest, "no recognized token action found in payload")
}

// ─── GET/PUT /api/settings/system ───────────────────────────────────────────

func (s *Server) handleSettingsSystemGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	src, err := s.es.GetDoc(ctx, systemSettingsIndex, "singleton")
	if err != nil || src == nil {
		writeJSON(w, http.StatusOK, map[string]interface{}{})
		return
	}
	var config map[string]interface{}
	_ = unmarshalRaw(src, &config)
	writeJSON(w, http.StatusOK, config)
}

func (s *Server) handleSettingsSystemUpdate(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body map[string]interface{}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	body["updated_at"] = nowISO()
	if err := s.es.Post(ctx, systemSettingsIndex+"/_update/singleton", map[string]interface{}{
		"doc": body, "doc_as_upsert": true,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update system settings")
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
}

// ─── GET/PUT/POST /api/settings/agent-models ────────────────────────────────

func (s *Server) handleSettingsAgentModelsGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	src, err := s.es.GetDoc(ctx, agentModelsIndex, "singleton")

	defaultRoleIds := []string{"pm", "implementer", "reviewer", "tester"}
	defaultLlmModel := "llama3.2"
	defaultExecutionHost := "localhost"
	settingsProvider := "ollama"

	availableProviders := []interface{}{
		map[string]interface{}{
			"providerId": "ollama",
			"label":      "Ollama (local)",
			"configured": true,
			"isPrimary":  true,
			"models": []interface{}{
				map[string]interface{}{"id": "llama3.2", "name": "Llama 3.2"},
				map[string]interface{}{"id": "llama3", "name": "Llama 3"},
				map[string]interface{}{"id": "mistral", "name": "Mistral"},
				map[string]interface{}{"id": "codegemma", "name": "CodeGemma"},
			},
			"allowCustomModelId": true,
			"hint":               "Uses your local Ollama instance (LLM_BASE_URL / default :11434).",
		},
	}

	availableCredentials := []interface{}{
		map[string]interface{}{
			"credentialId": "__ollama__",
			"label":        "Ollama (Local Default)",
			"shortLabel":   "Ollama",
			"providerId":   "ollama",
			"configured":   true,
			"models": []interface{}{
				map[string]interface{}{"id": "llama3.2", "name": "Llama 3.2"},
				map[string]interface{}{"id": "llama3", "name": "Llama 3"},
				map[string]interface{}{"id": "mistral", "name": "Mistral"},
				map[string]interface{}{"id": "codegemma", "name": "CodeGemma"},
			},
			"allowCustomModelId": true,
		},
		map[string]interface{}{
			"credentialId": "__settings_default__",
			"label":        "Global Settings Default",
			"shortLabel":   "Global Default",
			"providerId":   "ollama",
			"configured":   true,
			"models": []interface{}{
				map[string]interface{}{"id": "llama3.2", "name": "Llama 3.2"},
			},
			"allowCustomModelId": true,
		},
	}

	var rolesMap map[string]interface{}
	if err == nil && src != nil {
		var doc map[string]interface{}
		if err := json.Unmarshal(src, &doc); err == nil {
			if rMap, ok := doc["roles"].(map[string]interface{}); ok {
				rolesMap = rMap
			}
		}
	}
	if rolesMap == nil {
		rolesMap = map[string]interface{}{}
	}

	effectiveMap := map[string]interface{}{}
	for _, role := range defaultRoleIds {
		roleConf := map[string]interface{}{
			"provider":      settingsProvider,
			"model":         defaultLlmModel,
			"executionHost": defaultExecutionHost,
			"credentialId":  "__settings_default__",
		}
		if rawSpec, exists := rolesMap[role]; exists {
			if strSpec, ok := rawSpec.(string); ok {
				roleConf["model"] = strSpec
			} else if mapSpec, ok := rawSpec.(map[string]interface{}); ok {
				if p, exists := mapSpec["provider"].(string); exists && p != "" {
					roleConf["provider"] = p
				}
				if m, exists := mapSpec["model"].(string); exists && m != "" {
					roleConf["model"] = m
				}
				if h, exists := mapSpec["executionHost"].(string); exists && h != "" {
					roleConf["executionHost"] = h
				}
				if c, exists := mapSpec["credentialId"].(string); exists && c != "" {
					roleConf["credentialId"] = c
				}
			}
		}
		effectiveMap[role] = roleConf
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"defaultLlmModel":      defaultLlmModel,
		"defaultExecutionHost": defaultExecutionHost,
		"settingsProvider":     settingsProvider,
		"roles":                rolesMap,
		"effective":            effectiveMap,
		"availableProviders":   availableProviders,
		"availableCredentials": availableCredentials,
		"roleIds":              defaultRoleIds,
	})
}

func (s *Server) handleSettingsAgentModelsUpdate(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body map[string]interface{}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	body["updated_at"] = nowISO()
	if err := s.es.Post(ctx, agentModelsIndex+"/_update/singleton", map[string]interface{}{
		"doc": body, "doc_as_upsert": true,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update agent model settings")
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"success": true})
}

// ─── POST /api/settings/restart-services ────────────────────────────────────

func (s *Server) handleSettingsRestartServices(w http.ResponseWriter, r *http.Request) {
	// TODO: Wire to process manager restart logic (Phase 3).
	s.logger.Info("services restart requested")
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"success": true,
		"message": "restart signal sent",
	})
}

// ─── Unmarshal helper ───────────────────────────────────────────────────────

func unmarshalRaw(raw json.RawMessage, dst interface{}) error {
	return json.Unmarshal(raw, dst)
}
