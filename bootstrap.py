import os, subprocess, json, datetime, secrets, sys

def log(level, msg):
    print(json.dumps({
        "time": datetime.datetime.utcnow().isoformat() + "Z", 
        "level": level, 
        "message": msg
    }))
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
    
    keys_path = "/vault/file/keys.json"
    if not os.path.exists(keys_path):
        log("info", "First true boot detected: Initializing OpenBao cluster securely.")
        try:
            init_out = subprocess.check_output(
                ["vault", "operator", "init", "-key-shares=1", "-key-threshold=1", "-format=json"],
                text=True
            )
            with open(keys_path, "w") as f:
                f.write(init_out)
            log("info", "Successfully generated root topology and unseal arrays.")
        except Exception as e:
            log("error", f"Failed to initialize vault: {e}")
            sys.exit(1)
    else:
        log("info", "Persistent OpenBao cluster detected. Deserializing keys...")

    # Load keys
    with open(keys_path, "r") as f:
        keys_data = json.load(f)
        unseal_key = keys_data["unseal_keys_b64"][0]
        root_token = keys_data["root_token"]

    env_path = "/app/.env"
    lines = []
    has_target = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
            
    with open(env_path, "w") as f:
        for line in lines:
            if line.startswith("VAULT_TOKEN="):
                f.write(f"VAULT_TOKEN={root_token}\n")
                has_target = True
            else:
                f.write(line)
        if not has_target:
            f.write(f"\nVAULT_TOKEN={root_token}\n")
    log("info", "Injected dynamic VAULT_TOKEN into local .env successfully.")

    # 2. Unseal OpenBao
    try:
        subprocess.check_call(["vault", "operator", "unseal", unseal_key], stdout=subprocess.DEVNULL)
        log("info", "OpenBao KMS Unsealed Successfully.")
    except Exception as e:
        log("warn", "OpenBao KMS may already be unsealed. Continuing...")

    # 3. Login to Vault
    subprocess.check_call(["vault", "login", root_token], stdout=subprocess.DEVNULL)
    
    try:
        out = subprocess.check_output(["vault", "secrets", "list"], text=True)
        if "secret/" not in out:
            subprocess.check_call(["vault", "secrets", "enable", "-path=secret", "kv-v2"], stdout=subprocess.DEVNULL)
            log("info", "Successfully enabled Vault secret engine at secret/.")
        else:
            log("info", "Secret engine 'secret/' already exists. Skipping creation.")
    except Exception as e:
        log("error", f"Failed to interface with Vault: {e}")
        sys.exit(1)
        
    es_key = secrets.token_hex(32)
    log("info", "Generated Dynamic Elastic Token.")
    
    try:
        subprocess.check_call(
            ["vault", "kv", "put", "secret/flume/keys", f"ES_API_KEY={es_key}", f"OPENAI_API_KEY={openai_key}"],
            stdout=subprocess.DEVNULL
        )
        log("info", "Injected Matrix API keys into Vault KV-v2 secret/flume/keys block natively.")
    except Exception as e:
        log("error", f"Failed to push keys to Vault: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
