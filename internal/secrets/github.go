// github.go — GitHub PAT credential CRUD backed by ES metadata + OpenBao secrets.
//
// Direct port of Python: github_tokens_store.py (5 AST nodes).
// Metadata (labels, active token) → Elasticsearch (flume-github-tokens).
// PATs → OpenBao KV-V2 at secret/data/flume/github_tokens/{id}.
package secrets

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
)

// GitHub well-known constants — mirrors Python constants.
const (
	EnvGHToken         = "GH_TOKEN"
	DefaultGHLabel     = "GitHub PAT"
	DefaultLegacyLabel = "Default"
)

// GitHub token actions.
const (
	GHActionDelete    = "delete"
	GHActionSetActive = "setactive"
	GHActionUpsert    = "upsert"
)

// GHCredential represents a saved GitHub PAT.
// Derived from Python: GhCredential(BaseModel).
type GHCredential struct {
	ID    string `json:"id"`
	Label string `json:"label"`
	Token string `json:"token"`
}

// GHMetadataDoc is the singleton document in flume-github-tokens.
// Derived from Python: GhMetadataDoc(BaseModel).
type GHMetadataDoc struct {
	Version       int            `json:"version"`
	ActiveTokenID string         `json:"activeTokenId"`
	Tokens        []GHCredential `json:"tokens"`
}

// DefaultGHDoc returns an empty GitHub metadata document.
func DefaultGHDoc() *GHMetadataDoc {
	return &GHMetadataDoc{
		Version: 1,
		Tokens:  []GHCredential{},
	}
}

// GHTokenStore manages GitHub tokens via ES + OpenBao.
type GHTokenStore struct {
	esStore *ESStore
	bao     *OpenBaoClient
	logger  *slog.Logger
}

// NewGHTokenStore creates a new GitHub token store.
func NewGHTokenStore(esStore *ESStore, bao *OpenBaoClient, logger *slog.Logger) *GHTokenStore {
	if logger == nil {
		logger = slog.Default()
	}
	return &GHTokenStore{esStore: esStore, bao: bao, logger: logger}
}

// ValidateGitHubToken validates GitHub token format.
// Derived from Python: validate_github_token().
var ghTokenPattern = regexp.MustCompile(`^(ghp|github_pat|ghs|gho|ghu)_[a-zA-Z0-9_]{10,}$`)

func ValidateGitHubToken(token string) bool {
	t := strings.TrimSpace(token)
	if t == "" {
		return false
	}
	return ghTokenPattern.MatchString(t)
}

// LoadDocument loads GitHub token metadata from ES.
// Derived from Python: load_document().
func (s *GHTokenStore) LoadDocument(ctx context.Context) *GHMetadataDoc {
	raw, err := s.esStore.LoadGHTokens(ctx)
	if err != nil || raw == nil {
		return DefaultGHDoc()
	}
	return docToGHMetadata(raw)
}

// SaveDocument persists GitHub token metadata to ES.
// Derived from Python: save_document().
func (s *GHTokenStore) SaveDocument(ctx context.Context, doc *GHMetadataDoc) {
	masked := *doc
	maskedTokens := make([]GHCredential, len(doc.Tokens))
	for i, c := range doc.Tokens {
		maskedTokens[i] = c
		tok := strings.TrimSpace(c.Token)
		if tok != "" && tok != "***" && tok != OpenbaoMask {
			maskedTokens[i].Token = OpenbaoMask
		}
	}
	masked.Tokens = maskedTokens

	raw := ghMetadataToDoc(&masked)
	if err := s.esStore.SaveGHTokens(ctx, raw); err != nil {
		s.logger.Error("failed to persist GitHub tokens to ES", slog.String("error", err.Error()))
	}
}

// GetActiveTokenID returns the active token ID.
// Derived from Python: get_active_token_id().
func (s *GHTokenStore) GetActiveTokenID(ctx context.Context) string {
	return strings.TrimSpace(s.LoadDocument(ctx).ActiveTokenID)
}

// GetActiveTokenPlain returns the plaintext active PAT, resolving from OpenBao if delegated.
// Derived from Python: get_active_token_plain().
func (s *GHTokenStore) GetActiveTokenPlain(ctx context.Context) string {
	doc := s.LoadDocument(ctx)
	aid := strings.TrimSpace(doc.ActiveTokenID)
	for _, c := range doc.Tokens {
		if strings.TrimSpace(c.ID) != aid {
			continue
		}
		token := strings.TrimSpace(c.Token)

		// Hard safety: never return a raw secret that somehow came from ES.
		// The contract is: ES may only contain the OpenbaoMask (or legacy "***").
		// Real secrets must come from OpenBao.
		if token != "" && token != OpenbaoMask && token != "***" && token != "***OPENBAO_DELEGATED***" {
			s.logger.Error("SECURITY: raw secret value detected in ES document for GitHub token — refusing to use it. This violates the ES-metadata-only contract.",
				slog.String("token_id", aid))
			return ""
		}

		if token == OpenbaoMask && s.bao != nil {
			baoData, err := s.bao.KVGet(ctx, fmt.Sprintf("flume/github_tokens/%s", aid))
			if err == nil && baoData != nil {
				if delegated, ok := baoData["token"].(string); ok && strings.TrimSpace(delegated) != "" {
					token = strings.TrimSpace(delegated)
				}
			}
		}
		return token
	}
	return ""
}

// ListPublicTokens returns tokens safe for API responses (no raw PATs).
// Derived from Python: list_public_tokens().
func (s *GHTokenStore) ListPublicTokens(ctx context.Context) []map[string]interface{} {
	doc := s.LoadDocument(ctx)
	var out []map[string]interface{}
	for _, c := range doc.Tokens {
		tid := strings.TrimSpace(c.ID)
		if tid == "" {
			continue
		}
		tok := strings.TrimSpace(c.Token)
		out = append(out, map[string]interface{}{
			"id":          tid,
			"label":       strings.TrimSpace(c.Label),
			"tokenSuffix": keySuffix(tok),
			"hasToken":    tok != "",
		})
	}
	return out
}

// ApplyAction handles GitHub token CRUD from API.
// Derived from Python: apply_action().
func (s *GHTokenStore) ApplyAction(ctx context.Context, body map[string]interface{}) (bool, string) {
	action, _ := body["action"].(string)
	action = strings.TrimSpace(strings.ToLower(action))

	doc := s.LoadDocument(ctx)

	switch action {
	case GHActionDelete:
		return s.handleDelete(ctx, doc, body)
	case GHActionSetActive:
		return s.handleSetActive(ctx, doc, body)
	case GHActionUpsert:
		return s.handleUpsert(ctx, doc, body)
	default:
		return false, "githubTokenAction.action must be upsert, delete, or setActive"
	}
}

func (s *GHTokenStore) handleDelete(ctx context.Context, doc *GHMetadataDoc, body map[string]interface{}) (bool, string) {
	cid := strings.TrimSpace(strVal(body["id"]))
	if cid == "" {
		return false, "id is required"
	}

	newToks := make([]GHCredential, 0, len(doc.Tokens))
	for _, c := range doc.Tokens {
		if c.ID != cid {
			newToks = append(newToks, c)
		}
	}
	if len(newToks) == len(doc.Tokens) {
		return false, "GitHub token not found"
	}

	doc.Tokens = newToks
	if doc.ActiveTokenID == cid {
		if len(newToks) > 0 {
			doc.ActiveTokenID = strings.TrimSpace(newToks[0].ID)
		} else {
			doc.ActiveTokenID = ""
		}
	}

	// Clear OpenBao secret
	if s.bao != nil {
		_ = s.bao.KVDelete(ctx, fmt.Sprintf("flume/github_tokens/%s", cid))
	}

	s.SaveDocument(ctx, doc)
	return true, ""
}

func (s *GHTokenStore) handleSetActive(ctx context.Context, doc *GHMetadataDoc, body map[string]interface{}) (bool, string) {
	cid := strings.TrimSpace(strVal(body["id"]))
	if cid == "" {
		return false, "id is required"
	}

	var row *GHCredential
	for i := range doc.Tokens {
		if doc.Tokens[i].ID == cid {
			row = &doc.Tokens[i]
			break
		}
	}
	if row == nil {
		return false, "GitHub token not found"
	}
	if strings.TrimSpace(row.Token) == "" {
		return false, "Token has no secret — paste a PAT before setting active"
	}

	doc.ActiveTokenID = cid
	s.SaveDocument(ctx, doc)
	return true, ""
}

func (s *GHTokenStore) handleUpsert(ctx context.Context, doc *GHMetadataDoc, body map[string]interface{}) (bool, string) {
	label := strings.TrimSpace(strVal(body["label"]))
	credID := strings.TrimSpace(strVal(body["id"]))
	tokenIn := strings.TrimSpace(strVal(body["token"]))

	if credID != "" {
		// Update existing
		var row *GHCredential
		for i := range doc.Tokens {
			if doc.Tokens[i].ID == credID {
				row = &doc.Tokens[i]
				break
			}
		}
		if row == nil {
			return false, "GitHub token not found"
		}
		if label != "" {
			if ghLabelTaken(doc.Tokens, label, credID) {
				return false, fmt.Sprintf("Another token is already labeled %q", label)
			}
			row.Label = label
		}
		if tokenIn != "" && tokenIn != "***" {
			if !ValidateGitHubToken(tokenIn) {
				return false, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
			}
			row.Token = tokenIn
		}
		if doc.ActiveTokenID == "" && strings.TrimSpace(row.Token) != "" {
			doc.ActiveTokenID = credID
		}
		if tokenIn != "" && tokenIn != "***" && s.bao != nil {
			_ = s.bao.KVPut(ctx, fmt.Sprintf("flume/github_tokens/%s", credID), map[string]interface{}{"token": tokenIn})
		}
		s.SaveDocument(ctx, doc)
		return true, ""
	}

	// New credential
	if label == "" {
		label = DefaultGHLabel
	}
	if ghLabelTaken(doc.Tokens, label, "") {
		return false, fmt.Sprintf("Another token is already labeled %q", label)
	}
	if tokenIn == "" || tokenIn == "***" {
		return false, "token is required for new GitHub PATs"
	}
	if !ValidateGitHubToken(tokenIn) {
		return false, "Invalid GitHub token format. Must begin with ghp_, github_pat_, ghs_, gho_, or ghu_."
	}

	newID := GenerateShortID()
	doc.Tokens = append(doc.Tokens, GHCredential{ID: newID, Label: label, Token: tokenIn})
	if doc.ActiveTokenID == "" {
		doc.ActiveTokenID = newID
	}
	if s.bao != nil {
		_ = s.bao.KVPut(ctx, fmt.Sprintf("flume/github_tokens/%s", newID), map[string]interface{}{"token": tokenIn})
	}
	s.SaveDocument(ctx, doc)
	return true, ""
}

func ghLabelTaken(tokens []GHCredential, label, excludeID string) bool {
	ll := strings.TrimSpace(strings.ToLower(label))
	if ll == "" {
		return false
	}
	ex := strings.TrimSpace(excludeID)
	for _, c := range tokens {
		cid := strings.TrimSpace(c.ID)
		if ex != "" && cid == ex {
			continue
		}
		if strings.TrimSpace(strings.ToLower(c.Label)) == ll {
			return true
		}
	}
	return false
}

// ─── Marshalling ────────────────────────────────────────────────────────────

func docToGHMetadata(raw map[string]interface{}) *GHMetadataDoc {
	doc := DefaultGHDoc()
	if v, ok := raw["version"].(float64); ok {
		doc.Version = int(v)
	}
	if v, ok := raw["activeTokenId"].(string); ok {
		doc.ActiveTokenID = v
	}
	if toks, ok := raw["tokens"].([]interface{}); ok {
		for _, t := range toks {
			tm, _ := t.(map[string]interface{})
			if tm == nil {
				continue
			}
			doc.Tokens = append(doc.Tokens, GHCredential{
				ID:    strVal(tm["id"]),
				Label: strVal(tm["label"]),
				Token: strVal(tm["token"]),
			})
		}
	}
	return doc
}

func ghMetadataToDoc(doc *GHMetadataDoc) map[string]interface{} {
	toks := make([]interface{}, len(doc.Tokens))
	for i, t := range doc.Tokens {
		toks[i] = map[string]interface{}{
			"id":    t.ID,
			"label": t.Label,
			"token": t.Token,
		}
	}
	return map[string]interface{}{
		"version":       doc.Version,
		"activeTokenId": doc.ActiveTokenID,
		"tokens":        toks,
	}
}
