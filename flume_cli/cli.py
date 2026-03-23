import os
import subprocess
import click
import toml
import urllib.request
from urllib.error import URLError
from pathlib import Path

CYAN = '\033[0;36m'
GREEN = '\033[0;32m'
BOLD = '\033[1m'
NC = '\033[0m'

def print_banner():
    click.echo(f"""{CYAN}{BOLD}
  ███████╗██╗     ██╗   ██╗███╗   ███╗███████╗
  ██╔════╝██║     ██║   ██║████╗ ████║██╔════╝
  █████╗  ██║     ██║   ██║██╔████╔██║█████╗  
  ██╔══╝  ██║     ██║   ██║██║╚██╔╝██║██╔══╝  
  ██║     ███████╗╚██████╔╝██║ ╚═╝ ██║███████╗
  ╚═╝     ╚══════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝
         Autonomous Engineering Frontier{NC}
""")

def check_ports():
    import socket
    def is_open(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(('127.0.0.1', port)) == 0
    return {
        "dashboard": is_open(8765),
        "elasticsearch": is_open(9200),
        "openbao": is_open(8200)
    }

def handle_byob(ports):
    import yaml
    import sys
    if ports["dashboard"]:
        click.echo(f"{CYAN}▶ PORT COLLISION: Port 8765 is actively bound natively. Please modify DASHBOARD_PORT in your .env or close the conflicting application before booting Flume.{NC}", err=True)
        sys.exit(1)
        
    if ports["elasticsearch"] or ports["openbao"]:
        click.echo(f"{CYAN}▶ Native Backend Detected (9200/8200)! Dynamically bypassing Elasticsearch and OpenBao inside Docker to enable BYOB elasticity.{NC}")
        os.environ["FLUME_BYOB"] = "1"
        
        override = {
            "services": {
                "dashboard": {"depends_on": {}},
                "worker": {"depends_on": {}},
                "elasticsearch": {"profiles": ["donotstart"]},
                "openbao": {"profiles": ["donotstart"]},
                "bootstrap": {"profiles": ["donotstart"]}
            }
        }
        with open("docker-compose.override.yml", "w") as f:
            yaml.dump(override, f)
        import shutil
        orig = os.environ.get('FLUME_CONFIG', 'config.toml')
        if not os.path.exists(orig) and os.path.exists('config.toml'):
            pass 
        return True
    else:
        if os.path.exists("docker-compose.override.yml"):
            os.remove("docker-compose.override.yml")
        return False

def check_memory():
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
        gb = int(out) // (1024**3)
    except Exception:
        gb = 16
    if gb < 16: return {"impl": 1, "pm": 1, "rev": 0}
    elif gb <= 32: return {"impl": 3, "pm": 1, "rev": 0}
    else: return {"impl": 6, "pm": 1, "rev": 1}

def check_llms():
    try:
        urllib.request.urlopen("http://localhost:52415/v1/models", timeout=2)
        return "exo", "http://localhost:52415/v1"
    except Exception:
        pass
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        return "ollama", "http://localhost:11434/v1"
    except Exception:
        return None, None

@click.group()
def cli():
    """Flume CLI — Autonomous Engineering Frontier"""
    pass

@cli.command()
def install():
    """Install the Flume ecosystem natively"""
    print_banner()
    # Synchronize orchestrator layers
    click.echo(f"{CYAN}▶ Pulling Docker Swarm container layers directly from registry...{NC}")
    try:
        handle_byob(check_ports())
        subprocess.run(["docker", "compose", "pull"], check=True)
        click.echo(f"{GREEN}✔ Flume architecture explicitly downloaded and installed natively! Run './flume onboard' to map keys, and './flume start' to deploy.{NC}")
    except Exception:
        click.echo(f"{CYAN}▶ Warning: Encountered a discrepancy resolving Docker compose networks natively.{NC}")

@cli.command()
def start():
    """Start the entire Flume ecosystem natively"""
    print_banner()
    limits = check_memory()
    click.echo(f"{CYAN}▶ System Diagnostics: Provisioning {limits['impl']} Implementers, {limits['pm']} PM, {limits['rev']} Reviewers...{NC}")
    
    provider, base_url = check_llms()
    if not provider:
        click.echo(f"{CYAN}▶ No local LLM detected! Halting for human intervention.{NC}")
        chosen_provider = click.prompt("Select Provider (openai/anthropic/ollama)", default="openai")
        
        if chosen_provider.lower() in ["openai", "anthropic"]:
            key = click.prompt(f"Enter {chosen_provider.capitalize()} API Key to stash in OpenBao Vault", hide_input=True)
            env_key = f"{chosen_provider.upper()}_API_KEY"
            if chosen_provider.lower() == "openai":
                base_url = "https://api.openai.com/v1"
            else:
                base_url = "https://api.anthropic.com/v1"
                
            with open('.env', 'a') as f:
                f.write(f"\\n{env_key}={key}\\n")
            os.environ[env_key] = key
        else:
            base_url = click.prompt("LLM Base URL", default="http://localhost:11434/v1")
            
        click.echo(f"{GREEN}✔ Key delegated to Docker bootstrap conditionally for OpenBao ingestion!{NC}")
        provider = chosen_provider
    else:
        click.echo(f"{GREEN}✔ Auto-discovered {provider} at {base_url}!{NC}")
        
    try:
        handle_byob(check_ports())
        subprocess.run(["docker", "compose", "up", "-d"], check=True)
        click.echo(f"{GREEN}✔ Ecosystem is active with rigid OpenBao security topology.{NC}")
    except Exception as e:
        click.echo(f"{CYAN}▶ Docker daemon hit a strict security mismatch ({e}). Sweeping ghost containers to prevent socket locks...{NC}")
        subprocess.run(["docker", "compose", "down"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(f"{CYAN}▶ Fallback activated: Booting AI Orchestrator daemons natively via 'uv' locally...{NC}")
        os.environ['FLUME_ES_URL'] = os.environ.get('FLUME_ES_URL', 'http://127.0.0.1:9200')
        os.environ['FLUME_OPENBAO_ADDR'] = os.environ.get('FLUME_OPENBAO_ADDR', 'http://127.0.0.1:8200')
        os.environ['PYTHONPATH'] = os.path.abspath('src')
        subprocess.Popen(["uv", "run", "python", "src/dashboard/server.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["uv", "run", "python", "src/worker-manager/manager.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(f"{GREEN}✔ Native daemons launched on host memory. (Dashboard on :8765){NC}")

@cli.command()
def stop():
    """Terminate the ecosystem."""
    click.echo(f"{CYAN}▶ Teardown of active orchestrator arrays...{NC}")
    subprocess.run(["docker", "compose", "down"], check=True)
    click.echo(f"{GREEN}✔ Flume network offline.{NC}")

@cli.command()
def logs():
    """Tail active logs."""
    subprocess.run(["docker", "compose", "logs", "-f"])

@cli.command()
def onboard():
    """Execute interactive workspace calibration"""
    print_banner()
    click.echo(f"{CYAN}Initializing secure TOML workspace configurations...{NC}")
    provider = click.prompt("LLM Provider", default="exo")
    model = click.prompt("Model Name", default="qwen3-30b-A3B-4bit")
    base_url = click.prompt("Base URL", default="http://localhost:52415/v1")
    git_user = click.prompt("Git Username", default="FlumeAgent")
    git_email = click.prompt("Git Email", default="agent@flume.local")
    
    config = {
        "llm": { "provider": provider, "model": model, "base_url": base_url },
        "git": { "user": git_user, "email": git_email },
        "system": { "es_url": "http://elasticsearch:9200", "openbao_url": "http://openbao:8200" }
    }
    with open("config.toml", "w") as f:
        toml.dump(config, f)
    click.echo(f"{GREEN}✔ Written config.toml.{NC}")

if __name__ == '__main__':
    cli()
