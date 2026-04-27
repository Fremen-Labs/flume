from __future__ import annotations

import os
import urllib.error
from utils.exceptions import SAFE_EXCEPTIONS
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from utils.logger import get_logger

logger = get_logger("codex_app_server")

DEFAULT_LISTEN_URL = 'ws://127.0.0.1:4500'
DOCS_URL = 'https://developers.openai.com/codex/app-server'
ENV_LISTEN = 'FLUME_CODEX_APP_SERVER_LISTEN'
ENV_BIN = 'FLUME_CODEX_BIN'


def codex_listen_url() -> str:
    return (os.environ.get(ENV_LISTEN) or DEFAULT_LISTEN_URL).strip() or DEFAULT_LISTEN_URL


def _which(name: str) -> str | None:
    p = shutil.which(name)
    return p if p else None


def _codex_binary() -> str:
    return (os.environ.get(ENV_BIN) or 'codex').strip() or 'codex'


def _parse_ws_host_port(listen_url: str) -> tuple[str, int] | tuple[None, None]:
    try:
        u = urlparse(listen_url)
    except SAFE_EXCEPTIONS:
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
    codex_path = _which(codex_bin)
    npx_path = _which('npx')
    reachable, parse_error = tcp_reachable(listen)
    auth_path = Path.home() / '.codex' / 'auth.json'
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
        'docsUrl': DOCS_URL,
        'envFlumeListen': ENV_LISTEN,
        'envCodexBin': ENV_BIN,
    }


def launch_args(extra_args: list[str] | None = None) -> list[str]:
    listen = codex_listen_url()
    extra = list(extra_args or [])
    codex_path = _which(_codex_binary())
    if codex_path:
        return [codex_path, 'app-server', '--listen', listen, *extra]
    npx_path = _which('npx')
    if npx_path:
        return [npx_path, '--yes', '@openai/codex', 'app-server', '--listen', listen, *extra]
    raise FileNotFoundError('Neither codex nor npx is on PATH')


def run_foreground(extra_args: list[str] | None = None) -> int:
    cmd = launch_args(extra_args)
    logger.info("Launching Codex app-server", extra={"structured_data": {"cmd": " ".join(cmd)}})
    proc = subprocess.run(cmd)
    return int(proc.returncode)


if __name__ == '__main__':
    raise SystemExit(run_foreground(os.sys.argv[1:]))
