// Package es provides a typed Elasticsearch client for Flume.
//
// Replaces Python:
//   - dashboard/core/elasticsearch.py — _ESConfig, BulkFlusher (71 nodes)
//   - worker-manager/es/queries.py — count_available_by_status (1 node)
//   - worker-manager/es/telemetry.py — flush_telemetry, log_task_state_transition (2 nodes)
//   - es_credential_store.py — _request, _index_exists, ensure_credential_indices (6 nodes)
//
// Uses net/http directly rather than a third-party ES client library to minimize
// dependencies. The Flume ES usage patterns are simple enough (CRUD + _bulk + _search)
// that a typed wrapper over HTTP provides better control and debuggability.
package es

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Client wraps Elasticsearch HTTP operations with typed helpers.
type Client struct {
	baseURL    string
	apiKey     string
	httpClient *http.Client
	logger     *slog.Logger
}

// New creates a new Elasticsearch client.
func New(baseURL, apiKey string, logger *slog.Logger) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		apiKey:  apiKey,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
		logger: logger,
	}
}

// do executes an HTTP request against Elasticsearch with auth headers.
func (c *Client) do(ctx context.Context, method, path string, body io.Reader) (*http.Response, error) {
	url := fmt.Sprintf("%s/%s", c.baseURL, strings.TrimLeft(path, "/"))
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return nil, fmt.Errorf("es: request build failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if c.apiKey != "" {
		req.Header.Set("Authorization", "ApiKey "+c.apiKey)
	}
	return c.httpClient.Do(req)
}

// ─── Document Operations ────────────────────────────────────────────────────

// GetDoc retrieves a document by index and ID.
// Returns the _source as raw JSON. Returns nil if not found (404).
func (c *Client) GetDoc(ctx context.Context, index, id string) (json.RawMessage, error) {
	resp, err := c.do(ctx, http.MethodGet, fmt.Sprintf("%s/_doc/%s", index, id), nil)
	if err != nil {
		return nil, fmt.Errorf("es: get %s/%s failed: %w", index, id, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 404 {
		return nil, nil
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("es: get %s/%s returned HTTP %d", index, id, resp.StatusCode)
	}

	var doc struct {
		Source json.RawMessage `json:"_source"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, fmt.Errorf("es: decode %s/%s failed: %w", index, id, err)
	}
	return doc.Source, nil
}

// IndexDoc indexes a document (create or overwrite).
func (c *Client) IndexDoc(ctx context.Context, index, id string, body interface{}) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("es: marshal failed: %w", err)
	}

	path := fmt.Sprintf("%s/_doc", index)
	if id != "" {
		path = fmt.Sprintf("%s/_doc/%s", index, id)
	}

	resp, err := c.do(ctx, http.MethodPut, path, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("es: index %s/%s failed: %w", index, id, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("es: index %s/%s returned HTTP %d: %s", index, id, resp.StatusCode, string(respBody))
	}
	return nil
}

// DeleteDoc deletes a document by index and ID.
func (c *Client) DeleteDoc(ctx context.Context, index, id string) error {
	resp, err := c.do(ctx, http.MethodDelete, fmt.Sprintf("%s/_doc/%s", index, id), nil)
	if err != nil {
		return fmt.Errorf("es: delete %s/%s failed: %w", index, id, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 && resp.StatusCode != 404 {
		return fmt.Errorf("es: delete %s/%s returned HTTP %d", index, id, resp.StatusCode)
	}
	return nil
}

// ─── Search ─────────────────────────────────────────────────────────────────

// SearchResult is the parsed response from an ES _search query.
type SearchResult struct {
	Total int               `json:"total"`
	Hits  []json.RawMessage `json:"hits"`
}

// Search executes a search query and returns hits.
func (c *Client) Search(ctx context.Context, index string, query interface{}, size int) (*SearchResult, error) {
	body := map[string]interface{}{
		"query": query,
		"size":  size,
	}
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("es: search marshal failed: %w", err)
	}

	resp, err := c.do(ctx, http.MethodPost, fmt.Sprintf("%s/_search", index), bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("es: search %s failed: %w", index, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("es: search %s returned HTTP %d: %s", index, resp.StatusCode, string(respBody))
	}

	var esResp struct {
		Hits struct {
			Total struct {
				Value int `json:"value"`
			} `json:"total"`
			Hits []struct {
				Source json.RawMessage `json:"_source"`
			} `json:"hits"`
		} `json:"hits"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&esResp); err != nil {
		return nil, fmt.Errorf("es: search decode failed: %w", err)
	}

	result := &SearchResult{Total: esResp.Hits.Total.Value}
	for _, h := range esResp.Hits.Hits {
		result.Hits = append(result.Hits, h.Source)
	}
	return result, nil
}

// Count returns the document count matching a query.
// Derived from Python: count_available_by_status() in worker-manager/es/queries.py.
func (c *Client) Count(ctx context.Context, index string, query interface{}) (int, error) {
	data, err := json.Marshal(map[string]interface{}{"query": query})
	if err != nil {
		return 0, fmt.Errorf("es: count marshal failed: %w", err)
	}

	resp, err := c.do(ctx, http.MethodPost, fmt.Sprintf("%s/_count", index), bytes.NewReader(data))
	if err != nil {
		return 0, fmt.Errorf("es: count %s failed: %w", index, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return 0, fmt.Errorf("es: count %s returned HTTP %d", index, resp.StatusCode)
	}

	var countResp struct {
		Count int `json:"count"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&countResp); err != nil {
		return 0, fmt.Errorf("es: count decode failed: %w", err)
	}
	return countResp.Count, nil
}

// ─── Index Management ───────────────────────────────────────────────────────

// IndexExists checks if an index exists.
// Derived from Python: es_credential_store._index_exists().
func (c *Client) IndexExists(ctx context.Context, index string) (bool, error) {
	resp, err := c.do(ctx, http.MethodHead, index, nil)
	if err != nil {
		return false, fmt.Errorf("es: head %s failed: %w", index, err)
	}
	defer resp.Body.Close()
	return resp.StatusCode == 200, nil
}

// EnsureIndex creates an index if it doesn't exist.
func (c *Client) EnsureIndex(ctx context.Context, index string, mapping interface{}) error {
	exists, err := c.IndexExists(ctx, index)
	if err != nil {
		return err
	}
	if exists {
		return nil
	}

	var body io.Reader
	if mapping != nil {
		data, err := json.Marshal(mapping)
		if err != nil {
			return fmt.Errorf("es: mapping marshal failed: %w", err)
		}
		body = bytes.NewReader(data)
	}

	resp, err := c.do(ctx, http.MethodPut, index, body)
	if err != nil {
		return fmt.Errorf("es: create index %s failed: %w", index, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("es: create index %s returned HTTP %d: %s", index, resp.StatusCode, string(respBody))
	}
	c.logger.Info("ES index created", slog.String("index", index))
	return nil
}

// ─── Bulk Flusher ───────────────────────────────────────────────────────────

// BulkFlusher batches ES operations and flushes them periodically or when
// the buffer reaches maxSize. Replaces Python's BulkFlusher class.
// Derived from Python: dashboard/core/elasticsearch.BulkFlusher (2 nodes).
type BulkFlusher struct {
	client       *Client
	mu           sync.Mutex
	buffer       []string // NDJSON lines
	maxSize      int
	flushTicker  *time.Ticker
	stopCh       chan struct{}
}

// NewBulkFlusher creates a new bulk flusher.
func NewBulkFlusher(client *Client, maxSize int, interval time.Duration) *BulkFlusher {
	bf := &BulkFlusher{
		client:      client,
		buffer:      make([]string, 0, maxSize),
		maxSize:     maxSize,
		flushTicker: time.NewTicker(interval),
		stopCh:      make(chan struct{}),
	}
	go bf.backgroundFlush()
	return bf
}

// Add appends an action + document pair to the buffer.
// Flushes automatically when maxSize is reached.
func (bf *BulkFlusher) Add(ctx context.Context, action, doc string) {
	bf.mu.Lock()
	bf.buffer = append(bf.buffer, action, doc)
	shouldFlush := len(bf.buffer) >= bf.maxSize*2
	bf.mu.Unlock()

	if shouldFlush {
		bf.Flush(ctx)
	}
}

// Flush sends all buffered operations to ES.
func (bf *BulkFlusher) Flush(ctx context.Context) {
	bf.mu.Lock()
	if len(bf.buffer) == 0 {
		bf.mu.Unlock()
		return
	}
	batch := bf.buffer
	bf.buffer = make([]string, 0, bf.maxSize)
	bf.mu.Unlock()

	body := strings.Join(batch, "\n") + "\n"
	resp, err := bf.client.do(ctx, http.MethodPost, "_bulk", strings.NewReader(body))
	if err != nil {
		bf.client.logger.Error("ES Bulk HTTP Flush failed", slog.String("error", err.Error()))
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		bf.client.logger.Error("ES Bulk flush returned error", slog.Int("status", resp.StatusCode))
	}
}

func (bf *BulkFlusher) backgroundFlush() {
	for {
		select {
		case <-bf.flushTicker.C:
			bf.Flush(context.Background())
		case <-bf.stopCh:
			bf.flushTicker.Stop()
			bf.Flush(context.Background()) // final drain
			return
		}
	}
}

// Stop halts the background flusher and drains the buffer.
func (bf *BulkFlusher) Stop() {
	close(bf.stopCh)
}

// ─── Raw Search ─────────────────────────────────────────────────────────────

// SearchRaw executes a raw search query and returns the full ES response.
// Used when the caller needs access to _id, _score, aggregations, etc.
func (c *Client) SearchRaw(ctx context.Context, index string, body interface{}) (map[string]interface{}, error) {
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("es: search raw marshal failed: %w", err)
	}

	resp, err := c.do(ctx, http.MethodPost, fmt.Sprintf("%s/_search", index), bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("es: search raw %s failed: %w", index, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("es: search raw %s returned HTTP %d: %s", index, resp.StatusCode, string(respBody))
	}

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("es: search raw decode failed: %w", err)
	}
	return result, nil
}

// ─── Post ───────────────────────────────────────────────────────────────────

// Post sends a POST request to the specified ES path with a JSON body.
// Used for _update, _doc, and other POST-based ES APIs.
func (c *Client) Post(ctx context.Context, path string, body interface{}) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("es: post marshal failed: %w", err)
	}

	resp, err := c.do(ctx, http.MethodPost, path, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("es: post %s failed: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("es: post %s returned HTTP %d: %s", path, resp.StatusCode, string(respBody))
	}
	return nil
}

// ─── Generic HTTP Proxy Methods ─────────────────────────────────────────────
// These methods allow the dashboard to proxy requests to arbitrary URLs
// (e.g., the Go gateway, Vault, Exo topology).

// HTTPGet performs a GET request to an arbitrary URL and returns decoded JSON.
func (c *Client) HTTPGet(ctx context.Context, url string) (map[string]interface{}, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("http get: build failed: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http get %s failed: %w", url, err)
	}
	defer resp.Body.Close()

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("http get decode failed: %w", err)
	}
	return result, nil
}

// HTTPPost performs a POST request to an arbitrary URL with a JSON body.
func (c *Client) HTTPPost(ctx context.Context, url string, body interface{}) (map[string]interface{}, error) {
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("http post: marshal failed: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("http post: build failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http post %s failed: %w", url, err)
	}
	defer resp.Body.Close()

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("http post decode failed: %w", err)
	}
	return result, nil
}

// HTTPPut performs a PUT request to an arbitrary URL with a JSON body.
func (c *Client) HTTPPut(ctx context.Context, url string, body interface{}) (map[string]interface{}, error) {
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("http put: marshal failed: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("http put: build failed: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http put %s failed: %w", url, err)
	}
	defer resp.Body.Close()

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("http put decode failed: %w", err)
	}
	return result, nil
}

// HTTPDelete performs a DELETE request to an arbitrary URL.
func (c *Client) HTTPDelete(ctx context.Context, url string) (map[string]interface{}, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, url, nil)
	if err != nil {
		return nil, fmt.Errorf("http delete: build failed: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http delete %s failed: %w", url, err)
	}
	defer resp.Body.Close()

	var result map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("http delete decode failed: %w", err)
	}
	return result, nil
}

// ─── Health ─────────────────────────────────────────────────────────────────

// Ping checks if the ES cluster is reachable.
func (c *Client) Ping(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	resp, err := c.do(ctx, http.MethodGet, "", nil)
	if err != nil {
		return fmt.Errorf("es: ping failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("es: ping returned HTTP %d", resp.StatusCode)
	}
	return nil
}
