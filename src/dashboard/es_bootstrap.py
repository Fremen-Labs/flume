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

    # 2. Boot Indices Native Data Structures
    for index in REQUIRED_INDICES:
        url = f"{es_url}/{index}"
        
        req_check = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(req_check, timeout=5) as r:
                if r.status == 200:
                    continue
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning(f"Error checking index {index}: {e}")
                continue
        except Exception as e:
            logger.error(f"Cannot reach Elasticsearch at {es_url}: {e}")
            return
            
        req_create = urllib.request.Request(url, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req_create, timeout=5) as r:
                logger.info(f"Successfully bootstrapped ES Index natively: {index}")
        except Exception as e:
            logger.error(f"Failed to create index {index}: {e}")


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
