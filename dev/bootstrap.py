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
    
    try:
        client.secrets.kv.v2.create_or_update_secret(
            path='flume/keys',
            secret={
                'ES_API_KEY': es_key,
                'OPENAI_API_KEY': openai_key
            }
        )
        log("info", "Injected Matrix API keys into Vault KV-v2 secret/flume/keys block natively.", component="vault_kv_write", status="success")
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

if __name__ == "__main__":
    main()
