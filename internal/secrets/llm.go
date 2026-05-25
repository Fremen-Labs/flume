// llm.go — LLM credential CRUD backed by ES metadata + OpenBao secrets.
//
// Direct port of Python: llm_credentials_store.py (8 AST nodes).
// Metadata (labels, providers, active status) → Elasticsearch.
// API keys → OpenBao KV-V2 at secret/data/flume/llm_credentials/{id}.
package secrets

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
)

// Well-known credential IDs — mirrors Python constants.
const (
	OllamaCredentialID        = "__ollama__"
	SettingsDefaultCredID     = "__settings_default__"
	OpenAIOAuthCredentialID   = "__openai_oauth__"
)

// LLM credential actions — mirrors Python constants.
const (
	ActionDelete   = "delete"
	ActionActivate = "activate"
	ActionDefault  = "default"
	ActionPatch    = "patch"
	ActionUpsert   = "upsert"
)

// ProviderAliases maps alternative provider names to canonical IDs.
// Derived from Python: _PROVIDER_ALIASES.
var ProviderAliases = map[string]string{
	"google":              "gemini",
	"google-ai":           "gemini",
	"google_ai":           "gemini",
	"googleaistudio":      "gemini",
	"generativelanguage":  "gemini",
}

// NormalizeProviderID normalizes a provider identifier.
// Derived from Python: normalize_provider_id().
func NormalizeProviderID(pid string) string {
	p := strings.TrimSpace(strings.ToLower(pid))
	if alias, ok := ProviderAliases[p]; ok {
		return alias
	}
	return p
}

// LLMCredential represents a saved LLM API key.
// Derived from Python: LlmCredential(BaseModel).
type LLMCredential struct {
	ID       string `json:"id"`
	Label    string `json:"label"`
	Provider string `json:"provider"`
	APIKey   string `json:"apiKey"`
	BaseURL  string `json:"baseUrl"`
}

// LLMMetadataDoc is the singleton document in flume-llm-credentials.
// Derived from Python: LlmMetadataDoc(BaseModel).
type LLMMetadataDoc struct {
	Version             int             `json:"version"`
	ActiveCredentialID  string          `json:"activeCredentialId"`
	DefaultCredentialID string          `json:"defaultCredentialId"`
	Credentials         []LLMCredential `json:"credentials"`
}

// DefaultLLMDoc returns an empty metadata document.
func DefaultLLMDoc() *LLMMetadataDoc {
	return &LLMMetadataDoc{
		Version:     1,
		Credentials: []LLMCredential{},
	}
}

// LLMCredentialStore manages LLM credentials via ES + OpenBao.
type LLMCredentialStore struct {
	esStore *ESStore
	bao     *OpenBaoClient
	logger  *slog.Logger
}

// NewLLMCredentialStore creates a new LLM credential store.
func NewLLMCredentialStore(esStore *ESStore, bao *OpenBaoClient, logger *slog.Logger) *LLMCredentialStore {
	if logger == nil {
		logger = slog.Default()
	}
	return &LLMCredentialStore{
		esStore: esStore,
		bao:     bao,
		logger:  logger,
	}
}

// LoadDocument loads the LLM credentials metadata from ES.
// Derived from Python: load_document().
func (s *LLMCredentialStore) LoadDocument(ctx context.Context) *LLMMetadataDoc {
	raw, err := s.esStore.LoadLLMCredentials(ctx)
	if err != nil || raw == nil {
		return DefaultLLMDoc()
	}
	doc := docToLLMMetadata(raw)
	if doc.DefaultCredentialID == "" && doc.ActiveCredentialID != "" {
		doc.DefaultCredentialID = doc.ActiveCredentialID
	}
	return doc
}

// SaveDocument persists the LLM credentials metadata to ES.
// Derived from Python: save_document().
func (s *LLMCredentialStore) SaveDocument(ctx context.Context, doc *LLMMetadataDoc) {
	// Mask keys before saving to ES
	masked := *doc
	maskedCreds := make([]LLMCredential, len(doc.Credentials))
	for i, c := range doc.Credentials {
		maskedCreds[i] = c
		key := strings.TrimSpace(c.APIKey)
		if key != "" && key != "***" && key != OpenbaoMask {
			maskedCreds[i].APIKey = OpenbaoMask
		}
	}
	masked.Credentials = maskedCreds

	raw := llmMetadataToDoc(&masked)
	if err := s.esStore.SaveLLMCredentials(ctx, raw); err != nil {
		s.logger.Error("failed to persist LLM credentials to ES", slog.String("error", err.Error()))
	}
}

// ListPublicCredentials returns credentials safe for API responses (no raw keys).
// Derived from Python: list_public_credentials().
func (s *LLMCredentialStore) ListPublicCredentials(ctx context.Context) []map[string]interface{} {
	doc := s.LoadDocument(ctx)
	var out []map[string]interface{}
	for _, c := range doc.Credentials {
		cid := strings.TrimSpace(c.ID)
		if cid == "" {
			continue
		}
		key := strings.TrimSpace(c.APIKey)
		out = append(out, map[string]interface{}{
			"id":        cid,
			"label":     strings.TrimSpace(c.Label),
			"provider":  NormalizeProviderID(c.Provider),
			"keySuffix": keySuffix(key),
			"hasKey":    key != "",
			"baseUrl":   strings.TrimSpace(c.BaseURL),
		})
	}
	return out
}

// GetByID returns a credential by ID.
// Derived from Python: get_by_id().
func (s *LLMCredentialStore) GetByID(ctx context.Context, credID string) *LLMCredential {
	cid := strings.TrimSpace(credID)
	if cid == "" || cid == OllamaCredentialID || cid == OpenAIOAuthCredentialID {
		return nil
	}
	doc := s.LoadDocument(ctx)
	for _, c := range doc.Credentials {
		if strings.TrimSpace(c.ID) == cid {
			return &c
		}
	}
	return nil
}

// GetResolvedForWorker returns LLM overrides for one call.
// Derived from Python: get_resolved_for_worker().
func (s *LLMCredentialStore) GetResolvedForWorker(ctx context.Context, credID string) map[string]string {
	cid := strings.TrimSpace(credID)
	if cid == "" || cid == SettingsDefaultCredID {
		return nil
	}
	if cid == OllamaCredentialID {
		return map[string]string{"provider": "ollama", "api_key": "", "base_url": ""}
	}
	if cid == OpenAIOAuthCredentialID {
		return map[string]string{"provider": "openai", "api_key": "", "base_url": ""}
	}

	c := s.GetByID(ctx, cid)
	if c == nil {
		return nil
	}

	key := strings.TrimSpace(c.APIKey)
	prov := NormalizeProviderID(c.Provider)
	base := strings.TrimSpace(c.BaseURL)

	// Resolve delegated keys from OpenBao
	if key == OpenbaoMask && s.bao != nil {
		baoData, err := s.bao.KVGet(ctx, fmt.Sprintf("flume/llm_credentials/%s", cid))
		if err == nil && baoData != nil {
			if delegated, ok := baoData["api_key"].(string); ok && strings.TrimSpace(delegated) != "" {
				key = strings.TrimSpace(delegated)
			}
		}
	}

	if key == "" {
		return nil
	}

	return map[string]string{"provider": prov, "api_key": key, "base_url": base}
}

// GetActiveCredentialID returns the default saved credential ID.
// Derived from Python: get_active_credential_id().
func (s *LLMCredentialStore) GetActiveCredentialID(ctx context.Context) string {
	doc := s.LoadDocument(ctx)
	cid := strings.TrimSpace(doc.DefaultCredentialID)
	if cid == "" {
		cid = strings.TrimSpace(doc.ActiveCredentialID)
	}
	return cid
}

// SetActiveCredentialID persists the default saved key.
// Derived from Python: set_active_credential_id().
func (s *LLMCredentialStore) SetActiveCredentialID(ctx context.Context, credID string) {
	doc := s.LoadDocument(ctx)
	cid := strings.TrimSpace(credID)
	doc.DefaultCredentialID = cid
	doc.ActiveCredentialID = cid
	s.SaveDocument(ctx, doc)
}

// UpsertCredential inserts or replaces a credential by ID.
// Derived from Python: upsert_credential().
func (s *LLMCredentialStore) UpsertCredential(ctx context.Context, credID, label, provider, apiKey, baseURL string) (string, error) {
	doc := s.LoadDocument(ctx)
	pid := NormalizeProviderID(provider)
	label = strings.TrimSpace(label)
	if label == "" {
		label = pid + " key"
	}
	key := strings.TrimSpace(apiKey)
	base := strings.TrimSpace(baseURL)
	newID := strings.TrimSpace(credID)
	if newID == "" {
		newID = GenerateShortID()
	}

	replaced := false
	for i, c := range doc.Credentials {
		if c.ID == newID {
			oldKey := strings.TrimSpace(c.APIKey)
			doc.Credentials[i].Label = label
			doc.Credentials[i].Provider = pid
			doc.Credentials[i].BaseURL = base
			if key != "" {
				doc.Credentials[i].APIKey = key
			} else {
				doc.Credentials[i].APIKey = oldKey
			}
			replaced = true
			break
		}
	}

	if !replaced {
		doc.Credentials = append(doc.Credentials, LLMCredential{
			ID:       newID,
			Label:    label,
			Provider: pid,
			APIKey:   key,
			BaseURL:  base,
		})
	}

	// Store secret in OpenBao
	if key != "" && key != OpenbaoMask && s.bao != nil {
		_ = s.bao.KVPut(ctx, fmt.Sprintf("flume/llm_credentials/%s", newID), map[string]interface{}{
			"api_key": key,
		})
	}

	s.SaveDocument(ctx, doc)
	return newID, nil
}

// DeleteCredential removes a credential by ID.
// Derived from Python: delete_credential().
func (s *LLMCredentialStore) DeleteCredential(ctx context.Context, credID string) bool {
	doc := s.LoadDocument(ctx)

	newCreds := make([]LLMCredential, 0, len(doc.Credentials))
	for _, c := range doc.Credentials {
		if c.ID != credID {
			newCreds = append(newCreds, c)
		}
	}
	if len(newCreds) == len(doc.Credentials) {
		return false
	}

	doc.Credentials = newCreds
	if doc.ActiveCredentialID == credID {
		doc.ActiveCredentialID = ""
	}
	if doc.DefaultCredentialID == credID {
		doc.DefaultCredentialID = ""
	}

	// Clear OpenBao secret
	if s.bao != nil {
		_ = s.bao.KVPut(ctx, "flume/keys", map[string]interface{}{
			fmt.Sprintf("FLUME_CRED_%s", credID): "",
		})
	}

	s.SaveDocument(ctx, doc)
	return true
}

// ─── Helpers ────────────────────────────────────────────────────────────────

func keySuffix(key string) string {
	t := strings.TrimSpace(key)
	if t == "" {
		return ""
	}
	if len(t) <= 4 {
		return "••••"
	}
	return t[len(t)-4:]
}

func docToLLMMetadata(raw map[string]interface{}) *LLMMetadataDoc {
	doc := DefaultLLMDoc()
	if v, ok := raw["version"].(float64); ok {
		doc.Version = int(v)
	}
	if v, ok := raw["activeCredentialId"].(string); ok {
		doc.ActiveCredentialID = v
	}
	if v, ok := raw["defaultCredentialId"].(string); ok {
		doc.DefaultCredentialID = v
	}
	if creds, ok := raw["credentials"].([]interface{}); ok {
		for _, c := range creds {
			cm, _ := c.(map[string]interface{})
			if cm == nil {
				continue
			}
			doc.Credentials = append(doc.Credentials, LLMCredential{
				ID:       strVal(cm["id"]),
				Label:    strVal(cm["label"]),
				Provider: strVal(cm["provider"]),
				APIKey:   strVal(cm["apiKey"]),
				BaseURL:  strVal(cm["baseUrl"]),
			})
		}
	}
	return doc
}

func llmMetadataToDoc(doc *LLMMetadataDoc) map[string]interface{} {
	creds := make([]interface{}, len(doc.Credentials))
	for i, c := range doc.Credentials {
		creds[i] = map[string]interface{}{
			"id":       c.ID,
			"label":    c.Label,
			"provider": c.Provider,
			"apiKey":   c.APIKey,
			"baseUrl":  c.BaseURL,
		}
	}
	return map[string]interface{}{
		"version":             doc.Version,
		"activeCredentialId":  doc.ActiveCredentialID,
		"defaultCredentialId": doc.DefaultCredentialID,
		"credentials":         creds,
	}
}

func strVal(v interface{}) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}
