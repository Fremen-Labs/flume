import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import click
import toml
from pathlib import Path

from flume_cli.interactive_install import (
    CredentialMode,
    elasticsearch_reachable,
    ensure_platform_dependencies,
    is_interactive_tty,
    prompt_oauth_flow,
    prompt_run_es_indices,
    run_credential_wizard,
)

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

def _flume_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_codex_oauth_script(root: Path, args: tuple[str, ...]) -> int:
    script = root / "install" / "setup" / "codex_oauth_login.py"
    if not script.is_file():
        click.echo(f"{YELLOW}Missing {script}{NC}", err=True)
        return 1
    env = os.environ.copy()
    env.setdefault("FLUME_WORKSPACE_ROOT", str(root))
    src = root / "src"
    if src.is_dir():
        prev = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = str(src) if not prev else f"{src}{os.pathsep}{prev}"
    if shutil.which("uv") and (root / "pyproject.toml").is_file():
        cmd = ["uv", "run", "python", str(script), *args]
    else:
        cmd = ["python3", str(script), *args]
    return subprocess.run(cmd, cwd=root, env=env).returncode


def _docker_dashboard_exec_json(root: Path, py_expr: str) -> dict | None:
    compose_up = _docker_compose_running_services(root)
    if 'dashboard' not in compose_up:
        return None
    cmd = [
        'docker', 'exec', 'flume-dashboard', 'python', '-c',
        (
            'import json; '
            'from pathlib import Path; '
            'from src.dashboard.codex_app_server import status, start_background_if_needed; '
            f'result = ({py_expr}); '
            'print(json.dumps(result))'
        ),
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads((proc.stdout or '').strip())
    except Exception:
        return None


def _bootstrap_codex_app_server(root: Path, *, quiet: bool = False) -> None:
    result = _docker_dashboard_exec_json(root, 'start_background_if_needed(Path("/app"))')
    if result is not None:
        for _ in range(6):
            st = _docker_dashboard_exec_json(root, 'status()') or {}
            if st.get('tcpReachable') is True:
                result = {'started': False, 'reason': 'already_running', 'listenUrl': st.get('listenUrl')}
                break
            time.sleep(0.5)
        if not quiet:
            if result.get('started'):
                click.echo(
                    f"{GREEN}✔ Codex app-server started in dashboard container on {result.get('listenUrl')}{NC}"
                )
                click.echo(f"{CYAN}  Log: {result.get('logPath')}{NC}")
            elif result.get('reason') == 'already_running':
                click.echo(
                    f"{CYAN}Codex app-server already reachable at {result.get('listenUrl')}.{NC}"
                )
            elif result.get('reason') == 'no_codex_npx':
                click.echo(
                    f"{YELLOW}Install Codex CLI or Node/npm (npx) to run the app-server; see {CYAN}install/README.md{YELLOW} (OAuth / Codex).{NC}"
                )
            elif result.get('reason') not in (None, 'already_running'):
                click.echo(
                    f"{YELLOW}Codex app-server not started: {result.get('reason')} {result.get('error', '')}{NC}".rstrip(),
                    err=True,
                )
        return

    src = root / "src"
    if not src.is_dir():
        return
    old_path = sys.path[:]
    try:
        sys.path.insert(0, str(src))
        from dashboard.codex_app_server import start_background_if_needed
    except Exception as exc:
        if not quiet:
            click.echo(
                f"{YELLOW}Codex app-server bootstrap skipped (import error: {exc}).{NC}",
                err=True,
            )
        return
    finally:
        sys.path[:] = old_path
    try:
        result = start_background_if_needed(root)
    except Exception as exc:
        if not quiet:
            click.echo(f"{YELLOW}Codex app-server bootstrap failed: {exc}{NC}", err=True)
        return
    if not quiet:
        if result.get("started"):
            click.echo(
                f"{GREEN}✔ Codex app-server started (pid {result.get('pid')}) "
                f"on {result.get('listenUrl')}{NC}"
            )
            click.echo(f"{CYAN}  Log: {result.get('logPath')}{NC}")
        elif result.get("reason") == "already_running":
            click.echo(
                f"{CYAN}Codex app-server already reachable at {result.get('listenUrl')}.{NC}"
            )
        elif result.get("reason") == "no_codex_npx":
            click.echo(
                f"{YELLOW}Install Codex CLI or Node/npm (npx) to run the app-server; "
                f"see {CYAN}install/README.md{YELLOW} (OAuth / Codex).{NC}"
            )
        elif result.get("reason") not in (None, "already_running"):
            click.echo(
                f"{YELLOW}Codex app-server not started: {result.get('reason')} "
                f"{result.get('error', '')}{NC}".rstrip(),
                err=True,
            )


def _restart_codex_app_server_after_oauth(root: Path) -> None:
    pid_path = root / "logs" / "codex-app-server.pid"
    if pid_path.is_file():
        try:
            pid = int((pid_path.read_text(encoding="utf-8") or "0").strip())
            if pid > 0:
                os.kill(pid, 15)
                time.sleep(0.4)
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
    # After OAuth, show app-server outcome (PATH may now match codex/npx in /usr/bin, etc.).
    _bootstrap_codex_app_server(root, quiet=False)


def _run_codex_oauth_script_with_restart(root: Path, args: tuple[str, ...]) -> int:
    rc = _run_codex_oauth_script(root, args)
    if rc == 0 and args and args[0] in (
        "login",
        "login-browser",
        "login-paste",
        "import-codex",
    ):
        _restart_codex_app_server_after_oauth(root)
    return rc


def _pkill_flume_script(root: Path, relative_script: str) -> None:
    path = (root / relative_script).resolve()
    pat = re.escape(str(path))
    subprocess.run(["pkill", "-f", pat], check=False)


def _docker_compose_running_services(root: Path) -> set[str]:
    proc = subprocess.run(
        ["docker", "compose", "ps", "--services", "--filter", "status=running"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return set()
    return {s.strip() for s in proc.stdout.splitlines() if s.strip()}


def _systemd_user_flume_dashboard_unit() -> Path | None:
    unit = Path.home() / ".config" / "systemd" / "user" / "flume-dashboard.service"
    return unit if unit.is_file() else None


def _restart_dashboard_systemd() -> bool:
    if not shutil.which("systemctl"):
        return False
    if _systemd_user_flume_dashboard_unit() is None:
        return False
    r = subprocess.run(
        ["systemctl", "--user", "restart", "flume-dashboard"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def _nohup_native_dashboard(root: Path) -> bool:
    log = root / "logs" / "dashboard.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not shutil.which("uv"):
        click.echo(f"{YELLOW}uv not on PATH — cannot start dashboard.{NC}", err=True)
        return False
    env = os.environ.copy()
    sp = str(root / "src")
    env["PYTHONPATH"] = sp if not env.get("PYTHONPATH") else f"{sp}{os.pathsep}{env['PYTHONPATH']}"
    try:
        log_f = open(log, "ab", buffering=0)
    except OSError as exc:
        click.echo(f"{YELLOW}Cannot open log {log}: {exc}{NC}", err=True)
        return False
    try:
        subprocess.Popen(
            ["uv", "run", "python", "-u", str(root / "src" / "dashboard" / "server.py")],
            cwd=str(root),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        click.echo(f"{YELLOW}Failed to start dashboard: {exc}{NC}", err=True)
        return False
    click.echo(f"{GREEN}✔ Dashboard restarted → {log}{NC}")
    return True


def _run_frontend_build(root: Path) -> bool:
    fe = root / "src" / "frontend" / "src"
    if not (fe / "package.json").is_file():
        return True
    if not shutil.which("npm"):
        click.echo(f"{YELLOW}npm not on PATH — skipping UI build.{NC}", err=True)
        return False
    click.echo(f"{CYAN}▶ npm run build (frontend)…{NC}")
    b = subprocess.run(["npm", "run", "build"], cwd=fe)
    return b.returncode == 0


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
    click.echo(f"{CYAN}▶ Booting global Docker infrastructure...{NC}")
    try:
        subprocess.run(["docker", "compose", "up", "-d"], check=True)
        es_host = os.environ.get("FLUME_ES_HOST_PORT", "9201")
        dash_host = os.environ.get("FLUME_DASHBOARD_HOST_PORT", "8765")
        click.echo(f"{GREEN}✔ Ecosystem is active and scaled natively across all nodes.{NC}")
        click.echo(
            f"{CYAN}From the host, bundled Elasticsearch is at http://127.0.0.1:{es_host}/ "
            f"(containers still use http://elasticsearch:9200).{NC}"
        )
        click.echo(f"{CYAN}Dashboard (host): http://127.0.0.1:{dash_host}/{NC}")
    except FileNotFoundError:
        click.echo(f"{CYAN}▶ Docker command missing. Falling back to native Flume processes...{NC}")
        os.environ["FLUME_AUTO_START_WORKERS"] = "1"
        subprocess.Popen(["uv", "run", "python", "src/dashboard/app.py"])
        subprocess.Popen(["uv", "run", "python", "src/worker-manager/manager.py"])
        click.echo(f"{GREEN}✔ Native Dashboard and OS Swarm spawned autonomously.{NC}")
    except subprocess.CalledProcessError:
        click.echo(f"{CYAN}▶ Docker unavailable or compose failed. Falling back to native Flume processes...{NC}")
        es_port = os.environ.get("FLUME_ES_HOST_PORT", "9201")
        bao_port = os.environ.get("FLUME_OPENBAO_HOST_PORT", "8200")
        dash_port = os.environ.get("FLUME_DASHBOARD_HOST_PORT", "8765")
        click.echo(
            f"{YELLOW}If host ports are in use, try {GREEN}./flume native{NC} (Linux/macOS host mode) or set "
            f"{GREEN}FLUME_ES_HOST_PORT{NC}/{GREEN}FLUME_OPENBAO_HOST_PORT{NC}/"
            f"{GREEN}FLUME_DASHBOARD_HOST_PORT{NC} "
            f"(ES→{es_port}, OpenBao→{bao_port}, dashboard→{dash_port}).{NC}"
        )
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


def _run_create_es_indices_script(root: Path, *, non_interactive: bool = False) -> int:
    script = root / "install" / "setup" / "create-es-indices.sh"
    if not script.is_file():
        click.echo(f"{YELLOW}Missing {script}{NC}", err=True)
        return 1
    env = os.environ.copy()
    if non_interactive:
        env["FLUME_NON_INTERACTIVE"] = "1"
    return subprocess.run(["bash", str(script)], cwd=root, env=env).returncode


@cli.command("es-indices")
def es_indices_cmd():
    """Create or update Flume Elasticsearch indices (same step as during install)."""
    root = _flume_repo_root()
    os.chdir(root)
    click.echo(f"{CYAN}▶ Elasticsearch indices…{NC}")
    rc = _run_create_es_indices_script(root)
    if rc != 0:
        click.echo(
            f"{YELLOW}Index setup failed. Use a host-reachable ES_URL (not http://elasticsearch:9200 on the host) "
            f"and a valid ES_API_KEY, then run:{NC} {GREEN}./flume es-indices{NC}",
            err=True,
        )
        raise SystemExit(rc)
    click.echo(f"{GREEN}✔ Elasticsearch indices are ready.{NC}")


@cli.command("restart")
@click.option(
    "--all",
    "all_services",
    is_flag=True,
    help="Restart dashboard and worker-manager (Docker: dashboard+worker; native: kill both and run-native).",
)
@click.option(
    "--build-ui",
    "build_ui",
    is_flag=True,
    help="Run npm run build in the frontend before restarting.",
)
def restart_cmd(all_services: bool, build_ui: bool):
    """Restart Flume dashboard (and with --all, workers). Matches install/README.md and Settings restart."""
    root = _flume_repo_root()
    os.chdir(root)
    if build_ui and not _run_frontend_build(root):
        raise SystemExit(1)

    compose_up = _docker_compose_running_services(root)
    if "dashboard" in compose_up:
        svcs = ["dashboard"]
        if all_services and "worker" in compose_up:
            svcs.append("worker")
        elif all_services and "worker" not in compose_up:
            click.echo(
                f"{YELLOW}Compose worker service not running — restarting dashboard only.{NC}",
                err=True,
            )
        click.echo(f"{CYAN}▶ docker compose restart {' '.join(svcs)}…{NC}")
        rc = subprocess.run(["docker", "compose", "restart", *svcs], cwd=root).returncode
        if rc != 0:
            raise SystemExit(rc)
        click.echo(f"{GREEN}✔ Docker services restarted: {', '.join(svcs)}{NC}")
        return

    if all_services:
        click.echo(f"{CYAN}▶ Restarting native dashboard + worker-manager…{NC}")
        _pkill_flume_script(root, "src/dashboard/server.py")
        _pkill_flume_script(root, "src/worker-manager/manager.py")
        time.sleep(0.8)
        script = root / "install" / "setup" / "run-native.sh"
        if not script.is_file():
            click.echo(f"{YELLOW}Missing {script}{NC}", err=True)
            raise SystemExit(1)
        rc = subprocess.run(["bash", str(script)], cwd=root).returncode
        if rc != 0:
            raise SystemExit(rc)
        click.echo(f"{GREEN}✔ Native Flume processes restarted.{NC}")
        return

    if _restart_dashboard_systemd():
        click.echo(f"{GREEN}✔ Restarted systemd user unit flume-dashboard.{NC}")
        return

    click.echo(f"{CYAN}▶ Restarting native dashboard only…{NC}")
    _pkill_flume_script(root, "src/dashboard/server.py")
    time.sleep(0.5)
    if not _nohup_native_dashboard(root):
        raise SystemExit(1)
    click.echo(
        f"{YELLOW}Worker-manager was not restarted. After LLM/OAuth/code changes run: "
        f"{GREEN}./flume restart --all{NC}"
    )


def _host_port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _platform_family() -> str:
    sysname = platform.system().lower()
    if sysname == 'darwin':
        return 'macos'
    if sysname == 'linux':
        return 'linux'
    return sysname or 'unknown'


def _linux_native_supported() -> bool:
    return _platform_family() == 'linux'


def _host_es_credentials_look_configured(root: Path) -> bool:
    """Best-effort check that native host Elasticsearch on :9200 is actually usable.

    We do not require a live auth round-trip here, but we do require *some* credible
    credential source instead of just an open TCP port.
    """
    env_api_key = (os.environ.get("ES_API_KEY") or "").strip()
    if env_api_key and env_api_key != "AUTO_GENERATED_BY_INSTALLER":
        return True

    env_path = root / ".env"
    if env_path.is_file():
        try:
            for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "ES_API_KEY":
                    val = v.strip().strip('"').strip("'")
                    if val and val != "AUTO_GENERATED_BY_INSTALLER":
                        return True
        except OSError:
            pass

    for extra in (root / "install" / ".es-bootstrap.env", root / "install" / ".elastic-admin.env"):
        if extra.is_file():
            return True

    cfg = root / "flume.config.json"
    if cfg.is_file():
        try:
            obj = json.loads(cfg.read_text(encoding="utf-8"))
            ob = obj.get("openbao") or {}
            tf = str(ob.get("tokenFile") or "").strip()
            addr = str(ob.get("addr") or "").strip()
            if tf:
                token_path = Path(os.path.expanduser(tf))
                if token_path.is_file() and addr:
                    return True
        except Exception:
            pass

    return False


def _host_es_native_ready(root: Path) -> bool:
    return elasticsearch_reachable() and _host_es_credentials_look_configured(root)


def _post_install_start_flume(root: Path) -> None:
    """
    Bring up dashboard + workers: native run-native.sh only if host ES on :9200 looks reachable *and*
    credentials appear configured; otherwise Docker Compose. Skips if Compose dashboard is already
    running or port 8765 is taken.
    """
    compose_up = _docker_compose_running_services(root)
    if "dashboard" in compose_up:
        dash_port = os.environ.get("FLUME_DASHBOARD_HOST_PORT", "8765")
        es_port = os.environ.get("FLUME_ES_HOST_PORT", "9201")
        click.echo(
            f"{CYAN}Docker Compose dashboard already running — "
            f"http://127.0.0.1:{dash_port}/{NC}"
        )
        click.echo(
            f"{CYAN}Elasticsearch (mapped host port): http://127.0.0.1:{es_port}/{NC}"
        )
        return

    if _host_port_in_use("127.0.0.1", 8765):
        click.echo(
            f"{CYAN}Port 8765 is already in use — skipping auto-start. "
            f"If that is Flume: {GREEN}http://127.0.0.1:8765/{NC}"
        )
        return

    script = root / "install" / "setup" / "run-native.sh"
    if _host_es_native_ready(root):
        if not script.is_file():
            click.echo(f"{YELLOW}Missing {script}; cannot start native Flume.{NC}", err=True)
            return
        click.echo(f"{CYAN}▶ Starting Flume on the host (Elasticsearch on localhost:9200 with credentials detected)…{NC}")
        rc = subprocess.run(["bash", str(script)], cwd=root).returncode
        if rc != 0:
            click.echo(
                f"{YELLOW}Native start exited {rc}. Try {GREEN}./flume native{YELLOW} manually.{NC}",
                err=True,
            )
        else:
            click.echo(
                f"{GREEN}✔ Dashboard:{NC} {CYAN}http://127.0.0.1:8765/{NC}  "
                f"{CYAN}(logs: {root / 'logs'}){NC}"
            )
        return

    if shutil.which("docker"):
        click.echo(f"{CYAN}▶ Starting Flume with Docker Compose…{NC}")
        try:
            subprocess.run(["docker", "compose", "up", "-d"], cwd=root, check=True)
        except subprocess.CalledProcessError:
            es_port = os.environ.get("FLUME_ES_HOST_PORT", "9201")
            dash_port = os.environ.get("FLUME_DASHBOARD_HOST_PORT", "8765")
            bao_port = os.environ.get("FLUME_OPENBAO_HOST_PORT", "8200")
            click.echo(
                f"{YELLOW}docker compose up failed. Try {GREEN}./flume native{YELLOW} with host services, "
                f"or set {GREEN}FLUME_ES_HOST_PORT{YELLOW}, {GREEN}FLUME_OPENBAO_HOST_PORT{YELLOW}, "
                f"{GREEN}FLUME_DASHBOARD_HOST_PORT{YELLOW} "
                f"(defaults: ES→{es_port}, OpenBao→{bao_port}, dashboard→{dash_port}).{NC}",
                err=True,
            )
            if script.is_file():
                click.echo(f"{CYAN}▶ Falling back to native start…{NC}")
                rc = subprocess.run(["bash", str(script)], cwd=root).returncode
                if rc == 0:
                    click.echo(f"{GREEN}✔ Native processes started.{NC}")
                else:
                    click.echo(
                        f"{YELLOW}Native start also failed ({rc}). Run {GREEN}./flume start{YELLOW} again after fixing Docker, or "
                        f"{GREEN}./flume native{YELLOW} after fixing host prerequisites.{NC}",
                        err=True,
                    )
            return
        es_host = os.environ.get("FLUME_ES_HOST_PORT", "9201")
        dash_host = os.environ.get("FLUME_DASHBOARD_HOST_PORT", "8765")
        click.echo(f"{GREEN}✔ Docker stack is up.{NC}")
        click.echo(f"{CYAN}Dashboard:{NC} http://127.0.0.1:{dash_host}/")
        click.echo(
            f"{CYAN}Elasticsearch (host):{NC} http://127.0.0.1:{es_host}/ "
            f"{CYAN}(containers use http://elasticsearch:9200){NC}"
        )
        return

    if not script.is_file():
        click.echo(
            f"{YELLOW}Docker not found and {script} missing — install Docker or start the stack manually.{NC}",
            err=True,
        )
        return
    click.echo(
        f"{YELLOW}Docker not found; starting Flume natively (ensure ES_URL / ES_API_KEY are set). "
        f"This host-mode path is intended for Linux and macOS, not Windows.{NC}"
    )
    rc = subprocess.run(["bash", str(script)], cwd=root).returncode
    if rc != 0:
        click.echo(
            f"{YELLOW}Native start exited {rc}. Install Docker for `./flume start` or fix Elasticsearch.{NC}",
            err=True,
        )
    else:
        click.echo(f"{GREEN}✔ Dashboard:{NC} {CYAN}http://127.0.0.1:8765/{NC}")


@cli.command("install")
@click.option(
    "--yes",
    "-y",
    "non_interactive",
    is_flag=True,
    help="Non-interactive install (no prompts; typical for CI). Implies auto-start unless --no-start.",
)
@click.option(
    "--no-start",
    is_flag=True,
    help="Do not start the dashboard/workers after install (build deps only).",
)
def install_cmd(non_interactive: bool, no_start: bool):
    """Install Python deps, verify system tools, build the dashboard UI, and bootstrap ES indices when configured.

    Interactive installs: choosing OpenBao prompts to install the OpenBao CLI and native Elasticsearch
    (sudo) when missing; choosing .env prompts for Elasticsearch when no cluster is detected on localhost.
    Use ``-y``/``--yes`` to skip prompts (CI); provision ES and secrets yourself.
    After install, the dashboard and workers start automatically unless ``--no-start`` is set.
    """
    print_banner()
    root = _flume_repo_root()
    os.chdir(root)
    interactive = not non_interactive and is_interactive_tty()
    credential_mode: CredentialMode = "skip"
    if interactive:
        click.echo(
            f"{CYAN}A few questions first (use {GREEN}--yes{CYAN}/{GREEN}-y{CYAN} to skip and use defaults).{NC}\n"
        )
        credential_mode = run_credential_wizard(root)
        ensure_platform_dependencies(root, credential_mode)

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

    has_cred_files = (root / ".env").exists() or (root / "flume.config.json").exists()
    if interactive:
        want_es = prompt_run_es_indices(root, credential_mode)
    else:
        want_es = has_cred_files

    if want_es:
        click.echo(f"{CYAN}▶ Elasticsearch indices…{NC}")
        es_rc = _run_create_es_indices_script(root, non_interactive=non_interactive)
        if es_rc != 0:
            click.echo(
                f"{YELLOW}Index setup failed. Interactive installs can fix ES_URL and create an API key from "
                f"the elastic password automatically when you run {GREEN}./flume es-indices{YELLOW} in a terminal.{NC}"
            )
            if non_interactive:
                click.echo(
                    f"{YELLOW}Non-interactive (-y): set a host ES_URL and valid ES_API_KEY in OpenBao or .env first.{NC}"
                )
    elif not interactive:
        click.echo(
            f"{YELLOW}No .env or flume.config.json — skipping Elasticsearch index creation.{NC}"
        )

    click.echo(f"{CYAN}▶ Codex app-server (OAuth helper)…{NC}")
    _bootstrap_codex_app_server(root, quiet=False)

    oauth_prompted = False
    if interactive:
        oauth_prompted = prompt_oauth_flow(root, _run_codex_oauth_script_with_restart)

    if not no_start:
        if interactive:
            click.echo("")
            if click.confirm(
                f"{BOLD}Start the Flume dashboard and workers now?{NC}",
                default=True,
            ):
                click.echo("")
                _post_install_start_flume(root)
        else:
            click.echo("")
            _post_install_start_flume(root)

    click.echo(f"{GREEN}✔ install completed.{NC}")
    if not oauth_prompted:
        click.echo(
            f"{CYAN}OAuth for OpenAI / Codex: {GREEN}./flume setup{CYAN} or "
            f"{GREEN}./flume codex-oauth login-browser{NC}"
        )
    click.echo(
        f"{CYAN}After changing secrets or OAuth, reload processes: {GREEN}./flume restart{NC} "
        f"or {GREEN}./flume restart --all{NC}"
    )


@cli.command("setup")
@click.option(
    "--yes",
    "-y",
    "non_interactive",
    is_flag=True,
    help="Skip interactive OAuth prompt; start app-server and print manual commands only.",
)
def setup_cmd(non_interactive: bool):
    """Start Codex app-server if needed; optionally run OAuth interactively."""
    print_banner()
    root = _flume_repo_root()
    os.chdir(root)
    click.echo(f"{CYAN}▶ Codex app-server…{NC}")
    _bootstrap_codex_app_server(root, quiet=False)
    interactive = not non_interactive and is_interactive_tty()
    oauth_prompted = False
    if interactive:
        click.echo("")
        oauth_prompted = prompt_oauth_flow(root, _run_codex_oauth_script_with_restart)
    if not oauth_prompted:
        click.echo("")
        click.echo(f"{CYAN}Next — authenticate OpenAI (ChatGPT / Codex subscription):{NC}")
        click.echo(f"  {GREEN}./flume codex-oauth login-browser{NC}   # browser on this machine")
        click.echo(f"  {GREEN}./flume codex-oauth login-paste{NC}      # headless / SSH")
        click.echo(f"  {GREEN}./flume codex-oauth import-codex{NC}     # tokens from official Codex CLI")
        click.echo("")
    click.echo(f"{CYAN}Details:{NC} install/README.md (OpenAI ChatGPT / Codex OAuth)")


@cli.command(
    "codex-oauth",
    context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
    add_help_option=False,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def codex_oauth_cmd(args):
    """Run install/setup/codex_oauth_login.py (login-browser, login-paste, import-codex, …).

    Examples: flume codex-oauth login-browser --help
    """
    root = _flume_repo_root()
    rc = _run_codex_oauth_script(root, tuple(args))
    if rc != 0:
        raise SystemExit(rc)
    if args and args[0] in ("login", "login-browser", "login-paste", "import-codex"):
        _restart_codex_app_server_after_oauth(root)


@cli.group("codex-app-server", invoke_without_command=True)
@click.pass_context
def codex_app_server_grp(ctx: click.Context):
    """Inspect or start the local Codex WebSocket app-server (same as ``start`` when no subcommand)."""
    if ctx.invoked_subcommand is None:
        root = _flume_repo_root()
        os.chdir(root)
        _bootstrap_codex_app_server(root, quiet=False)


@codex_app_server_grp.command("status")
def codex_app_status_cmd():
    root = _flume_repo_root()
    src = root / "src"
    if not src.is_dir():
        click.echo(f"{YELLOW}Missing {src}{NC}", err=True)
        raise SystemExit(1)
    old_path = sys.path[:]
    try:
        sys.path.insert(0, str(src))
        from dashboard.codex_app_server import status as codex_status
    except Exception as exc:
        click.echo(f"{YELLOW}{exc}{NC}", err=True)
        raise SystemExit(1)
    finally:
        sys.path[:] = old_path
    click.echo(json.dumps(codex_status(), indent=2))


@codex_app_server_grp.command("start")
def codex_app_start_cmd():
    root = _flume_repo_root()
    os.chdir(root)
    _bootstrap_codex_app_server(root, quiet=False)


@codex_app_server_grp.command("logs")
@click.option("-n", "lines", default=40, show_default=True, help="Lines to show when not following")
@click.option("-f", "follow", is_flag=True, help="Tail -f the log (Ctrl+C to stop)")
def codex_app_logs_cmd(lines: int, follow: bool):
    root = _flume_repo_root()
    log_path = root / "logs" / "codex-app-server.log"
    if not log_path.is_file():
        click.echo(f"{YELLOW}No log at {log_path}{NC}")
        raise SystemExit(1)
    if follow:
        tail_bin = shutil.which("tail")
        if not tail_bin:
            click.echo(f"{YELLOW}tail(1) not found; showing last {lines} lines.{NC}")
        else:
            raise SystemExit(subprocess.run([tail_bin, "-f", str(log_path)]).returncode)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        click.echo(f"{YELLOW}{exc}{NC}", err=True)
        raise SystemExit(1)
    chunk = text.splitlines()[-lines:]
    click.echo("\n".join(chunk))


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
