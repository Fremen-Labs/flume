import os
import sys
import json
import secrets
import datetime
import requests

import hvac
from hvac.exceptions import VaultError
from tenacity import retry, wait_exponential, stop_after_attempt

def log(level, message, **kwargs):
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "level": level.upper(),
        "message": message,
        "service": "flume-bootstrap",
        **kwargs
    }
    print(json.dumps(log_entry), file=sys.stdout)
    sys.stdout.flush()

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(10))
def await_openbao_boot():
    """Wait for OpenBao to come online natively with exponential backoff."""
    res = requests.get('http://openbao:8200/v1/sys/health')
    if res.status_code not in [200, 429, 472, 473, 501, 503]:
        raise Exception(f"OpenBao health check failed with status {res.status_code}")
    return True

def initialize_and_unseal(client, keys_path):
    if not os.path.exists(keys_path):
        # Check if vault is already initialized (orphaned persistent volume)
        try:
            already_init = client.sys.is_initialized()
        except Exception:
            already_init = False

        if already_init:
            log("warn", "OpenBao already initialized but keys.json missing. "
                "Vault data is orphaned from a previous run. "
                "Remove the openbao_data volume and restart: "
                "docker volume rm flume-fix-e2e_openbao_data")
            # Attempt graceful recovery: try accessing with the dev token
            dev_token = os.environ.get("OPENBAO_TOKEN", "flume-dev-token")
            client.token = dev_token
            try:
                if client.sys.is_sealed():
                    log("error", "Vault is sealed and keys are lost. "
                        "Run: docker volume rm flume-fix-e2e_openbao_data && flume start -p ollama",
                        component="vault_init")
                    sys.exit(1)
                # If unsealed with dev token, proceed
                log("info", "Vault already initialized and unsealed. Recovering with existing token.")
                return dev_token
            except Exception as e:
                log("error", "Vault recovery failed. Remove openbao_data volume and restart.",
                    component="vault_init", error_details=str(e))
                sys.exit(1)

        log("info", "First true boot detected: Initializing OpenBao cluster securely.")
        try:
            init_result = client.sys.initialize(secret_shares=1, secret_threshold=1)
            keys_data = {
                "unseal_keys_b64": init_result["keys_base64"],
                "root_token": init_result["root_token"]
            }
            with open(keys_path, "w") as f:
                json.dump(keys_data, f)
            os.chmod(keys_path, 0o600)
            log("info", "Successfully generated root topology and unseal arrays.", component="vault_init", status="success")
        except VaultError as e:
            log("error", "Failed to initialize vault", component="vault_init", error_details=str(e))
            sys.exit(1)
    else:
        log("info", "Persistent OpenBao cluster detected. Deserializing keys...")

    # Load keys
    with open(keys_path, "r") as f:
        keys_data = json.load(f)
        unseal_key = keys_data["unseal_keys_b64"][0]
        root_token = keys_data["root_token"]

    # Unseal OpenBao
    try:
        if client.sys.is_sealed():
            client.sys.submit_unseal_key(unseal_key)
            log("info", "OpenBao KMS Unsealed Successfully.", component="vault_unseal")
        else:
            log("info", "OpenBao KMS already unsealed. Continuing...", component="vault_unseal")
    except VaultError as e:
        log("error", "Failed to unseal vault", component="vault_unseal", error_details=str(e))
        sys.exit(1)

    return root_token

def _resolve_llm_base_url():
    """Build the canonical LLM_BASE_URL from compose environment.

    Inside Docker, 127.0.0.1 is unreachable on the host.  The compose file
    already sets LOCAL_OLLAMA_BASE_URL with host.docker.internal; prefer that.
    Strip /v1 suffix since agent_runner adds the right path per provider.
    """
    local_ollama = os.environ.get('LOCAL_OLLAMA_BASE_URL', '').strip()
    llm_base = os.environ.get('LLM_BASE_URL', '').strip()

    url = local_ollama or llm_base or 'http://host.docker.internal:11434'
    # Strip /v1 or /api suffix — workers append what they need
    for suffix in ('/v1', '/api'):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
    # Rewrite loopback to Docker-reachable address
    for loopback in ('://127.0.0.1', '://localhost'):
        if loopback in url:
            url = url.replace(loopback, '://host.docker.internal')
    return url.rstrip('/')


def configure_secrets_engine(client, openai_key):
    try:
        secrets_engines = client.sys.list_mounted_secrets_engines()
        if 'secret/' not in secrets_engines.get('data', {}):
            client.sys.enable_secrets_engine(backend_type='kv', path='secret', options={'version': '2'})
            log("info", "Successfully enabled Vault secret engine at secret/.", component="vault_secrets_enable")
        else:
            log("info", "Secret engine 'secret/' already exists. Skipping creation.", component="vault_secrets_enable")
    except VaultError as e:
        log("error", "Failed to interface with Vault", component="vault_secrets", error_details=str(e))
        sys.exit(1)
        
    es_key = secrets.token_hex(32)
    log("info", "Generated Dynamic Elastic Token.")

    # Build full LLM config from compose environment — eliminates .env file dependency
    llm_provider = os.environ.get('LLM_PROVIDER', 'ollama').strip() or 'ollama'
    llm_model = os.environ.get('LLM_MODEL', 'llama3.2').strip() or 'llama3.2'
    llm_base_url = _resolve_llm_base_url()
    llm_api_key = os.environ.get('LLM_API_KEY', '').strip()

    kv_payload = {
        'ES_API_KEY': es_key,
        'OPENAI_API_KEY': openai_key or llm_api_key,
        'LLM_PROVIDER': llm_provider,
        'LLM_MODEL': llm_model,
        'LLM_BASE_URL': llm_base_url,
        'LLM_API_KEY': llm_api_key,
    }
    # Only include non-empty values
    kv_payload = {k: v for k, v in kv_payload.items() if v}

    try:
        # Merge with existing KV data so Settings/dashboard writes are preserved
        try:
            existing = client.secrets.kv.v2.read_secret_version(path='flume/keys')
            existing_data = existing.get('data', {}).get('data', {})
            if existing_data:
                # Existing values win for LLM keys that the user may have configured via Settings
                for key in ('LLM_PROVIDER', 'LLM_MODEL', 'LLM_BASE_URL', 'LLM_API_KEY'):
                    if key in existing_data and existing_data[key].strip():
                        kv_payload[key] = existing_data[key]
                # Always update ES_API_KEY (it's generated fresh)
                existing_data.update(kv_payload)
                kv_payload = existing_data
        except Exception:
            pass  # First boot — no existing secret

        client.secrets.kv.v2.create_or_update_secret(
            path='flume/keys',
            secret=kv_payload
        )
        log("info", "Injected LLM config + API keys into OpenBao KV.",
            component="vault_kv_write", status="success",
            keys_written=list(kv_payload.keys()))
    except VaultError as e:
        log("error", "Failed to push keys to Vault", component="vault_kv_write", error_details=str(e))
        sys.exit(1)

def provision_approle(client):
    try:
        auth_methods = client.sys.list_auth_methods()
        auth_data = auth_methods.get('data', auth_methods) if isinstance(auth_methods, dict) else auth_methods
        if 'approle/' not in auth_data:
            client.sys.enable_auth_method(method_type='approle')
            log("info", "Enabled AppRole authentication engine.")
        else:
            log("info", "AppRole authentication engine already enabled.")
        
        policy = 'path "secret/data/flume/*" { capabilities = ["read"] }'
        
        # Idempotent policy creation
        policies_resp = client.sys.list_policies()
        existing_policies = []
        if isinstance(policies_resp, dict):
            if 'data' in policies_resp and isinstance(policies_resp['data'], dict) and 'policies' in policies_resp['data']:
                existing_policies = policies_resp['data']['policies']
            elif 'policies' in policies_resp:
                existing_policies = policies_resp['policies']
            else:
                existing_policies = list(policies_resp.keys())
        else:
            existing_policies = policies_resp
            
        if 'flume-read-policy' not in existing_policies:
            client.sys.create_or_update_policy(name='flume-read-policy', policy=policy)
            log("info", "Created flume-read-policy for Vault KV access.")
        else:
            log("info", "Policy flume-read-policy already exists.")
        
        client.write('auth/approle/role/flume-worker', token_policies=['flume-read-policy'])
        client.write('auth/approle/role/flume-worker/role-id', role_id='flume-client-role')
        
        # Determine if secret_id already provided natively, otherwise generate
        secret_resp = client.write('auth/approle/role/flume-worker/secret-id')
        secret_id = secret_resp['data']['secret_id']
        
        env_path = "/app/.env"
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
            
            replaced = False
            with open(env_path, "w") as f:
                for line in lines:
                    if line.startswith("BAO_SECRET_ID="):
                        f.write(f'BAO_SECRET_ID="{secret_id}"\n')
                        replaced = True
                    else:
                        f.write(line)
                if not replaced:
                    if lines and not lines[-1].endswith("\n"):
                        f.write("\n")
                    f.write(f'BAO_SECRET_ID="{secret_id}"\n')
            log("info", "Injected dynamically generated BAO_SECRET_ID into /app/.env")
            
        log("info", "Successfully provisioned dynamic AppRole flume-worker.")
    except VaultError as e:
        log("error", "Failed to provision AppRole engine", component="vault_approle", error_details=str(e))
        sys.exit(1)

def main():
    log("info", "Bootstrapping new Flume secrets & Topologies...")
    
    env_vars = {}
    if os.path.exists("/app/.env"):
        with open("/app/.env", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        v = v.strip("'\"")
                        env_vars[k.strip()] = v
        log("info", "Sourced dynamically generated environment topography securely.")
    else:
        log("warn", "/app/.env missing; continuing without explicit API token.")
    
    openai_key = env_vars.get("OPENAI_API_KEY", "")
    
    client = hvac.Client(url='http://openbao:8200')
    
    log("info", "Awaiting OpenBao Native Initialization...", component="vault_boot")
    try:
        await_openbao_boot()
    except Exception as e:
        log("error", "OpenBao failed to initialize after retries.", component="vault_boot", error_details=str(e))
        sys.exit(1)
        
    keys_path = "/vault/file/keys.json"
    root_token = initialize_and_unseal(client, keys_path)
    
    client.token = root_token
    
    configure_secrets_engine(client, openai_key)
    provision_approle(client)
    
    sys.path.insert(0, "/app/src")
    from dashboard.es_bootstrap import ensure_es_indices
    ensure_es_indices()

if __name__ == "__main__":
    main()
