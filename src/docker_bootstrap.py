import os
import json
import logging
import urllib.request
import urllib.error
import base64
import time

# docker_bootstrap.py runs as a container entrypoint — before the full app is
# up — so we use a minimal stdlib logger rather than importing utils.logger,
# which has Flask/app-level dependencies that may not be available at bootstrap.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("docker_bootstrap")

def call_es(endpoint: str, payload: dict = None) -> dict:
    password = os.environ.get("ELASTIC_PASSWORD", "flume-elastic-pass")
    auth_str = f"elastic:{password}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    req = urllib.request.Request(
        f"http://elasticsearch:9200{endpoint}",
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {b64_auth}"},
        method="POST" if payload else "GET"
    )
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())

def call_vault(endpoint: str, payload: dict = None) -> dict:
    token = os.environ.get("VAULT_TOKEN", "flume-dev-token")
    req = urllib.request.Request(
        f"http://openbao:8200{endpoint}",
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json", "X-Vault-Token": token},
        method="POST" if payload else "GET"
    )
    with urllib.request.urlopen(req) as res:
        data = res.read().decode()
        return json.loads(data) if data else None

if __name__ == "__main__":
    logger.info("Waiting for Elasticsearch and OpenBao to fully bootstrap...")
    time.sleep(5)

    logger.info("Enabling OpenBao KV-V2 engine at secret/...")
    try:
        call_vault("/v1/sys/mounts/secret", {"type": "kv", "options": {"version": "2"}})
    except urllib.error.HTTPError as e:
        if e.code == 400:
            logger.debug("OpenBao KV-V2 mount already exists (HTTP 400) — skipping")
        else:
            logger.warning("Unexpected HTTP error enabling OpenBao KV-V2 mount", extra={"status": e.code})

    logger.info("Minting dynamic Elasticsearch API Key via xpack.security...")
    es_key_res = call_es("/_security/api_key", {
        "name": "flume-swarm-key",
        "role_descriptors": {
            "flume_role": {
                "cluster": ["all"],
                "index": [{"names": ["*"], "privileges": ["all"]}]
            }
        }
    })
    es_api_key = es_key_res["encoded"]
    logger.info("Elasticsearch API key minted successfully")

    logger.info("Flushing final credentials into OpenBao Vault...")
    call_vault("/v1/secret/data/flume/keys", {
        "data": {
            "ES_API_KEY": es_api_key,
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "")
        }
    })
    logger.info("Bootstrap complete. Ecosystem ready for execution.")
