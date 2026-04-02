import os
import json
import logging
import urllib.request
import urllib.error
import time
import sys
from pathlib import Path

# Fix relative import path for Bootstrap Context
_BS_WS = Path(__file__).resolve().parent.parent
if str(_BS_WS) not in sys.path:
    sys.path.insert(0, str(_BS_WS))

from utils.logger import get_logger
logger = get_logger("es_bootstrap")

REQUIRED_INDICES = [
    "flume-projects",
    "flume-tasks",
    "flume-workers",
    "agent-task-records",
    "agent-security-audits",
    "agent-checkpoints",
    "flume-elastro-graph",
    "agent_semantic_memory",
    "flow_tools",
    "agent_knowledge",
    "agent-plan-sessions",
    "agent-system-cluster",
    "agent-system-workers",
    "agent-token-telemetry",
    # Kubernetes-grade credential metadata stores (secrets in OpenBao)
    "flume-llm-credentials",
    "flume-ado-tokens",
    "flume-github-tokens",
]

INDEX_TEMPLATES = {
    "agent-system-state-tpl": {
        "index_patterns": ["agent-system-workers*", "agent-system-cluster*", "agent-plan-sessions*"],
        "template": {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0
            },
            "mappings": {
                "dynamic_templates": [
                    {
                        "strings_as_keywords": {
                            "match_mapping_type": "string",
                            "mapping": {
                                "type": "text",
                                "fields": {
                                    "keyword": {
                                        "type": "keyword",
                                        "ignore_above": 512
                                    }
                                }
                            }
                        }
                    }
                ],
                "properties": {
                    "updated_at": {"type": "date"},
                    "created_at": {"type": "date"},
                    "heartbeat_at": {"type": "date"},
                    "status": {"type": "keyword"}
                }
            }
        }
    }
}

TASK_RECORDS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            # Identity
            "id":                          {"type": "keyword"},
            "title":                       {"type": "text"},
            "objective":                   {"type": "text"},
            "acceptance_criteria":         {"type": "text"},
            "artifacts":                   {"type": "text"},
            "agent_log":                   {
                "type": "nested",
                "properties": {
                    "ts":   {"type": "date"},
                    "note": {"type": "text"},
                }
            },
            # Taxonomy / routing
            "item_type":                   {"type": "keyword"},
            "repo":                        {"type": "keyword"},
            "worktree":                    {"type": "keyword"},
            "priority":                    {"type": "keyword"},
            "risk":                        {"type": "keyword"},
            "depends_on":                  {"type": "keyword"},
            # Worker ownership — ALL keyword so ES term queries & Painless scripts work
            "owner":                       {"type": "keyword"},
            "assigned_agent_role":         {"type": "keyword"},
            "active_worker":               {"type": "keyword"},
            "execution_host":              {"type": "keyword"},
            # State machine — keyword ensures term/terms queries are exact-match
            "status":                      {"type": "keyword"},
            "queue_state":                 {"type": "keyword"},
            "ast_sync_status":             {"type": "keyword"},
            "ast_synced":                  {"type": "boolean"},
            "ast_sync_attempts":           {"type": "integer"},
            "needs_human":                 {"type": "boolean"},
            # LLM preferences
            "preferred_model":             {"type": "keyword"},
            "preferred_llm_provider":      {"type": "keyword"},
            "preferred_llm_credential_id": {"type": "keyword"},
            # VCS
            "commit_sha":                  {"type": "keyword"},
            "branch":                      {"type": "keyword"},
            # Timestamps
            "created_at":                  {"type": "date"},
            "updated_at":                  {"type": "date"},
            "last_update":                 {"type": "date"},
        }
    }
}

TOKEN_TELEMETRY_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "worker_name": {"type": "keyword"},
            "worker_role": {"type": "keyword"},
            "provider": {"type": "keyword"},
            "model": {"type": "keyword"},
            "input_tokens": {"type": "long"},
            "output_tokens": {"type": "long"},
            "savings": {"type": "long"},
            "created_at": {"type": "date"}
        }
    }
}

SECURITY_AUDIT_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "@timestamp": {"type": "date"},
            "message": {"type": "text"},
            "agent_roles": {"type": "keyword"},
            "worker_name": {"type": "keyword"},
            "secret_path": {"type": "keyword"},
            "keys_retrieved": {"type": "keyword"}
        }
    }
}

PROJECTS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "id":           { "type": "keyword" },
            "name":         { "type": "keyword" },
            "repoUrl":      { "type": "keyword" },
            "localPath":    { "type": "keyword" },
            "cloneStatus": { "type": "keyword" },
            "cloneError":  { "type": "text" },
            "repoType":    { "type": "keyword" },
            "gitflow":     { "type": "object", "enabled": False },
            "created_at":  { "type": "date" },
            "updated_at":  { "type": "date" },
        }
    }
}

# Per-index explicit mappings used during initial creation.
# Only applied when the index does not already exist.
EXPLICIT_INDEX_MAPPINGS = {
    "flume-projects":      PROJECTS_MAPPING,
    "agent-task-records": TASK_RECORDS_MAPPING,
    "agent-token-telemetry": TOKEN_TELEMETRY_MAPPING,
    "agent-security-audits": SECURITY_AUDIT_MAPPING,
}

def ensure_es_indices():
    """Bootstraps all explicit Elasticsearch namespaces required by the Autonomous Docker architecture."""
    es_url = os.environ.get("ES_URL", "http://elasticsearch:9200")
    es_api_key = os.environ.get("ES_API_KEY", "")
    
    headers = {"Content-Type": "application/json"}
    if es_api_key and "bypass" not in es_api_key:
        headers["Authorization"] = f"ApiKey {es_api_key}"

    # 1. Apply Strict Index Templates First
    for tpl_name, tpl_body in INDEX_TEMPLATES.items():
        url = f"{es_url}/_index_template/{tpl_name}"
        req = urllib.request.Request(url, headers=headers, data=json.dumps(tpl_body).encode(), method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                logger.info(f"Successfully applied ES template: {tpl_name}")
        except Exception as e:
            logger.error(f"Failed to apply ES template {tpl_name}: {e}")

    # 2. Boot Indices with Explicit Mappings Where Required
    for index in REQUIRED_INDICES:
        url = f"{es_url}/{index}"
        
        req_check = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(req_check, timeout=5) as r:
                if r.status == 200:
                    # Index exists — ensure critical keyword field mappings are up-to-date
                    # via a no-op PUT mapping (ES ignores existing compatible types).
                    if index in EXPLICIT_INDEX_MAPPINGS:
                        mapping_body = EXPLICIT_INDEX_MAPPINGS[index].get("mappings", {})
                        mapping_url = f"{es_url}/{index}/_mapping"
                        mapping_req = urllib.request.Request(
                            mapping_url,
                            headers=headers,
                            data=json.dumps(mapping_body).encode(),
                            method="PUT",
                        )
                        try:
                            with urllib.request.urlopen(mapping_req, timeout=5):
                                pass
                        except Exception:
                            pass  # Mapping conflicts on existing indices are non-fatal
                    continue
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning(f"Error checking index {index}: {e}")
                continue
        except Exception as e:
            logger.error(f"Cannot reach Elasticsearch at {es_url}: {e}")
            return
            
        # Index does not exist — create with full explicit mapping if available
        mapping = EXPLICIT_INDEX_MAPPINGS.get(index)
        body = json.dumps(mapping).encode() if mapping else None
        req_create = urllib.request.Request(url, headers=headers, data=body, method="PUT")
        try:
            with urllib.request.urlopen(req_create, timeout=5) as r:
                logger.info(f"Successfully bootstrapped ES Index natively: {index}")
        except Exception as e:
            logger.error(f"Failed to create index {index}: {e}")

    # 3. Bootstrap credential store indices with explicit mappings
    try:
        from es_credential_store import ensure_credential_indices
        ensure_credential_indices()
    except Exception as e:
        logger.warning(f"Credential index bootstrap skipped: {e}")


def ensure_vault_credentials():
    logger.info("Initializing OpenBao (Vault) native bootstrap...")
    vault_url = os.environ.get("OPENBAO_ADDR", "http://openbao:8200")
    vault_token = os.environ.get("OPENBAO_TOKEN", "flume-dev-token")
    headers = {"X-Vault-Token": vault_token, "Content-Type": "application/json"}
    
    for attempt in range(40):
        try:
            req_sys = urllib.request.Request(f"{vault_url}/v1/sys/health")
            with urllib.request.urlopen(req_sys, timeout=2) as r:
                pass
        except Exception as e:
            logger.info(f"Vault not ready ({e}), waiting...")
            time.sleep(2)
            continue
            
        req_write = urllib.request.Request(
            f"{vault_url}/v1/secret/data/flume",
            headers=headers,
            data=json.dumps({"data": {"ES_API_KEY": "flume-enterprise-dev-bypass-key"}}).encode(),
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_write, timeout=5) as r:
                logger.info("Successfully provisioned ES_API_KEY to OpenBao natively!")
                return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info(f"Vault KV secret mount not fully initialized yet (404), retrying... ({attempt})")
                time.sleep(2)
            else:
                logger.error(f"Failed to bootstrap Vault: {e}")
                return
        except Exception as e:
            logger.error(f"Failed to reach Vault: {e}")
            time.sleep(2)
            
    logger.error("Failed to provision OpenBao credentials after maximum retries.")

if __name__ == "__main__":
    ensure_vault_credentials()
    ensure_es_indices()
