package orchestrator

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/charmbracelet/log"
)

const (
	seedMaxRetries    = 10
	seedRetryInterval = 2 * time.Second
)

func esRequest(esURL, apiKey, endpoint, method string, payload interface{}) ([]byte, int, error) {
	url := fmt.Sprintf("%s/%s", strings.TrimRight(esURL, "/"), strings.TrimLeft(endpoint, "/"))

	var reqBody io.Reader
	if payload != nil {
		bodyBytes, err := json.Marshal(payload)
		if err != nil {
			return nil, 0, err
		}
		reqBody = bytes.NewBuffer(bodyBytes)
	}

	req, err := http.NewRequest(method, url, reqBody)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if apiKey != "" {
		req.Header.Set("Authorization", "ApiKey "+apiKey)
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}

	if resp.StatusCode >= 400 && resp.StatusCode != 404 {
		return respBody, resp.StatusCode, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(respBody))
	}

	return respBody, resp.StatusCode, nil
}

func BootstrapElasticsearch(esURL, apiKey string) error {
	log.Info("[ELASTICSEARCH BOOTSTRAP] Bootstrapping Kubernetes-Grade Integration", "url", esURL)

	// 1. Create ILM Policy
	ilmPolicy := map[string]interface{}{
		"policy": map[string]interface{}{
			"phases": map[string]interface{}{
				"hot": map[string]interface{}{
					"actions": map[string]interface{}{
						"rollover": map[string]interface{}{
							"max_size": "30gb",
							"max_age":  "30d",
						},
					},
				},
				"warm": map[string]interface{}{
					"min_age": "30d",
					"actions": map[string]interface{}{
						"forcemerge": map[string]interface{}{
							"max_num_segments": 1,
						},
						"readonly": map[string]interface{}{},
					},
				},
			},
		},
	}

	_, _, err := esRequest(esURL, apiKey, "_ilm/policy/flume-task-records-policy", "PUT", ilmPolicy)
	if err != nil {
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to create ILM policy", "error", err)
	} else {
		log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ ILM Policy 'flume-task-records-policy' created")
	}

	// 2. Create Index Template mapping pattern to ILM policy (Enforcing STRICT keyword matching to avoid token splits)
	template := map[string]interface{}{
		"index_patterns": []string{"agent-task-records-*"},
		"template": map[string]interface{}{
			"settings": map[string]interface{}{
				"number_of_shards":               1,
				"number_of_replicas":             0,
				"index.lifecycle.name":           "flume-task-records-policy",
				"index.lifecycle.rollover_alias": "agent-task-records",
			},
			"mappings": map[string]interface{}{
				"dynamic_templates": []map[string]interface{}{
					{
						"strings_as_keywords": map[string]interface{}{
							"match_mapping_type": "string",
							"mapping": map[string]interface{}{
								"type":         "keyword",
								"ignore_above": 256,
							},
						},
					},
				},
				"properties": map[string]interface{}{
					"status":              map[string]interface{}{"type": "keyword"},
					"queue_state":         map[string]interface{}{"type": "keyword"},
					"active_worker":       map[string]interface{}{"type": "keyword"},
					"assigned_agent_role": map[string]interface{}{"type": "keyword"},
					"owner":               map[string]interface{}{"type": "keyword"},
					"updated_at":          map[string]interface{}{"type": "date"},
					"last_update":         map[string]interface{}{"type": "date"},
				},
			},
		},
	}

	_, _, err = esRequest(esURL, apiKey, "_index_template/flume-task-records-template", "PUT", template)
	if err != nil {
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to create Index Template", "error", err)
	} else {
		log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Index Template 'flume-task-records-template' created")
	}

	// 3. Bootstrap Initial Index for the Alias
	_, status, _ := esRequest(esURL, apiKey, "_alias/agent-task-records", "GET", nil)
	if status == 404 {
		initialIndex := map[string]interface{}{
			"aliases": map[string]interface{}{
				"agent-task-records": map[string]interface{}{
					"is_write_index": true,
				},
			},
		}
		_, _, err = esRequest(esURL, apiKey, "agent-task-records-000001", "PUT", initialIndex)
		if err != nil {
			log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to bootstrap initial write index", "error", err)
		} else {
			log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Bootstrapped initial write index 'agent-task-records-000001' attached to alias 'agent-task-records'")
		}
	} else {
		log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Alias 'agent-task-records' already exists. ILM handling ongoing rotations.")
	}

	return nil
}

// SeedLLMConfig writes non-sensitive LLM settings to the flume-llm-config ES index.
// This is the canonical bootstrap path: CLI collects provider/model/baseUrl from the
// interactive prompt and writes them to ES so the dashboard reads them correctly on
// first load — no GUI save required. The Settings page can then update the same
// document at runtime to switch models without a restart.
func SeedLLMConfig(esURL, apiKey string, cfg EnvConfig) error {
	// Fetch existing document to avoid overwriting fields not supplied in this run.
	// On macOS Docker Desktop the ES port-forward can have a brief lag after the
	// healthcheck passes — retry until ES responds before giving up.
	var existBody []byte
	var status int
	for attempt := 1; attempt <= seedMaxRetries; attempt++ {
		var reqErr error
		existBody, status, reqErr = esRequest(esURL, apiKey, "flume-llm-config/_doc/singleton", "GET", nil)
		if reqErr == nil {
			break
		}
		log.Warn("[ELASTICSEARCH BOOTSTRAP] ES not yet reachable, retrying...",
			"attempt", attempt, "max", seedMaxRetries, "error", reqErr)
		if attempt < seedMaxRetries {
			time.Sleep(seedRetryInterval)
		}
	}

	existing := map[string]interface{}{}
	if status == 200 && existBody != nil {
		var doc map[string]interface{}
		if err := json.Unmarshal(existBody, &doc); err == nil {
			if src, ok := doc["_source"].(map[string]interface{}); ok {
				existing = src
			}
		}
	}

	// Only overwrite fields the user explicitly provided in this CLI run.
	provider := cfg.Provider
	if provider == "" {
		provider = "ollama"
	}
	existing["LLM_PROVIDER"] = provider
	if cfg.Model != "" {
		existing["LLM_MODEL"] = cfg.Model
	}

	// Prefer the Docker-rewritten base URL for container access; fall back to host value.
	baseURL := cfg.BaseURL
	if baseURL == "" {
		baseURL = cfg.LocalOllamaBaseURL
	}
	if baseURL != "" {
		existing["LLM_BASE_URL"] = baseURL
	}

	// Retry the PUT as well in case the GET succeeded but a brief blip occurred.
	for attempt := 1; attempt <= seedMaxRetries; attempt++ {
		_, _, err := esRequest(esURL, apiKey, "flume-llm-config/_doc/singleton", "PUT", existing)
		if err == nil {
			log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Seeded flume-llm-config",
				"provider", provider, "model", cfg.Model, "attempt", attempt)
			return nil
		}
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to write flume-llm-config, retrying...",
			"attempt", attempt, "max", seedMaxRetries, "error", err)
		if attempt < seedMaxRetries {
			time.Sleep(seedRetryInterval)
		}
	}
	return fmt.Errorf("failed to seed flume-llm-config after %d attempts", seedMaxRetries)
}
