"""
Codex app-server integration helpers.

The official Codex CLI exposes `codex app-server` (JSON-RPC over stdio or WebSocket).
Flume uses the same ChatGPT/Codex OAuth session as the CLI (~/.codex/auth.json or
Flume's codex-oauth import) when you run app-server on the Flume host.

See: https://developers.openai.com/codex/app-server
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_LISTEN = "ws://127.0.0.1:4500"
DOCS_URL = "https://developers.openai.com/codex/app-server"


def codex_listen_url() -> str:
    return (os.environ.get("FLUME_CODEX_APP_SERVER_LISTEN") or DEFAULT_LISTEN).strip()


def codex_binary_name() -> str:
    return (os.environ.get("CODEX_BIN") or "codex").strip() or "codex"


def _parse_ws_target(url: str) -> tuple[str, int] | None:
    u = urlparse(url)
    if u.scheme not in ("ws", "wss"):
        return None
    host = u.hostname
    if not host:
        return None
    if u.port is not None:
        return host, u.port
    return host, 443 if u.scheme == "wss" else 80


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _codex_auth_present() -> bool:
    home = Path.home()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        p = Path(codex_home) / "auth.json"
        return p.is_file()
    return (home / ".codex" / "auth.json").is_file()


def get_codex_app_server_status() -> dict[str, Any]:
    """
    Return configuration and a lightweight TCP probe for the configured WebSocket listen URL.

    Note: TCP reachability does not guarantee JSON-RPC health; it only detects a listening port.
    """
    listen_url = codex_listen_url()
    target = _parse_ws_target(listen_url)
    parse_error: str | None = None
    tcp_reachable: bool | None = None
    if target is None:
        parse_error = (
            f"FLUME_CODEX_APP_SERVER_LISTEN must be a ws:// or wss:// URL (got {listen_url!r})."
        )
    else:
        tcp_reachable = _tcp_reachable(target[0], target[1])

    bin_name = codex_binary_name()
    if os.path.isabs(bin_name) or "/" in bin_name:
        p = Path(bin_name)
        which = str(p) if p.is_file() and os.access(p, os.X_OK) else None
    else:
        which = shutil.which(bin_name)

    npx_path = shutil.which("npx")
    # ./flume codex-app-server falls back to npx when CODEX_BIN is unset and codex is missing.
    explicit_codex_bin = bool((os.environ.get("CODEX_BIN") or "").strip())
    npx_fallback_ok = bool(npx_path) and not explicit_codex_bin and not which

    return {
        "listenUrl": listen_url,
        "defaultListenUrl": DEFAULT_LISTEN,
        "codexBinary": bin_name,
        "codexResolvedPath": which,
        "codexOnPath": bool(which),
        "npxOnPath": bool(npx_path),
        "npxResolvedPath": npx_path,
        "flumeWillUseNpxFallback": npx_fallback_ok,
        "tcpReachable": tcp_reachable,
        "parseError": parse_error,
        "codexAuthFilePresent": _codex_auth_present(),
        "docsUrl": DOCS_URL,
        "envFlumeListen": "FLUME_CODEX_APP_SERVER_LISTEN",
        "envCodexBin": "CODEX_BIN",
    }
