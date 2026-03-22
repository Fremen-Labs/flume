import os
import shutil
import subprocess
import click
import toml
from pathlib import Path

CYAN = '\033[0;36m'
GREEN = '\033[0;32m'
YELLOW = '\033[0;33m'
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

@click.group()
def cli():
    """Flume CLI — Autonomous Engineering Frontier"""
    pass

@cli.command("native")
def native_cmd():
    """Start dashboard + worker-manager on the host (use when ES/OpenBao run locally, not via Compose)."""
    print_banner()
    root = Path(__file__).resolve().parent.parent
    script = root / "install" / "setup" / "run-native.sh"
    if not script.is_file():
        click.echo(f"{YELLOW}Missing {script}{NC}", err=True)
        raise SystemExit(1)
    rc = subprocess.run(["bash", str(script)], cwd=root).returncode
    if rc != 0:
        raise SystemExit(rc)
    click.echo(f"{GREEN}✔ Native processes started (see logs/ under the repo root).{NC}")


@cli.command()
def start():
    """Start the entire Flume ecosystem natively via Docker Compose (Netflix Architecture)"""
    print_banner()
    click.echo(f"{CYAN}▶ Booting global Docker infrastructure...{NC}")
    try:
        subprocess.run(["docker", "compose", "up", "-d"], check=True)
        click.echo(f"{GREEN}✔ Ecosystem is active and scaled natively across all nodes.{NC}")
    except FileNotFoundError:
        # Fallback if docker isn't installed
        click.echo(f"{CYAN}▶ Docker command missing. Initializing Native Process Swarms...{NC}")
        os.environ["FLUME_AUTO_START_WORKERS"] = "1"
        subprocess.Popen(["uv", "run", "python", "src/dashboard/app.py"])
        subprocess.Popen(["uv", "run", "python", "src/worker-manager/manager.py"])
        click.echo(f"{GREEN}✔ Native Dashboard and OS Swarm spawned autonomously.{NC}")
    except subprocess.CalledProcessError:
        # Fallback if docker is installed but daemon is offline
        click.echo(f"{CYAN}▶ Docker daemon offline. Booting Native OS Swarm Matrix...{NC}")
        os.environ["FLUME_AUTO_START_WORKERS"] = "1"
        subprocess.Popen(["uv", "run", "python", "src/dashboard/app.py"])
        subprocess.Popen(["uv", "run", "python", "src/worker-manager/manager.py"])
        click.echo(f"{GREEN}✔ Native Dashboard and OS Swarm spawned autonomously.{NC}")

@cli.command()
def stop():
    """Terminate the ecosystem and flush parallel agents."""
    click.echo(f"{CYAN}▶ Teardown of active orchestrator arrays...{NC}")
    subprocess.run(["docker", "compose", "down"], check=True)
    click.echo(f"{GREEN}✔ Flume network offline.{NC}")

@cli.command()
def logs():
    """Tail active orchestration mesh logs globally."""
    subprocess.run(["docker", "compose", "logs", "-f"])

@cli.command("install")
def install_cmd():
    """Install Python deps, verify system tools, build the dashboard UI, and bootstrap ES indices when configured."""
    print_banner()
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    click.echo(f"{CYAN}▶ uv sync…{NC}")
    subprocess.run(["uv", "sync"], cwd=root, check=True)
    click.echo(f"{CYAN}▶ Verifying system dependencies…{NC}")
    verify = subprocess.run(
        ["bash", str(root / "install" / "setup" / "verify-deps.sh")],
        cwd=root,
    )
    if verify.returncode != 0:
        click.echo(
            f"{YELLOW}verify-deps exited {verify.returncode} (see messages above). Continuing.{NC}"
        )
    fe = root / "src" / "frontend" / "src"
    if (fe / "package.json").exists():
        if shutil.which("npm"):
            click.echo(f"{CYAN}▶ npm install (frontend)…{NC}")
            subprocess.run(["npm", "install"], cwd=fe, check=False)
            click.echo(f"{CYAN}▶ npm run build (frontend)…{NC}")
            subprocess.run(["npm", "run", "build"], cwd=fe, check=False)
        else:
            click.echo(f"{YELLOW}npm not on PATH — skipping frontend build.{NC}")
    create_script = root / "install" / "setup" / "create-es-indices.sh"
    if (root / ".env").exists() or (root / "flume.config.json").exists():
        click.echo(f"{CYAN}▶ Elasticsearch indices…{NC}")
        es = subprocess.run(["bash", str(create_script)], cwd=root, env=os.environ.copy())
        if es.returncode != 0:
            click.echo(
                f"{YELLOW}Index creation failed or ES not configured. "
                f"Set ES_URL + ES_API_KEY in .env or OpenBao (secret/flume), then run:{NC}\n"
                f"  bash install/setup/create-es-indices.sh"
            )
    else:
        click.echo(
            f"{YELLOW}No .env or flume.config.json — skipping Elasticsearch index creation.{NC}"
        )
    click.echo(f"{GREEN}✔ install completed.{NC}")


@cli.command()
def onboard():
    """Execute interactive workspace calibration synthesizing TOML arrays securely out of the box."""
    print_banner()
    click.echo(f"{CYAN}Initializing secure TOML workspace configurations...{NC}")
    
    config = {
        "llm": {
            "provider": "exo",
            "model": "qwen3-30b-A3B-4bit",
            "base_url": "http://host.docker.internal:52415/v1"
        },
        "git": {
            "user": "FlumeAgent",
            "email": "agent@flume.local"
        },
        "system": {
            "es_url": "http://elasticsearch:9200",
            "openbao_url": "http://openbao:8200"
        }
    }
    
    with open("config.toml", "w") as f:
        toml.dump(config, f)
        
    click.echo(f"{GREEN}✔ Written optimized parameters spanning config.toml natively.{NC}")

if __name__ == '__main__':
    cli()
