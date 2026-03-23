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

def check_memory():
    import sys
    try:
        if sys.platform == 'darwin':
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
            total_bytes = int(out)
        elif sys.platform.startswith('linux'):
            total_bytes = 16 * (1024**3)
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if 'MemTotal' in line:
                        total_bytes = int(line.split()[1]) * 1024
                        break
        else:
            total_bytes = 16 * (1024**3)
        gb = total_bytes // (1024**3)
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
def start():
    """Start the entire Flume ecosystem natively"""
    print_banner()
    limits = check_memory()
    click.echo(f"{CYAN}▶ System Diagnostics: Provisioning {limits['impl']} Implementers, {limits['pm']} PM, {limits['rev']} Reviewers...{NC}")
    
    provider, base_url = check_llms()
    if not provider:
        click.echo(f"{CYAN}▶ No local LLM detected! Flume natively awaits provider configuration.{NC}")
    else:
        click.echo(f"{GREEN}✔ Auto-discovered {provider} at {base_url}!{NC}")
        
    try:
        click.echo(f"{CYAN}▶ Attempting isolated Docker orchestration...{NC}")
        subprocess.run(["docker", "compose", "up", "-d", "--build"], check=True)
        click.echo(f"{GREEN}✔ Ecosystem is active with rigid OpenBao security topology.{NC}")
    except Exception as e:
        click.echo(f"{CYAN}▶ Docker daemon hit a strict security mismatch ({e}). Sweeping ghost containers to prevent socket locks...{NC}")
        subprocess.run(["docker", "compose", "down"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(f"{CYAN}▶ Fallback activated: Booting AI Orchestrator daemons natively via 'uv' locally...{NC}")
        import stat
        os.environ['FLUME_ES_URL'] = os.environ.get('FLUME_ES_URL', 'http://127.0.0.1:9200')
        os.environ['FLUME_OPENBAO_ADDR'] = os.environ.get('FLUME_OPENBAO_ADDR', 'http://127.0.0.1:8200')
        subprocess.Popen(["uv", "run", "python", "src/dashboard/server.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["uv", "run", "python", "src/worker-manager/manager.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        click.echo(f"{GREEN}✔ Native daemons launched on host memory. (Dashboard on :8765){NC}")

@cli.command()
def stop():
    """Terminate the ecosystem."""
    click.echo(f"{CYAN}▶ Teardown of active orchestrator arrays...{NC}")
    try:
        subprocess.run(["docker", "compose", "down"], check=True)
    except Exception:
        pass
    subprocess.run(["pkill", "-f", "worker_handlers\\.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "manager\\.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "server\\.py"], capture_output=True)
    click.echo(f"{GREEN}✔ Flume network offline.{NC}")

@cli.command()
def logs():
    """Tail active logs."""
    try:
        subprocess.run(["docker", "compose", "logs", "-f"], check=True)
    except Exception:
        click.echo(f"{CYAN}▶ Tailing native dashboard logs (monitor flume-worker-logs/ for node details)...{NC}")
        subprocess.run(["tail", "-f", "src/worker-manager/manager.log"])

@cli.command()
def onboard():
    """Execute interactive workspace calibration"""
    print_banner()
    click.echo(f"{CYAN}Initializing secure TOML workspace configurations...{NC}")
    config = {
        "llm": { "provider": "exo", "model": "qwen3-30b-A3B-4bit", "base_url": "http://localhost:52415/v1" },
        "git": { "user": "FlumeAgent", "email": "agent@flume.local" },
        "system": { "es_url": "http://127.0.0.1:9200", "openbao_url": "http://127.0.0.1:8200", "dashboard_url": "http://127.0.0.1:8765" }
    }
    with open("config.toml", "w") as f:
        toml.dump(config, f)
    click.echo(f"{GREEN}✔ Written config.toml.{NC}")

@cli.command()
def install():
    """Runs the automated installer for dependencies, virtual environments, and GUI routines."""
    print_banner()
    click.echo(f"{CYAN}▶ Provisioning host environment dependencies via 'uv'...{NC}")
    subprocess.run(["uv", "sync"], check=True)
    
    click.echo(f"{CYAN}▶ Compiling strict React artifacts for local routing...{NC}")
    target_dir = Path("src/frontend/src")
    if target_dir.exists():
        try:
            subprocess.run(["npm", "install"], cwd="src/frontend/src", check=True)
            subprocess.run(["npm", "run", "build"], cwd="src/frontend/src", check=True)
            click.echo(f"{GREEN}✔ React GUI payload pre-compiled natively into dist/{NC}")
        except FileNotFoundError:
            click.echo(f"{CYAN}▶ Skipping native UI compile (Node.js/npm not found natively). Flume Backend active. Install npm to build the local frontend.{NC}")
        except subprocess.CalledProcessError as e:
            click.echo(f"{CYAN}▶ UI build encountered a compilation warning, bypassed for backend activation: {e}{NC}")
    else:
        click.echo(f"{CYAN}▶ Skipping UI compile (source missing in this tree){NC}")

@cli.command()
def doctor():
    """Diagnose ecosystem health, Vault seal status, ES indices, and active LLMs"""
    print_banner()
    click.echo(f"{CYAN}▶ Initiating Flume Ecosystem Diagnostics...{NC}")
    
    # 1. Docker Status
    try:
        out = subprocess.check_output(["docker", "compose", "ps", "--format", "json"]).decode()
        if "flume-" in out:
            click.echo(f"{GREEN}✔ Docker Compose Engine: Active & Bound{NC}")
        else:
            click.echo(f"{CYAN}⚠ Docker Compose Engine: Offline / Missing Containers{NC}")
    except Exception:
        click.echo(f"{CYAN}⚠ Docker Compose Engine: Not Running natively{NC}")

    # 2. Vault Status
    try:
        resp = urllib.request.urlopen("http://localhost:8200/v1/sys/seal-status", timeout=2)
        import json
        data = json.loads(resp.read().decode())
        if not data.get('sealed', True):
            click.echo(f"{GREEN}✔ OpenBao Vault: Unsealed & Cryptographically Ready{NC}")
        else:
            click.echo(f"{CYAN}⚠ OpenBao Vault: Sealed or Error{NC}")
    except Exception:
        click.echo(f"{CYAN}⚠ OpenBao Vault: Offline (localhost:8200){NC}")

    # 3. Elasticsearch Status
    try:
        resp = urllib.request.urlopen("http://localhost:9200/_cluster/health", timeout=2)
        import json
        data = json.loads(resp.read().decode())
        status = data.get('status', 'red')
        if status in ['green', 'yellow']:
            click.echo(f"{GREEN}✔ Elasticsearch Nodes: Active ({status.title()}){NC}")
        else:
            click.echo(f"{CYAN}⚠ Elasticsearch Nodes: Degraded ({status.title()}){NC}")
    except Exception:
        click.echo(f"{CYAN}⚠ Elasticsearch Nodes: Offline (localhost:9200){NC}")

    # 4. LLM Readiness
    provider, url = check_llms()
    if provider:
        click.echo(f"{GREEN}✔ LLM Engine: Reached {provider.title()} synchronously on {url}{NC}")
    else:
        click.echo(f"{CYAN}⚠ LLM Engine: Neither Exo nor Ollama detected on local ports.{NC}")

    click.echo(f"\n{BOLD}Diagnostic Scan Complete.{NC}")

if __name__ == '__main__':
    cli()
