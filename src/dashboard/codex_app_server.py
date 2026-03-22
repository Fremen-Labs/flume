from __future__ import annotations

import os
import sys
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_LISTEN_URL = 'ws://127.0.0.1:4500'
DOCS_URL = 'https://developers.openai.com/codex/app-server'
ENV_LISTEN = 'FLUME_CODEX_APP_SERVER_LISTEN'
ENV_BIN = 'FLUME_CODEX_BIN'

# Dashboard/workers often run with a stripped PATH (nohup/systemd); Codex CLI and Node may live here.
_PATH_PREFIX_DIRS = (
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    str(Path.home() / ".local" / "bin"),
)


def codex_listen_url() -> str:
    return (os.environ.get(ENV_LISTEN) or DEFAULT_LISTEN_URL).strip() or DEFAULT_LISTEN_URL


def _find_executable(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    for d in _PATH_PREFIX_DIRS:
        cand = Path(d) / name
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:
            continue
    return None


def _codex_child_env() -> dict[str, str]:
    e = os.environ.copy()
    prefix = ":".join(_PATH_PREFIX_DIRS)
    e["PATH"] = f"{prefix}:{e.get('PATH', '')}"
    return e


def _codex_binary() -> str:
    return (os.environ.get(ENV_BIN) or 'codex').strip() or 'codex'


def _parse_ws_host_port(listen_url: str) -> tuple[str, int] | tuple[None, None]:
    try:
        u = urlparse(listen_url)
    except Exception:
        return None, None
    if u.scheme != 'ws' or not u.hostname or not u.port:
        return None, None
    return u.hostname, u.port


def tcp_reachable(listen_url: str) -> tuple[bool | None, str | None]:
    host, port = _parse_ws_host_port(listen_url)
    if not host or not port:
        return None, f'Invalid listen URL: {listen_url!r} (expected ws://host:port)'
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True, None
    except OSError:
        return False, None


def status() -> dict[str, Any]:
    listen = codex_listen_url()
    codex_bin = _codex_binary()
    codex_path = _find_executable(codex_bin)
    npx_path = _find_executable("npx")
    reachable, parse_error = tcp_reachable(listen)
    auth_path = Path.home() / '.codex' / 'auth.json'
    oauth_state_env = (os.environ.get('OPENAI_OAUTH_STATE_FILE') or '').strip()
    oauth_state_path = Path(oauth_state_env).expanduser() if oauth_state_env else (Path.cwd() / '.openai-oauth.json')
    oauth_state_present = oauth_state_path.is_file()
    return {
        'listenUrl': listen,
        'defaultListenUrl': DEFAULT_LISTEN_URL,
        'codexBinary': codex_bin,
        'codexResolvedPath': codex_path,
        'codexOnPath': bool(codex_path),
        'npxOnPath': bool(npx_path),
        'npxResolvedPath': npx_path,
        'flumeWillUseNpxFallback': (not codex_path) and bool(npx_path),
        'tcpReachable': reachable,
        'parseError': parse_error,
        'codexAuthFilePresent': auth_path.is_file(),
        'codexAuthFilePath': str(auth_path),
        'flumeOAuthStateFileConfigured': bool(oauth_state_env),
        'flumeOAuthStateFilePath': str(oauth_state_path),
        'flumeOAuthStateFilePresent': oauth_state_present,
        'flumeOAuthConfigured': oauth_state_present,
        'docsUrl': DOCS_URL,
        'envFlumeListen': ENV_LISTEN,
        'envCodexBin': ENV_BIN,
    }
def launch_args(extra_args: list[str] | None = None) -> list[str]:
    listen = codex_listen_url()
    extra = list(extra_args or [])
    codex_path = _find_executable(_codex_binary())
    if codex_path:
        return [codex_path, 'app-server', '--listen', listen, *extra]
    npx_path = _find_executable("npx")
    if npx_path:
        return [npx_path, '--yes', '@openai/codex', 'app-server', '--listen', listen, *extra]
    raise FileNotFoundError('Neither codex nor npx is on PATH')


def run_foreground(extra_args: list[str] | None = None) -> int:
    cmd = launch_args(extra_args)
    print('Running:', ' '.join(cmd), flush=True)
    proc = subprocess.run(cmd)
    return int(proc.returncode)


def start_background_if_needed(workspace_root: Path | None = None) -> dict[str, Any]:
    """
    Start Codex app-server detached if nothing is listening on codex_listen_url().
    Logs to <workspace>/logs/codex-app-server.log; PID in codex-app-server.pid.
    """
    wr = (workspace_root or Path.cwd()).resolve()
    listen = codex_listen_url()
    reachable, parse_error = tcp_reachable(listen)
    if reachable is True:
        return {'started': False, 'reason': 'already_running', 'listenUrl': listen}
    if parse_error:
        return {'started': False, 'reason': 'invalid_listen', 'error': parse_error, 'listenUrl': listen}
    try:
        cmd = launch_args()
    except FileNotFoundError as e:
        return {'started': False, 'reason': 'no_codex_npx', 'error': str(e), 'listenUrl': listen}
    logs_dir = wr / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / 'codex-app-server.log'
    pid_path = logs_dir / 'codex-app-server.pid'
    try:
        with open(log_path, 'ab', buffering=0) as log_f:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=str(wr),
                env=_codex_child_env(),
                start_new_session=True,
            )
        pid_path.write_text(str(proc.pid), encoding='utf-8')
    except OSError as e:
        return {'started': False, 'reason': 'spawn_failed', 'error': str(e), 'listenUrl': listen}
    return {
        'started': True,
        'pid': proc.pid,
        'listenUrl': listen,
        'logPath': str(log_path),
        'pidPath': str(pid_path),
    }


if __name__ == '__main__':
    raise SystemExit(run_foreground(sys.argv[1:]))
