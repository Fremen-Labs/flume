package ui

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"time"
)

// FlumeClient is a lightweight HTTP client for the Flume dashboard API.
// URL resolution follows Kubernetes service discovery conventions:
//
//  1. FLUME_DASHBOARD_SERVICE_HOST + FLUME_DASHBOARD_SERVICE_PORT env vars
//     (matches what K8s injects into every pod automatically)
//  2. Docker Compose DNS: http://flume-dashboard:8765 (probed with 200 ms timeout)
//     (mirrors K8s <svc>.<ns>.svc.cluster.local DNS resolution)
//  3. Native localhost fallback: http://localhost:8765
type FlumeClient struct {
	BaseURL    string
	httpClient *http.Client
}

// NewFlumeClient resolves the dashboard URL and returns a ready client.
func NewFlumeClient() *FlumeClient {
	return &FlumeClient{
		BaseURL:    resolveDashboardURL(),
		httpClient: &http.Client{Timeout: 10 * time.Second},
	}
}

// resolveDashboardURL implements the 3-tier K8s-grade resolution chain.
func resolveDashboardURL() string {
	// Tier 1 — Explicit K8s-style env injection (works for K8s and Docker Compose).
	host := os.Getenv("FLUME_DASHBOARD_SERVICE_HOST")
	port := os.Getenv("FLUME_DASHBOARD_SERVICE_PORT")
	if host != "" && port != "" {
		return fmt.Sprintf("http://%s:%s", host, port)
	}

	// Tier 2 — Docker Compose DNS probe (service name == hostname, same as K8s DNS).
	probe := &http.Client{Timeout: 200 * time.Millisecond}
	if resp, err := probe.Get("http://flume-dashboard:8765/api/health"); err == nil {
		resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			return "http://flume-dashboard:8765"
		}
	}

	// Tier 3 — Native localhost fallback.
	return "http://localhost:8765"
}

// Get performs a GET request and decodes the JSON response.
// path is joined to BaseURL using url.JoinPath to prevent traversal attacks.
func (c *FlumeClient) Get(path string) (map[string]any, error) {
	target, err := url.JoinPath(c.BaseURL, path)
	if err != nil {
		return nil, fmt.Errorf("invalid API path '%s': %w", path, err)
	}
	resp, err := c.httpClient.Get(target)
	if err != nil {
		return nil, fmt.Errorf("dashboard unreachable at %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("invalid JSON from dashboard: %w", err)
	}
	return out, nil
}

// GetRaw performs a GET and returns the raw response bytes (used for logs/diffs).
func (c *FlumeClient) GetRaw(path string) ([]byte, error) {
	target, err := url.JoinPath(c.BaseURL, path)
	if err != nil {
		return nil, fmt.Errorf("invalid API path '%s': %w", path, err)
	}
	resp, err := c.httpClient.Get(target)
	if err != nil {
		return nil, fmt.Errorf("dashboard unreachable at %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

// Post performs a POST with a JSON body and decodes the response.
func (c *FlumeClient) Post(path string, body any) (map[string]any, error) {
	target, err := url.JoinPath(c.BaseURL, path)
	if err != nil {
		return nil, fmt.Errorf("invalid API path '%s': %w", path, err)
	}
	b, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request body: %w", err)
	}
	resp, err := c.httpClient.Post(target, "application/json", bytes.NewReader(b))
	if err != nil {
		return nil, fmt.Errorf("dashboard unreachable at %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("invalid JSON from dashboard: %w", err)
	}
	return out, nil
}

// Put performs a PUT with a JSON body and decodes the response.
func (c *FlumeClient) Put(path string, body any) (map[string]any, error) {
	target, err := url.JoinPath(c.BaseURL, path)
	if err != nil {
		return nil, fmt.Errorf("invalid API path '%s': %w", path, err)
	}
	b, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request body: %w", err)
	}
	req, err := http.NewRequest(http.MethodPut, target, bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("dashboard unreachable at %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("invalid JSON from dashboard: %w", err)
	}
	return out, nil
}
