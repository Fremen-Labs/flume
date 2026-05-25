// ado.go — Azure DevOps token CRUD backed by ES metadata + OpenBao secrets.
package secrets

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
)

const (
	ADODeletedMask     = "***"
	DefaultADOLabel    = "Azure DevOps Token"
	ADOActionDelete    = "delete"
	ADOActionSetActive = "setActive"
	ADOActionUpsert    = "upsert"
)

// ADOCredential represents a saved ADO organization URL and token.
type ADOCredential struct {
	ID     string `json:"id"`
	Label  string `json:"label"`
	OrgURL string `json:"orgUrl"`
	Token  string `json:"token"`
}

// ADOMetadataDoc is the singleton document in flume-ado-tokens.
type ADOMetadataDoc struct {
	Version       int             `json:"version"`
	ActiveTokenID string          `json:"activeTokenId"`
	Tokens        []ADOCredential `json:"tokens"`
}

// DefaultADODoc returns an empty metadata document.
func DefaultADODoc() *ADOMetadataDoc {
	return &ADOMetadataDoc{
		Version: 1,
		Tokens:  []ADOCredential{},
	}
}

// ADOTokenStore manages Azure DevOps tokens via ES + OpenBao.
type ADOTokenStore struct {
	esStore *ESStore
	bao     *OpenBaoClient
	logger  *slog.Logger
}

// NewADOTokenStore creates a new ADO token store.
func NewADOTokenStore(esStore *ESStore, bao *OpenBaoClient, logger *slog.Logger) *ADOTokenStore {
	if logger == nil {
		logger = slog.Default()
	}
	return &ADOTokenStore{esStore: esStore, bao: bao, logger: logger}
}

// LoadDocument loads ADO token metadata from ES.
func (s *ADOTokenStore) LoadDocument(ctx context.Context) *ADOMetadataDoc {
	raw, err := s.esStore.LoadADOTokens(ctx)
	if err != nil || raw == nil {
		return DefaultADODoc()
	}
	return docToADOMetadata(raw)
}

// SaveDocument persists ADO token metadata to ES.
func (s *ADOTokenStore) SaveDocument(ctx context.Context, doc *ADOMetadataDoc) {
	masked := *doc
	maskedTokens := make([]ADOCredential, len(doc.Tokens))
	for i, c := range doc.Tokens {
		maskedTokens[i] = c
		tok := strings.TrimSpace(c.Token)
		if tok != "" && tok != "***" && tok != OpenbaoMask {
			maskedTokens[i].Token = OpenbaoMask
		}
	}
	masked.Tokens = maskedTokens

	raw := adoMetadataToDoc(&masked)
	if err := s.esStore.SaveADOTokens(ctx, raw); err != nil {
		s.logger.Error("failed to persist ADO tokens to ES", slog.String("error", err.Error()))
	}
}

// GetActiveTokenID returns the active token ID.
func (s *ADOTokenStore) GetActiveTokenID(ctx context.Context) string {
	return strings.TrimSpace(s.LoadDocument(ctx).ActiveTokenID)
}

// GetActiveTokenPlain returns the plaintext active PAT, resolving from OpenBao if delegated.
func (s *ADOTokenStore) GetActiveTokenPlain(ctx context.Context) string {
	doc := s.LoadDocument(ctx)
	aid := strings.TrimSpace(doc.ActiveTokenID)
	for _, c := range doc.Tokens {
		if strings.TrimSpace(c.ID) != aid {
			continue
		}
		token := strings.TrimSpace(c.Token)
		if token == OpenbaoMask && s.bao != nil {
			baoData, err := s.bao.KVGet(ctx, fmt.Sprintf("flume/ado_tokens/%s", aid))
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
func (s *ADOTokenStore) ListPublicTokens(ctx context.Context) []map[string]interface{} {
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
			"orgUrl":      strings.TrimSpace(c.OrgURL),
			"tokenSuffix": keySuffix(tok),
			"hasToken":    tok != "",
		})
	}
	return out
}

// ApplyAction handles ADO token CRUD from API.
func (s *ADOTokenStore) ApplyAction(ctx context.Context, body map[string]interface{}) (bool, string) {
	action, _ := body["action"].(string)
	action = strings.TrimSpace(strings.ToLower(action))

	doc := s.LoadDocument(ctx)

	switch action {
	case ADOActionDelete:
		return s.handleDelete(ctx, doc, body)
	case ADOActionSetActive:
		return s.handleSetActive(ctx, doc, body)
	case ADOActionUpsert:
		return s.handleUpsert(ctx, doc, body)
	default:
		return false, "adoTokenAction.action must be upsert, delete, or setActive"
	}
}

func (s *ADOTokenStore) handleDelete(ctx context.Context, doc *ADOMetadataDoc, body map[string]interface{}) (bool, string) {
	cid := strings.TrimSpace(strVal(body["id"]))
	if cid == "" {
		return false, "id is required"
	}

	newToks := make([]ADOCredential, 0, len(doc.Tokens))
	for _, c := range doc.Tokens {
		if c.ID != cid {
			newToks = append(newToks, c)
		}
	}
	if len(newToks) == len(doc.Tokens) {
		return false, "ADO token not found"
	}

	doc.Tokens = newToks
	if doc.ActiveTokenID == cid {
		if len(newToks) > 0 {
			doc.ActiveTokenID = strings.TrimSpace(newToks[0].ID)
		} else {
			doc.ActiveTokenID = ""
		}
	}

	if s.bao != nil {
		_ = s.bao.KVDelete(ctx, fmt.Sprintf("flume/ado_tokens/%s", cid))
	}

	s.SaveDocument(ctx, doc)
	return true, ""
}

func (s *ADOTokenStore) handleSetActive(ctx context.Context, doc *ADOMetadataDoc, body map[string]interface{}) (bool, string) {
	cid := strings.TrimSpace(strVal(body["id"]))
	if cid == "" {
		return false, "id is required"
	}

	var row *ADOCredential
	for i := range doc.Tokens {
		if doc.Tokens[i].ID == cid {
			row = &doc.Tokens[i]
			break
		}
	}
	if row == nil {
		return false, "ADO token not found"
	}
	if strings.TrimSpace(row.Token) == "" {
		return false, "Token has no secret — paste a PAT before setting active"
	}

	doc.ActiveTokenID = cid
	s.SaveDocument(ctx, doc)
	return true, ""
}

func (s *ADOTokenStore) handleUpsert(ctx context.Context, doc *ADOMetadataDoc, body map[string]interface{}) (bool, string) {
	label := strings.TrimSpace(strVal(body["label"]))
	credID := strings.TrimSpace(strVal(body["id"]))
	orgURL := strings.TrimSpace(strVal(body["orgUrl"]))
	tokenIn := strings.TrimSpace(strVal(body["token"]))

	if credID != "" {
		// Update existing
		var row *ADOCredential
		for i := range doc.Tokens {
			if doc.Tokens[i].ID == credID {
				row = &doc.Tokens[i]
				break
			}
		}
		if row == nil {
			return false, "ADO token not found"
		}
		if label != "" {
			if adoLabelTaken(doc.Tokens, label, credID) {
				return false, fmt.Sprintf("Another token is already labeled %q", label)
			}
			row.Label = label
		}
		if orgURL != "" {
			row.OrgURL = orgURL
		}
		if tokenIn != "" && tokenIn != "***" {
			row.Token = tokenIn
		}
		if doc.ActiveTokenID == "" && strings.TrimSpace(row.Token) != "" {
			doc.ActiveTokenID = credID
		}
		if tokenIn != "" && tokenIn != "***" && s.bao != nil {
			_ = s.bao.KVPut(ctx, fmt.Sprintf("flume/ado_tokens/%s", credID), map[string]interface{}{"token": tokenIn})
		}
		s.SaveDocument(ctx, doc)
		return true, ""
	}

	// New credential
	if label == "" {
		label = DefaultADOLabel
	}
	if adoLabelTaken(doc.Tokens, label, "") {
		return false, fmt.Sprintf("Another token is already labeled %q", label)
	}
	if tokenIn == "" || tokenIn == "***" {
		return false, "token is required for new ADO PATs"
	}

	newID := GenerateShortID()
	doc.Tokens = append(doc.Tokens, ADOCredential{ID: newID, Label: label, OrgURL: orgURL, Token: tokenIn})
	if doc.ActiveTokenID == "" {
		doc.ActiveTokenID = newID
	}
	if s.bao != nil {
		_ = s.bao.KVPut(ctx, fmt.Sprintf("flume/ado_tokens/%s", newID), map[string]interface{}{"token": tokenIn})
	}
	s.SaveDocument(ctx, doc)
	return true, ""
}

func adoLabelTaken(tokens []ADOCredential, label, excludeID string) bool {
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

func docToADOMetadata(raw map[string]interface{}) *ADOMetadataDoc {
	doc := DefaultADODoc()
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
			doc.Tokens = append(doc.Tokens, ADOCredential{
				ID:     strVal(tm["id"]),
				Label:  strVal(tm["label"]),
				OrgURL: strVal(tm["orgUrl"]),
				Token:  strVal(tm["token"]),
			})
		}
	}
	return doc
}

func adoMetadataToDoc(doc *ADOMetadataDoc) map[string]interface{} {
	toks := make([]interface{}, len(doc.Tokens))
	for i, t := range doc.Tokens {
		toks[i] = map[string]interface{}{
			"id":     t.ID,
			"label":  t.Label,
			"orgUrl": t.OrgURL,
			"token":  t.Token,
		}
	}
	return map[string]interface{}{
		"version":       doc.Version,
		"activeTokenId": doc.ActiveTokenID,
		"tokens":        toks,
	}
}
