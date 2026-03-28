import os, json, datetime, secrets, sys
import hvac
from hvac.exceptions import VaultError

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
    
    # Wait for OpenBao to come online natively
    log("info", "Awaiting OpenBao Native Initialization...", component="vault_boot")
    import time
    import requests
    for i in range(30):
        try:
            res = requests.get('http://openbao:8200/v1/sys/health')
            if res.status_code in [200, 429, 472, 473, 501, 503]:
                break
        except Exception:
            pass
        time.sleep(1)
        
    keys_path = "/vault/file/keys.json"
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

    # 2. Unseal OpenBao
    try:
        if client.sys.is_sealed():
            client.sys.submit_unseal_key(unseal_key)
            log("info", "OpenBao KMS Unsealed Successfully.", component="vault_unseal")
        else:
            log("info", "OpenBao KMS already unsealed. Continuing...", component="vault_unseal")
    except VaultError as e:
        log("error", "Failed to unseal vault", component="vault_unseal", error_details=str(e))
        sys.exit(1)

    # 3. Login to Vault
    client.token = root_token
    
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

if __name__ == "__main__":
    main()
