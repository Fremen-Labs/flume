"""
WebSocket proxy: browser (dashboard) <-> local Codex app-server.

Relays JSON-RPC text frames transparently to FLUME_CODEX_APP_SERVER_LISTEN
(default ws://127.0.0.1:4500). Requires the optional ``websockets`` package.

Security: anyone who can reach the proxy port can use your Codex session — same
trust boundary as the dashboard HTTP port. Bind to loopback if the host is shared.
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
from utils.exceptions import SAFE_EXCEPTIONS
import threading
import traceback
from typing import Any

from utils.logger import get_logger

logger = get_logger("codex_ws_proxy")

WEBSOCKETS_IMPORT_ERROR: str | None = None
try:
    import websockets  # type: ignore[import-untyped]
except ImportError as e:  # pragma: no cover - optional dependency
    websockets = None  # type: ignore[assignment]
    WEBSOCKETS_IMPORT_ERROR = str(e)

_serve_ready = threading.Event()
_proxy_thread: threading.Thread | None = None
_proxy_listen_error: str | None = None


def _proxy_env_enabled() -> bool:
    v = (os.environ.get("FLUME_CODEX_WS_PROXY") or "1").strip().lower()
    return v not in ("0", "false", "off", "no")


def proxy_bind_host() -> str:
    return (
        os.environ.get("FLUME_CODEX_WS_PROXY_BIND")
        or os.environ.get("FLUME_CODEX_WS_PROXY_HOST")
        or os.environ.get("DASHBOARD_HOST")
        or "0.0.0.0"
    ).strip() or "0.0.0.0"


def proxy_port() -> int:
    return int(os.environ.get("FLUME_CODEX_WS_PROXY_PORT", "8766"))


def _client_hostname_from_host_header(host_header: str | None) -> str:
    if not host_header or not str(host_header).strip():
        return "localhost"
    h = str(host_header).strip()
    if h.startswith("["):
        end = h.find("]")
        if end > 1:
            return h[1:end]
        return h
    return h.split(":")[0] or "localhost"


def get_proxy_config(host_header: str | None = None) -> dict[str, Any]:
    """JSON for GET /api/codex-app-server/proxy-config."""
    import codex_app_server  # type: ignore

    upstream = codex_app_server.codex_listen_url()
    port = proxy_port()
    bind = proxy_bind_host()
    chost = _client_hostname_from_host_header(host_header)
    client_ws_url = f"ws://{chost}:{port}/"

    pip_hint = (
        "pip install 'websockets>=12'  (or: python3 -m pip install --user 'websockets>=12')"
    )

    if WEBSOCKETS_IMPORT_ERROR:
        return {
            "proxyWanted": _proxy_env_enabled(),
            "proxyEnabled": False,
            "proxyRunning": False,
            "proxyPort": port,
            "proxyBind": bind,
            "clientWsUrl": client_ws_url,
            "upstreamListenUrl": upstream,
            "workspaceRoot": __import__("config").get_settings().LOOM_WORKSPACE or "",
            "websocketsInstalled": False,
            "websocketsImportError": WEBSOCKETS_IMPORT_ERROR,
            "installHint": pip_hint,
            "disableReason": "Python package `websockets` is not installed.",
            "serveError": _proxy_listen_error,
        }

    if not _proxy_env_enabled():
        return {
            "proxyWanted": False,
            "proxyEnabled": False,
            "proxyRunning": False,
            "proxyPort": port,
            "proxyBind": bind,
            "clientWsUrl": client_ws_url,
            "upstreamListenUrl": upstream,
            "workspaceRoot": __import__("config").get_settings().LOOM_WORKSPACE or "",
            "websocketsInstalled": True,
            "websocketsImportError": None,
            "installHint": None,
            "disableReason": "Set FLUME_CODEX_WS_PROXY=0 to disable; currently disabled by env.",
            "serveError": _proxy_listen_error,
        }

    running = _serve_ready.is_set() and _proxy_listen_error is None

    return {
        "proxyWanted": True,
        "proxyEnabled": True,
        "proxyRunning": running,
        "proxyPort": port,
        "proxyBind": bind,
        "clientWsUrl": client_ws_url,
        "upstreamListenUrl": upstream,
            "workspaceRoot": __import__("config").get_settings().LOOM_WORKSPACE or "",
        "websocketsInstalled": True,
        "websocketsImportError": None,
        "installHint": None,
        "disableReason": None,
        "serveError": _proxy_listen_error,
    }


async def _relay(browser_ws: Any, upstream_uri: str) -> None:
    assert websockets is not None
    try:
        async with websockets.connect(
            upstream_uri,
            max_size=None,
            ping_interval=20,
            ping_timeout=120,
        ) as upstream:
            async def browser_to_upstream() -> None:
                async for message in browser_ws:
                    await upstream.send(message)

            async def upstream_to_browser() -> None:
                async for message in upstream:
                    await browser_ws.send(message)

            b_task = asyncio.create_task(browser_to_upstream())
            u_task = asyncio.create_task(upstream_to_browser())
            try:
                done, pending = await asyncio.wait(
                    (b_task, u_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(b_task, u_task, return_exceptions=True)
            finally:
                try:
                    await browser_ws.close()
                except SAFE_EXCEPTIONS:
                    pass
    except SAFE_EXCEPTIONS as e:
        try:
            reason = str(e)[:120]
            logger.warning(
                "WebSocket relay error — closing browser connection",
                extra={"structured_data": {"upstream": upstream_uri, "error": str(e)}},
            )
            await browser_ws.close(code=1011, reason=reason)
        except SAFE_EXCEPTIONS:
            pass


async def _one_client(browser_ws: Any) -> None:
    import codex_app_server  # type: ignore

    upstream_uri = codex_app_server.codex_listen_url()
    await _relay(browser_ws, upstream_uri)


async def _async_main() -> None:
    global _proxy_listen_error
    assert websockets is not None

    import codex_app_server  # type: ignore

    host = proxy_bind_host()
    port = proxy_port()
    upstream = codex_app_server.codex_listen_url()

    async def handler(browser_ws: Any) -> None:
        await _one_client(browser_ws)

    try:
        async with websockets.serve(
            handler,
            host,
            port,
            max_size=None,
            ping_interval=20,
            ping_timeout=120,
        ):
            logger.info(
                "Codex WebSocket proxy listening",
                extra={"structured_data": {"bind": f"{host}:{port}", "upstream": upstream}},
            )
            _serve_ready.set()
            _proxy_listen_error = None
            await asyncio.Future()
    except OSError as e:
        _proxy_listen_error = f"{type(e).__name__}: {e}"
        logger.error(
            "Codex WebSocket proxy failed to bind",
            extra={"structured_data": {"bind": f"{host}:{port}", "error": _proxy_listen_error}},
        )
        _serve_ready.set()
    except SAFE_EXCEPTIONS:
        _proxy_listen_error = traceback.format_exc()[-500:]
        logger.error(
            "Codex WebSocket proxy crashed",
            extra={"structured_data": {"error": _proxy_listen_error}},
        )
        _serve_ready.set()


def start_codex_ws_proxy_background() -> None:
    """
    Start the WebSocket proxy in a daemon thread (own asyncio loop).
    No-op if disabled, websockets missing, or already started.
    """
    global _proxy_thread

    if _proxy_thread is not None and _proxy_thread.is_alive():
        return
    if not _proxy_env_enabled():
        return
    if WEBSOCKETS_IMPORT_ERROR:
        return

    def runner() -> None:
        try:
            asyncio.run(_async_main())
        except SAFE_EXCEPTIONS:
            global _proxy_listen_error
            _proxy_listen_error = traceback.format_exc()[-500:]
            logger.error(
                "Codex WebSocket proxy runner crashed",
                extra={"structured_data": {"error": _proxy_listen_error}},
            )
            _serve_ready.set()

    _proxy_thread = threading.Thread(
        target=runner,
        daemon=True,
        name="flume-codex-ws-proxy",
    )
    logger.info(
        "Starting Codex WebSocket proxy thread",
        extra={"structured_data": {"bind": f"{proxy_bind_host()}:{proxy_port()}"}},
    )
    _proxy_thread.start()
    # So startup logs reflect listen failure or success without racing the first HTTP request.
    _serve_ready.wait(timeout=3.0)
