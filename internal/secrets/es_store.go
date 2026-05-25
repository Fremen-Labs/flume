// es_store.go — Elasticsearch-backed credential metadata storage.
//
// Direct port of Python: es_credential_store.py (6 AST nodes).
// Non-sensitive config → ES index; secrets → OpenBao KV.
//
// Architecture:
//   - credential labels, providers, active status → Elasticsearch
//   - API keys, PATs, OAuth tokens → OpenBao KV-V2
//   - API keys are scrubbed to "***OPENBAO_DELEGATED***" before ES write
package secrets

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

// ES index names — mirrors Python constants.
const (
	IndexLLMConfig      = "flume-llm-config"
	IndexLLMCredentials = "flume-llm-credentials"
	IndexADOTokens      = "flume-ado-tokens"
	IndexGHTokens       = "flume-github-tokens"

	// Singleton document ID inside each index.
	docID = "singleton"
)

// OpenbaoMask is the placeholder written to ES for secrets stored in OpenBao.
const OpenbaoMask = "***OPENBAO_DELEGATED***"

// ESStore provides credential metadata persistence via Elasticsearch.
// Derived from Python: es_credential_store.py module.
type ESStore struct {
	esURL      string
	esAPIKey   string
	esPassword string
	httpClient *http.Client
	logger     *slog.Logger
}

// NewESStore creates a new ES credential store.
func NewESStore(logger *slog.Logger) *ESStore {
	if logger == nil {
		logger = slog.Default()
	}
	esURL := os.Getenv("ES_URL")
	if esURL == "" {
		esURL = "http://elasticsearch:9200"
	}
	esURL = strings.TrimRight(esURL, "/")

	esAPIKey := os.Getenv("ES_API_KEY")
	esPassword := ""
	if esAPIKey == "" {
		esPassword = os.Getenv("FLUME_ELASTIC_PASSWORD")
	}

	return &ESStore{
		esURL:      esURL,
		esAPIKey:   esAPIKey,
		esPassword: esPassword,
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec
			},
		},
		logger: logger,
	}
}

// request executes an ES request and returns parsed JSON.
// Derived from Python: _request().
func (s *ESStore) request(ctx context.Context, method, path string, body interface{}) (map[string]interface{}, error) {
	url := s.esURL + path
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("es_store: marshal failed: %w", err)
		}
		bodyReader = strings.NewReader(string(data))
	}

	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("es_store: request build failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if s.esAPIKey != "" {
		req.Header.Set("Authorization", "ApiKey "+s.esAPIKey)
	} else if s.esPassword != "" {
		req.SetBasicAuth("elastic", s.esPassword)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		s.logger.Warn("ES request error", slog.String("method", method), slog.String("path", path), slog.String("error", err.Error()))
		return nil, err
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 404 {
		return nil, nil
	}
	if resp.StatusCode >= 300 {
		s.logger.Warn("ES request failed",
			slog.String("method", method),
			slog.String("path", path),
			slog.Int("status", resp.StatusCode),
			slog.String("body", truncateStr(string(respBody), 200)),
		)
		return nil, fmt.Errorf("es_store: %s %s returned HTTP %d", method, path, resp.StatusCode)
	}

	if len(respBody) == 0 {
		return map[string]interface{}{}, nil
	}

	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("es_store: decode failed: %w", err)
	}
	return result, nil
}

// IndexExists checks whether an ES index exists.
// Derived from Python: _index_exists().
func (s *ESStore) IndexExists(ctx context.Context, index string) bool {
	url := fmt.Sprintf("%s/%s", s.esURL, index)
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, url, nil)
	if err != nil {
		return false
	}
	if s.esAPIKey != "" {
		req.Header.Set("Authorization", "ApiKey "+s.esAPIKey)
	} else if s.esPassword != "" {
		req.SetBasicAuth("elastic", s.esPassword)
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == 200
}

// EnsureCredentialIndices verifies that credential metadata indices exist.
// Derived from Python: ensure_credential_indices().
func (s *ESStore) EnsureCredentialIndices(ctx context.Context) {
	indices := []string{IndexLLMConfig, IndexLLMCredentials, IndexADOTokens, IndexGHTokens}
	for _, index := range indices {
		if s.IndexExists(ctx, index) {
			s.logger.Debug("ES credential index verified", slog.String("index", index))
		} else {
			s.logger.Warn("ES credential index missing — expected to be pre-created by `flume start`",
				slog.String("index", index))
		}
	}
}

// ─── Generic Load/Save ──────────────────────────────────────────────────────

// LoadFromES loads the singleton metadata document from an ES index.
// Derived from Python: load_from_es().
func (s *ESStore) LoadFromES(ctx context.Context, index string) (map[string]interface{}, error) {
	result, err := s.request(ctx, http.MethodGet, fmt.Sprintf("/%s/_doc/%s", index, docID), nil)
	if err != nil || result == nil {
		return nil, err
	}
	if found, ok := result["found"].(bool); ok && found {
		if src, ok := result["_source"].(map[string]interface{}); ok {
			return src, nil
		}
	}
	return nil, nil
}

// SaveToES writes a secret-scrubbed metadata document to ES.
// Derived from Python: save_to_es().
func (s *ESStore) SaveToES(ctx context.Context, index string, doc map[string]interface{}) error {
	safe := scrubSecrets(doc)
	_, err := s.request(ctx, http.MethodPut, fmt.Sprintf("/%s/_doc/%s", index, docID), safe)
	return err
}

// ─── LLM Config (non-sensitive) ─────────────────────────────────────────────

// LLMConfigSafeKeys are the only keys stored in ES for LLM config.
var LLMConfigSafeKeys = map[string]bool{
	"LLM_PROVIDER":  true,
	"LLM_MODEL":     true,
	"LLM_BASE_URL":  true,
	"LLM_ROUTE_TYPE": true,
}

// LoadLLMConfig loads non-sensitive LLM settings from ES.
// Derived from Python: load_llm_config().
func (s *ESStore) LoadLLMConfig(ctx context.Context) map[string]string {
	result, err := s.request(ctx, http.MethodGet, fmt.Sprintf("/%s/_doc/%s", IndexLLMConfig, docID), nil)
	if err != nil || result == nil {
		return nil
	}
	if found, ok := result["found"].(bool); ok && found {
		if src, ok := result["_source"].(map[string]interface{}); ok {
			out := make(map[string]string)
			for k, v := range src {
				if v != nil {
					out[k] = fmt.Sprintf("%v", v)
				}
			}
			return out
		}
	}
	return nil
}

// SaveLLMConfig persists non-sensitive LLM settings to ES.
// Derived from Python: save_llm_config().
func (s *ESStore) SaveLLMConfig(ctx context.Context, config map[string]string) bool {
	doc := make(map[string]string)
	for k, v := range config {
		if LLMConfigSafeKeys[k] && v != "" {
			doc[k] = v
		}
	}
	// Convert to interface map for request
	iDoc := make(map[string]interface{})
	for k, v := range doc {
		iDoc[k] = v
	}
	_, err := s.request(ctx, http.MethodPut, fmt.Sprintf("/%s/_doc/%s", IndexLLMConfig, docID), iDoc)
	return err == nil
}

// ─── Store-specific helpers ─────────────────────────────────────────────────

// LoadLLMCredentials loads LLM credential metadata from ES.
func (s *ESStore) LoadLLMCredentials(ctx context.Context) (map[string]interface{}, error) {
	return s.LoadFromES(ctx, IndexLLMCredentials)
}

// SaveLLMCredentials writes LLM credential metadata to ES.
func (s *ESStore) SaveLLMCredentials(ctx context.Context, doc map[string]interface{}) error {
	return s.SaveToES(ctx, IndexLLMCredentials, doc)
}

// LoadADOTokens loads ADO token metadata from ES.
func (s *ESStore) LoadADOTokens(ctx context.Context) (map[string]interface{}, error) {
	return s.LoadFromES(ctx, IndexADOTokens)
}

// SaveADOTokens writes ADO token metadata to ES.
func (s *ESStore) SaveADOTokens(ctx context.Context, doc map[string]interface{}) error {
	return s.SaveToES(ctx, IndexADOTokens, doc)
}

// LoadGHTokens loads GitHub token metadata from ES.
func (s *ESStore) LoadGHTokens(ctx context.Context) (map[string]interface{}, error) {
	return s.LoadFromES(ctx, IndexGHTokens)
}

// SaveGHTokens writes GitHub token metadata to ES.
func (s *ESStore) SaveGHTokens(ctx context.Context, doc map[string]interface{}) error {
	return s.SaveToES(ctx, IndexGHTokens, doc)
}

// ─── Secret Scrubbing ───────────────────────────────────────────────────────

// scrubSecrets strips actual secret values before writing to ES.
// Derived from Python: _scrub_secrets().
func scrubSecrets(doc map[string]interface{}) map[string]interface{} {
	scrubbed := make(map[string]interface{})
	for k, v := range doc {
		scrubbed[k] = v
	}

	secretFields := []string{"apiKey", "token", "pat", "password"}

	for _, listKey := range []string{"credentials", "tokens"} {
		items, ok := scrubbed[listKey].([]interface{})
		if !ok {
			continue
		}
		cleaned := make([]interface{}, 0, len(items))
		for _, item := range items {
			row, ok := item.(map[string]interface{})
			if !ok {
				cleaned = append(cleaned, item)
				continue
			}
			rowCopy := make(map[string]interface{})
			for k, v := range row {
				rowCopy[k] = v
			}
			for _, field := range secretFields {
				val, _ := rowCopy[field].(string)
				val = strings.TrimSpace(val)
				if val != "" && val != OpenbaoMask {
					rowCopy[field] = OpenbaoMask
				}
			}
			cleaned = append(cleaned, rowCopy)
		}
		scrubbed[listKey] = cleaned
	}

	return scrubbed
}

func truncateStr(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen]
}
