import os
import json
import logging
import ssl
import urllib.request
import urllib.error

logger = logging.getLogger("es_bootstrap")


def _es_ssl_context():
    if os.environ.get("ES_VERIFY_TLS", "false").lower() == "true":
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

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
    "agent_knowledge"
]

def ensure_es_indices():
    """Bootstraps all explicit Elasticsearch namespaces required by the Autonomous Docker architecture."""
    es_url = os.environ.get("ES_URL", "http://elasticsearch:9200")
    es_api_key = os.environ.get("ES_API_KEY", "")
    
    headers = {"Content-Type": "application/json"}
    if es_api_key:
        headers["Authorization"] = f"ApiKey {es_api_key}"

    es_ctx = _es_ssl_context()

    for index in REQUIRED_INDICES:
        url = f"{es_url}/{index}"
        
        # 1. Check if exists
        req_check = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(req_check, timeout=5, context=es_ctx) as r:
                if r.status == 200:
                    continue
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning(f"Error checking index {index}: {e}")
                continue
        except Exception as e:
            logger.error(f"Cannot reach Elasticsearch at {es_url}: {e}")
            return
            
        # 2. Create if missing (404)
        req_create = urllib.request.Request(url, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req_create, timeout=5, context=es_ctx) as r:
                logger.info(f"Successfully bootstrapped ES Index natively: {index}")
        except Exception as e:
            logger.error(f"Failed to create index {index}: {e}")

def ensure_vault_credentials():
    es_api_key = os.environ.get("ES_API_KEY", "").strip()
    vault_url = os.environ.get("OPENBAO_ADDR", "").strip()
    vault_token = os.environ.get("OPENBAO_TOKEN", "").strip()

    # Native fresh-install path: if Elasticsearch credentials already exist, do not block startup on OpenBao.
    if es_api_key:
        logger.info("Skipping OpenBao bootstrap because ES_API_KEY is already configured.")
        return

    # If OpenBao is not explicitly configured, skip quietly instead of assuming a container hostname.
    if not vault_url:
        logger.info("Skipping OpenBao bootstrap because OPENBAO_ADDR is not configured.")
        return

    print("Initializing OpenBao (Vault) native bootstrap...", flush=True)
    headers = {"X-Vault-Token": vault_token, "Content-Type": "application/json"}

    import time
    for attempt in range(10):
        try:
            req_sys = urllib.request.Request(f"{vault_url}/v1/sys/health")
            with urllib.request.urlopen(req_sys, timeout=2) as r:
                pass
        except Exception as e:
            print(f"Vault not ready ({e}), waiting...", flush=True)
            time.sleep(1)
            continue

        req_write = urllib.request.Request(
            f"{vault_url}/v1/secret/data/flume",
            headers=headers,
            data=json.dumps({"data": {"ES_API_KEY": "flume-enterprise-dev-bypass-key"}}).encode(),
            method="POST"
        )
        try:
            with urllib.request.urlopen(req_write, timeout=5) as r:
                print("Successfully provisioned ES_API_KEY to OpenBao natively!", flush=True)
                return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"Vault KV secret mount not fully initialized yet (404), retrying... ({attempt})", flush=True)
                time.sleep(1)
            else:
                print(f"Failed to bootstrap Vault: {e}", flush=True)
                return
        except Exception as e:
            print(f"Failed to reach Vault: {e}", flush=True)
            time.sleep(1)

    print("Skipping OpenBao bootstrap after retries; continuing with local configuration.", flush=True)

if __name__ == "__main__":
    ensure_vault_credentials()
    ensure_es_indices()
