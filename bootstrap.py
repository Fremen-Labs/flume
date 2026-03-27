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
