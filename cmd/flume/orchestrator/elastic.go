package orchestrator

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/charmbracelet/log"
)

const (
	seedMaxRetries    = 10
	seedRetryInterval = 2 * time.Second
)

func esRequest(ctx context.Context, esURL, apiKey, endpoint, method string, payload interface{}) ([]byte, int, error) {
	url := fmt.Sprintf("%s/%s", strings.TrimRight(esURL, "/"), strings.TrimLeft(endpoint, "/"))

	var reqBody io.Reader
	if payload != nil {
		bodyBytes, err := json.Marshal(payload)
		if err != nil {
			return nil, 0, err
		}
		reqBody = bytes.NewBuffer(bodyBytes)
	}

	req, err := http.NewRequestWithContext(ctx, method, url, reqBody)
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

// ─────────────────────────────────────────────────────────────────────────────
// Centralized ES Index Bootstrap
//
// ALL Elasticsearch index creation is owned by the CLI. This function runs
// during `flume start` after ES + OpenBao are healthy but BEFORE any
// application containers (dashboard, worker, gateway) are started.
//
// This eliminates the boot-race where workers hit 404s because indices
// haven't been created yet by the dashboard.
// ─────────────────────────────────────────────────────────────────────────────

// indexDef holds the name and optional explicit mapping for an ES index.
type indexDef struct {
	Name    string
	Mapping map[string]interface{} // nil = use singleNodeDefaults
}

// templateDef holds an index template definition.
type templateDef struct {
	Name string
	Body map[string]interface{}
}

// ── Shared mapping fragments ────────────────────────────────────────────────

// eventRecordMapping is the shared mapping for lifecycle-event indices (reviews,
// failures, provenance, handoffs, memory entries, settings, telemetry).
var eventRecordMapping = map[string]interface{}{
	"settings": map[string]interface{}{
		"number_of_shards":   1,
		"number_of_replicas": 0,
	},
	"mappings": map[string]interface{}{
		"properties": map[string]interface{}{
			"task_id":    map[string]interface{}{"type": "keyword"},
			"repo":       map[string]interface{}{"type": "keyword"},
			"status":     map[string]interface{}{"type": "keyword"},
			"worker":     map[string]interface{}{"type": "keyword"},
			"created_at": map[string]interface{}{"type": "date"},
			"updated_at": map[string]interface{}{"type": "date"},
			"message":    map[string]interface{}{"type": "text"},
		},
	},
}

// ── Index definitions ───────────────────────────────────────────────────────

// allIndices is the authoritative list of every ES index Flume requires.
// Ported from: es_bootstrap.py REQUIRED_INDICES + EXPLICIT_INDEX_MAPPINGS,
// es_credential_store.py _INDEX_MAPPINGS, config.go EnsureAgentModelsIndex,
// node_registry.go EnsureIndex.
var allIndices = []indexDef{
	// ── Core project & task indices ──────────────────────────────────────
	{Name: "flume-projects", Mapping: map[string]interface{}{
		"settings": map[string]interface{}{
			"number_of_shards": 1, "number_of_replicas": 0,
		},
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"id":          map[string]interface{}{"type": "keyword"},
				"name":        map[string]interface{}{"type": "keyword"},
				"repoUrl":     map[string]interface{}{"type": "keyword"},
				"localPath":   map[string]interface{}{"type": "keyword"},
				"cloneStatus": map[string]interface{}{"type": "keyword"},
				"cloneError":  map[string]interface{}{"type": "text"},
				"repoType":    map[string]interface{}{"type": "keyword"},
				"gitflow":     map[string]interface{}{"type": "object", "enabled": false},
				"created_at":  map[string]interface{}{"type": "date"},
				"updated_at":  map[string]interface{}{"type": "date"},
			},
		},
	}},
	{Name: "flume-tasks", Mapping: nil},
	{Name: "flume-workers", Mapping: nil},

	// ── AP-1: Atomic monotonic counters (replaces sequence_counters.json) ─
	{Name: "flume-counters", Mapping: map[string]interface{}{
		"settings": map[string]interface{}{
			"number_of_shards": 1, "number_of_replicas": 0,
		},
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"prefix":     map[string]interface{}{"type": "keyword"},
				"value":      map[string]interface{}{"type": "long"},
				"updated_at": map[string]interface{}{"type": "date"},
			},
		},
	}},

	// ── AP-8: Per-role LLM model overrides (replaces agent_models.json) ──
	{Name: "flume-config", Mapping: nil},

	// ── AP-10: Non-sensitive LLM settings (provider/model/baseUrl) ───────
	{Name: "flume-llm-config", Mapping: map[string]interface{}{
		"settings": map[string]interface{}{
			"number_of_shards": 1, "number_of_replicas": 0,
		},
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"LLM_PROVIDER":   map[string]interface{}{"type": "keyword"},
				"LLM_MODEL":      map[string]interface{}{"type": "keyword"},
				"LLM_BASE_URL":   map[string]interface{}{"type": "keyword"},
				"LLM_ROUTE_TYPE": map[string]interface{}{"type": "keyword"},
			},
		},
	}},

	// ── System-level runtime config ──────────────────────────────────────
	{Name: "flume-settings", Mapping: eventRecordMapping},

	// ── Worker-manager heartbeat telemetry ───────────────────────────────
	{Name: "flume-telemetry", Mapping: eventRecordMapping},

	// ── Core task records ────────────────────────────────────────────────
	{Name: "agent-task-records", Mapping: map[string]interface{}{
		"settings": map[string]interface{}{
			"number_of_shards": 1, "number_of_replicas": 0,
		},
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"id":                          map[string]interface{}{"type": "keyword"},
				"title":                       map[string]interface{}{"type": "text"},
				"objective":                   map[string]interface{}{"type": "text"},
				"acceptance_criteria":         map[string]interface{}{"type": "text"},
				"artifacts":                   map[string]interface{}{"type": "text"},
				"agent_log": map[string]interface{}{
					"type": "nested",
					"properties": map[string]interface{}{
						"ts":   map[string]interface{}{"type": "date"},
						"note": map[string]interface{}{"type": "text"},
					},
				},
				"execution_thoughts": map[string]interface{}{
					"type": "nested",
					"properties": map[string]interface{}{
						"ts":      map[string]interface{}{"type": "date"},
						"thought": map[string]interface{}{"type": "text"},
					},
				},
				"item_type":                   map[string]interface{}{"type": "keyword"},
				"repo":                        map[string]interface{}{"type": "keyword"},
				"worktree":                    map[string]interface{}{"type": "keyword"},
				"priority":                    map[string]interface{}{"type": "keyword"},
				"risk":                        map[string]interface{}{"type": "keyword"},
				"depends_on":                  map[string]interface{}{"type": "keyword"},
				"owner":                       map[string]interface{}{"type": "keyword"},
				"assigned_agent_role":         map[string]interface{}{"type": "keyword"},
				"active_worker":               map[string]interface{}{"type": "keyword"},
				"execution_host":              map[string]interface{}{"type": "keyword"},
				"status":                      map[string]interface{}{"type": "keyword"},
				"queue_state":                 map[string]interface{}{"type": "keyword"},
				"ast_sync_status":             map[string]interface{}{"type": "keyword"},
				"ast_synced":                  map[string]interface{}{"type": "boolean"},
				"ast_sync_attempts":           map[string]interface{}{"type": "integer"},
				"needs_human":                 map[string]interface{}{"type": "boolean"},
				"preferred_model":             map[string]interface{}{"type": "keyword"},
				"preferred_llm_provider":      map[string]interface{}{"type": "keyword"},
				"preferred_llm_credential_id": map[string]interface{}{"type": "keyword"},
				"commit_sha":                  map[string]interface{}{"type": "keyword"},
				"branch":                      map[string]interface{}{"type": "keyword"},
				"created_at":                  map[string]interface{}{"type": "date"},
				"updated_at":                  map[string]interface{}{"type": "date"},
				"last_update":                 map[string]interface{}{"type": "date"},
			},
		},
	}},

	// ── Lifecycle-event indices (shared mapping) ─────────────────────────
	{Name: "agent-review-records", Mapping: eventRecordMapping},
	{Name: "agent-failure-records", Mapping: eventRecordMapping},
	{Name: "agent-provenance-records", Mapping: eventRecordMapping},
	{Name: "agent-handoff-records", Mapping: eventRecordMapping},
	{Name: "agent-memory-entries", Mapping: eventRecordMapping},

	// ── Token telemetry ──────────────────────────────────────────────────
	{Name: "agent-token-telemetry", Mapping: map[string]interface{}{
		"settings": map[string]interface{}{
			"number_of_shards": 1, "number_of_replicas": 0,
		},
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"worker_name":  map[string]interface{}{"type": "keyword"},
				"worker_role":  map[string]interface{}{"type": "keyword"},
				"provider":     map[string]interface{}{"type": "keyword"},
				"model":        map[string]interface{}{"type": "keyword"},
				"input_tokens": map[string]interface{}{"type": "long"},
				"output_tokens": map[string]interface{}{"type": "long"},
				"savings":      map[string]interface{}{"type": "long"},
				"created_at":   map[string]interface{}{"type": "date"},
			},
		},
	}},

	// ── Security audit index ─────────────────────────────────────────────
	{Name: "agent-security-audits", Mapping: map[string]interface{}{
		"settings": map[string]interface{}{
			"number_of_shards": 1, "number_of_replicas": 0,
		},
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"@timestamp":     map[string]interface{}{"type": "date"},
				"message":        map[string]interface{}{"type": "text"},
				"agent_roles":    map[string]interface{}{"type": "keyword"},
				"worker_name":    map[string]interface{}{"type": "keyword"},
				"secret_path":    map[string]interface{}{"type": "keyword"},
				"keys_retrieved": map[string]interface{}{"type": "keyword"},
			},
		},
	}},

	// ── System state / orchestration ─────────────────────────────────────
	{Name: "agent-checkpoints", Mapping: nil},
	{Name: "agent-plan-sessions", Mapping: nil},
	{Name: "agent-system-cluster", Mapping: nil},
	{Name: "agent-system-workers", Mapping: nil},

	// ── AST / knowledge / memory ─────────────────────────────────────────
	{Name: "flume-elastro-graph", Mapping: nil},
	{Name: "agent_semantic_memory", Mapping: nil},
	{Name: "flow_tools", Mapping: nil},
	{Name: "agent_knowledge", Mapping: nil},

	// ── Task events ──────────────────────────────────────────────────────
	{Name: "flume-task-events", Mapping: nil},

	// ── Kubernetes-grade credential metadata stores (secrets in OpenBao) ─
	{Name: "flume-llm-credentials", Mapping: map[string]interface{}{
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"store_key":          map[string]interface{}{"type": "keyword"},
				"version":            map[string]interface{}{"type": "integer"},
				"activeCredentialId": map[string]interface{}{"type": "keyword"},
				"defaultCredentialId": map[string]interface{}{"type": "keyword"},
				"credentials":        map[string]interface{}{"type": "object", "enabled": false},
			},
		},
	}},
	{Name: "flume-ado-tokens", Mapping: map[string]interface{}{
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"store_key":          map[string]interface{}{"type": "keyword"},
				"version":            map[string]interface{}{"type": "integer"},
				"activeCredentialId": map[string]interface{}{"type": "keyword"},
				"credentials":        map[string]interface{}{"type": "object", "enabled": false},
			},
		},
	}},
	{Name: "flume-github-tokens", Mapping: map[string]interface{}{
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"store_key":          map[string]interface{}{"type": "keyword"},
				"version":            map[string]interface{}{"type": "integer"},
				"activeTokenId":      map[string]interface{}{"type": "keyword"},
				"tokens":             map[string]interface{}{"type": "object", "enabled": false},
			},
		},
	}},

	// ── Gateway: per-role model overrides (ported from config.go) ────────
	{Name: "flume-agent-models", Mapping: map[string]interface{}{
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"roles":      map[string]interface{}{"type": "object", "enabled": false},
				"updated_at": map[string]interface{}{"type": "date"},
			},
		},
	}},

	// ── Gateway: distributed Ollama node mesh (ported from node_registry.go)
	{Name: "flume-node-registry", Mapping: map[string]interface{}{
		"mappings": map[string]interface{}{
			"properties": map[string]interface{}{
				"id":               map[string]interface{}{"type": "keyword"},
				"host":             map[string]interface{}{"type": "keyword"},
				"model_tag":        map[string]interface{}{"type": "keyword"},
				"capabilities":     map[string]interface{}{"type": "object", "enabled": true},
				"health":           map[string]interface{}{"type": "object", "enabled": true},
				"concurrency_cap":  map[string]interface{}{"type": "integer"},
				"auth_secret_path": map[string]interface{}{"type": "keyword"},
				"updated_at":       map[string]interface{}{"type": "date"},
			},
		},
	}},
}

// allTemplates is the authoritative list of ES index templates.
// Ported from: es_bootstrap.py INDEX_TEMPLATES.
var allTemplates = []templateDef{
	{
		Name: "agent-system-state-tpl",
		Body: map[string]interface{}{
			"index_patterns": []string{"agent-system-workers*", "agent-system-cluster*", "agent-plan-sessions*"},
			"template": map[string]interface{}{
				"settings": map[string]interface{}{
					"number_of_shards":   1,
					"number_of_replicas": 0,
				},
				"mappings": map[string]interface{}{
					"dynamic_templates": []map[string]interface{}{
						{
							"strings_as_keywords": map[string]interface{}{
								"match_mapping_type": "string",
								"mapping": map[string]interface{}{
									"type": "text",
									"fields": map[string]interface{}{
										"keyword": map[string]interface{}{
											"type":         "keyword",
											"ignore_above": 512,
										},
									},
								},
							},
						},
					},
					"properties": map[string]interface{}{
						"updated_at":   map[string]interface{}{"type": "date"},
						"created_at":   map[string]interface{}{"type": "date"},
						"heartbeat_at": map[string]interface{}{"type": "date"},
						"status":       map[string]interface{}{"type": "keyword"},
					},
				},
			},
		},
	},
}

// EnsureAllIndices creates every ES index and template Flume requires.
//
// This is the single source of truth for all Elasticsearch schema. It runs
// from `flume start` after ES is healthy and OpenBao is deployed, but BEFORE
// any application containers (dashboard, worker, gateway) are brought up.
//
// Each index uses an idempotent HEAD→PUT pattern: skip if the index already
// exists, create with the explicit mapping if it doesn't.
func EnsureAllIndices(ctx context.Context, esURL, apiKey string) error {
	log.Info("[ES INDEX BOOTSTRAP] Creating all Elasticsearch indices", "url", esURL, "indices", len(allIndices), "templates", len(allTemplates))

	client := &http.Client{Timeout: 10 * time.Second}

	// 1. Apply index templates first (they govern indices matching patterns)
	for _, tpl := range allTemplates {
		endpoint := fmt.Sprintf("%s/_index_template/%s", strings.TrimRight(esURL, "/"), tpl.Name)
		body, err := json.Marshal(tpl.Body)
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Failed to marshal template", "template", tpl.Name, "error", err)
			continue
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPut, endpoint, bytes.NewReader(body))
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Failed to build template request", "template", tpl.Name, "error", err)
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		if apiKey != "" {
			req.Header.Set("Authorization", "ApiKey "+apiKey)
		}
		resp, err := client.Do(req)
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Failed to apply template", "template", tpl.Name, "error", err)
			continue
		}
		resp.Body.Close()
		if resp.StatusCode < 400 {
			log.Info("[ES INDEX BOOTSTRAP] ✅ Applied index template", "template", tpl.Name)
		} else {
			log.Warn("[ES INDEX BOOTSTRAP] Template apply returned non-success", "template", tpl.Name, "status", resp.StatusCode)
		}
	}

	// 2. Create each index (HEAD check → skip if exists → PUT if not)
	created := 0
	skipped := 0
	failed := 0

	for _, idx := range allIndices {
		url := fmt.Sprintf("%s/%s", strings.TrimRight(esURL, "/"), idx.Name)

		// HEAD check
		headReq, err := http.NewRequestWithContext(ctx, http.MethodHead, url, nil)
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Failed to build HEAD request", "index", idx.Name, "error", err)
			failed++
			continue
		}
		headReq.Header.Set("Content-Type", "application/json")
		if apiKey != "" {
			headReq.Header.Set("Authorization", "ApiKey "+apiKey)
		}
		headResp, err := client.Do(headReq)
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Cannot reach Elasticsearch", "index", idx.Name, "error", err)
			failed++
			continue
		}
		headResp.Body.Close()

		if headResp.StatusCode == 200 {
			// Index already exists — update mapping if we have one (idempotent)
			if idx.Mapping != nil {
				mappingBody, ok := idx.Mapping["mappings"]
				if ok {
					mappingURL := fmt.Sprintf("%s/%s/_mapping", strings.TrimRight(esURL, "/"), idx.Name)
					mBody, _ := json.Marshal(mappingBody)
					mReq, _ := http.NewRequestWithContext(ctx, http.MethodPut, mappingURL, bytes.NewReader(mBody))
					if mReq != nil {
						mReq.Header.Set("Content-Type", "application/json")
						if apiKey != "" {
							mReq.Header.Set("Authorization", "ApiKey "+apiKey)
						}
						mResp, mErr := client.Do(mReq)
						if mErr == nil {
							mResp.Body.Close()
						}
					}
				}
			}
			skipped++
			continue
		}

		// Index does not exist — create with explicit mapping or single-node defaults.
		// Q4: Always include number_of_replicas:0 to prevent yellow status on
		// single-node clusters where replica shards can never be allocated.
		var putBody io.Reader
		if idx.Mapping != nil {
			// Ensure settings include replicas:0 even for explicit mappings
			if _, hasSettings := idx.Mapping["settings"]; !hasSettings {
				idx.Mapping["settings"] = map[string]interface{}{
					"number_of_shards":   1,
					"number_of_replicas": 0,
				}
			}
			mappingBytes, _ := json.Marshal(idx.Mapping)
			putBody = bytes.NewReader(mappingBytes)
		} else {
			// No mapping — use single-node defaults
			defaultBody := map[string]interface{}{
				"settings": map[string]interface{}{
					"number_of_shards":   1,
					"number_of_replicas": 0,
				},
			}
			defaultBytes, _ := json.Marshal(defaultBody)
			putBody = bytes.NewReader(defaultBytes)
		}
		putReq, err := http.NewRequestWithContext(ctx, http.MethodPut, url, putBody)
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Failed to build PUT request", "index", idx.Name, "error", err)
			failed++
			continue
		}
		putReq.Header.Set("Content-Type", "application/json")
		if apiKey != "" {
			putReq.Header.Set("Authorization", "ApiKey "+apiKey)
		}
		putResp, err := client.Do(putReq)
		if err != nil {
			log.Error("[ES INDEX BOOTSTRAP] Failed to create index", "index", idx.Name, "error", err)
			failed++
			continue
		}
		putResp.Body.Close()

		if putResp.StatusCode < 400 {
			log.Info("[ES INDEX BOOTSTRAP] ✅ Created index", "index", idx.Name)
			created++
		} else {
			log.Warn("[ES INDEX BOOTSTRAP] Index creation returned non-success", "index", idx.Name, "status", putResp.StatusCode)
			failed++
		}
	}

	log.Info("[ES INDEX BOOTSTRAP] Index bootstrap complete", "created", created, "skipped_existing", skipped, "failed", failed, "total", len(allIndices))

	if failed > 0 {
		return fmt.Errorf("failed to create %d indices", failed)
	}
	return nil
}

// SeedPainlessScripts securely uploads native compilation scripts into ES
// drastically reducing HTTP wire overhead natively during swarm operations.
func SeedPainlessScripts(ctx context.Context, esURL, apiKey string) error {
	log.Debug("[ELASTICSEARCH SCRIPTS] Seeding Enterprise Painless execution routines natively")

	scripts := map[string]string{
		"flume-task-claim": `
			if (ctx._source.status == params.expected_status 
			&& (ctx._source.active_worker == null || ctx._source.active_worker == "")) {
				ctx._source.status = params.new_status;
				ctx._source.queue_state = "active";
				ctx._source.active_worker = params.worker_name;
				ctx._source.assigned_agent_role = params.role;
				ctx._source.owner = params.role;
				ctx._source.updated_at = params.now;
				ctx._source.last_update = params.now;
				if (params.execution_host != null) { ctx._source.execution_host = params.execution_host; }
				if (params.preferred_model != null) { ctx._source.preferred_model = params.preferred_model; }
				if (params.preferred_llm_provider != null) { ctx._source.preferred_llm_provider = params.preferred_llm_provider; }
				if (params.preferred_llm_credential_id != null) { ctx._source.preferred_llm_credential_id = params.preferred_llm_credential_id; }
			} else {
				ctx.op = "noop";
			}`,
		"flume-task-block": `
			ctx._source.status = "blocked";
			ctx._source.queue_state = "idle";
			ctx._source.message = "Task paused due to mesh capacity limits (node overload). Will automatically resume with jitter when resources free up.";
		`,
		"flume-task-resume": `
			ctx._source.status = "ready";
			ctx._source.queue_state = "idle";
			ctx._source.active_worker = null;
		`,
		"flume-append-agent-note": `
			if (ctx._source.agent_log == null) { ctx._source.agent_log = []; }
			ctx._source.agent_log.add(params.entry);
			if (ctx._source.agent_log.length > 100) { ctx._source.agent_log.remove(0); }
			ctx._source.updated_at = params.touch;
			ctx._source.last_update = params.touch;
		`,
		"flume-append-execution-thought": `
			if (ctx._source.execution_thoughts == null) { ctx._source.execution_thoughts = []; }
			ctx._source.execution_thoughts.add(params.entry);
			if (ctx._source.execution_thoughts.length > 500) { ctx._source.execution_thoughts.remove(0); }
			ctx._source.updated_at = params.touch;
			ctx._source.last_update = params.touch;
		`,
	}

	for id, source := range scripts {
		payload := map[string]interface{}{
			"script": map[string]interface{}{
				"lang":   "painless",
				"source": source,
			},
		}
		endpoint := fmt.Sprintf("_scripts/%s", id)
		_, _, err := esRequest(ctx, esURL, apiKey, endpoint, "PUT", payload)
		if err != nil {
			log.Warn("[ELASTICSEARCH SCRIPTS] Failed to seed Painless script natively", "id", id, "error", err)
		} else {
			log.Info(fmt.Sprintf("[ELASTICSEARCH SCRIPTS] ✅ Native Painless routine deployed: %s", id))
		}
	}
	return nil
}

// BootstrapElasticsearch runs all ES infrastructure setup: ILM policies,
// index templates, the full index catalogue, and seed data.
//
// Call sequence during `flume start`:
//
//	ES healthy → OpenBao deployed → BootstrapElasticsearch() → SeedLLMConfig() → containers
func BootstrapElasticsearch(ctx context.Context, esURL, apiKey string) error {
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

	_, _, err := esRequest(ctx, esURL, apiKey, "_ilm/policy/flume-task-records-policy", "PUT", ilmPolicy)
	if err != nil {
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to create ILM policy", "error", err)
	} else {
		log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ ILM Policy 'flume-task-records-policy' created")
	}

	// 2. Create Index Template mapping pattern to ILM policy
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

	_, _, err = esRequest(ctx, esURL, apiKey, "_index_template/flume-task-records-template", "PUT", template)
	if err != nil {
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to create Index Template", "error", err)
	} else {
		log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Index Template 'flume-task-records-template' created")
	}

	// 3. Create ALL indices (centralized — replaces es_bootstrap.py, config.go, node_registry.go)
	if err := EnsureAllIndices(ctx, esURL, apiKey); err != nil {
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Some indices failed to create", "error", err)
		// Non-fatal: continue boot — existing indices will work
	}

	// 4. Seed Native Painless Routine Scripts
	SeedPainlessScripts(ctx, esURL, apiKey)

	return nil
}

// SeedLLMConfig writes non-sensitive LLM settings to the flume-llm-config ES index.
// This is the canonical bootstrap path: CLI collects provider/model/baseUrl from the
// interactive prompt and writes them to ES so the dashboard reads them correctly on
// first load — no GUI save required. The Settings page can then update the same
// document at runtime to switch models without a restart.
func SeedLLMConfig(ctx context.Context, esURL, apiKey string, cfg EnvConfig) error {
	// Fetch existing document to avoid overwriting fields not supplied in this run.
	// On macOS Docker Desktop the ES port-forward can have a brief lag after the
	// healthcheck passes — retry until ES responds before giving up.
	var existBody []byte
	var status int
	for attempt := 1; attempt <= seedMaxRetries; attempt++ {
		var reqErr error
		existBody, status, reqErr = esRequest(ctx, esURL, apiKey, "flume-llm-config/_doc/singleton", "GET", nil)
		if reqErr == nil {
			break
		}
		log.Warn("[ELASTICSEARCH BOOTSTRAP] ES not yet reachable, retrying...",
			"attempt", attempt, "max", seedMaxRetries, "error", reqErr)
		if attempt < seedMaxRetries {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(seedRetryInterval):
			}
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
		_, _, err := esRequest(ctx, esURL, apiKey, "flume-llm-config/_doc/singleton", "PUT", existing)
		if err == nil {
			log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Seeded flume-llm-config",
				"provider", provider, "model", cfg.Model, "attempt", attempt)
				
			// BOOTSTRAP EXTENSION: Native Routing Policy Injection
			// Bind the frontier models natively into the Gateway's Routing Policy map
			if len(cfg.CloudProviders) > 0 {
				var frontierMix []map[string]interface{}
				for _, cp := range cfg.CloudProviders {
					modelStr := cp.Model
					if modelStr == "" {
						modelStr = "default"
					}
					frontierMix = append(frontierMix, map[string]interface{}{
						"provider":      cp.Provider,
						"model":         modelStr,
						"credential_id": "__settings_default__",
						"weight":        1.0,
						"budget_usd":    50.0,
					})
				}

				routingPayload := map[string]interface{}{
					"mode":                 "hybrid",
					"frontier_mix":         frontierMix,
					"frontier_local_ratio": 0.3,
					"complexity_threshold": 7,
				}

				_, _, rErr := esRequest(ctx, esURL, apiKey, "flume-routing-policy/_doc/singleton", "PUT", routingPayload)
				if rErr == nil {
					log.Info("[ELASTICSEARCH BOOTSTRAP] ✅ Seeded Native Routing Policy for Frontier Mesh.")
					slog.Info("orchestrator: safely bootstrapped flume-routing-policy document natively into ES",
						slog.Int("provider_count", len(cfg.CloudProviders)))
				} else {
					log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to seed routing policy natively", "error", rErr)
					slog.Warn("orchestrator: failed to bootstrap flume-routing-policy", "error", rErr)
				}
			}

			return nil
		}
		log.Warn("[ELASTICSEARCH BOOTSTRAP] Failed to write flume-llm-config, retrying...",
			"attempt", attempt, "max", seedMaxRetries, "error", err)
		if attempt < seedMaxRetries {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(seedRetryInterval):
			}
		}
	}
	return fmt.Errorf("failed to seed flume-llm-config after %d attempts", seedMaxRetries)
}
