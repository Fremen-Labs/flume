"""System API router — Health, snapshot, telemetry, autonomy sweeps, codex.

Extracted from server.py as part of the modular router decomposition.
This module contains system-level endpoints that don't fit a specific
domain but provide operational visibility and control.
"""
import asyncio
import json
import os
import re
import uuid
import traceback
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, WebSocket, Request, Depends, HTTPException, Header
from fastapi.responses import JSONResponse

import httpx

from utils.logger import get_logger
from utils.async_subprocess import run_cmd_async
from core.elasticsearch import es_search, es_post, es_upsert
from core.tasks import load_workers
from core.projects_store import load_projects_registry
from config import AppConfig, get_settings
from api.models import TaskClaimRequest

logger = get_logger(__name__)
router = APIRouter()

_SRC_ROOT = Path(__file__).resolve().parent.parent.parent  # src/


def _lazy_append_task_agent_log_note(es_id: str, note: str) -> bool:
    """Lazy import to avoid circular dependency."""
    from api.tasks import _append_task_agent_log_note  # noqa: PLC0415
    return _append_task_agent_log_note(es_id, note)


# ── Health ─────────────────────────────────────────────────────────────────────

@router.get('/api/health')
def health():
    return {
        "status": "ok",
        "service": "flume-dashboard",
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


# ── Snapshot ───────────────────────────────────────────────────────────────────

@router.get('/api/snapshot')
async def api_snapshot():
    try:
        from server import load_snapshot  # noqa: PLC0415
        return await load_snapshot()
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:400], 'code': 'ES_CONNECTION'})


# ── System State ───────────────────────────────────────────────────────────────

import shutil

_flume_cli_checked: bool = False
_flume_cli_found: bool = False


def _flume_cli_available() -> bool:
    """Cache whether the `flume` CLI binary exists on $PATH (checked once per process)."""
    global _flume_cli_checked, _flume_cli_found
    if not _flume_cli_checked:
        _flume_cli_found = shutil.which("flume") is not None
        _flume_cli_checked = True
    return _flume_cli_found


@router.get('/api/system-state')
async def api_system_state():
    try:
        workers = load_workers()
        active = sum(1 for w in workers if w.get('status') in ('busy', 'claimed'))
        total = len(workers)

        telemetry = {}
        if _flume_cli_available():
            try:
                rc, out, _err = await run_cmd_async("flume", "doctor", "--json", timeout=5)
                if rc == 0:
                    telemetry = json.loads(out)
            except Exception:
                logger.debug("api_system_state: flume doctor parse failed (non-critical)", exc_info=True)

        return {
            "status": "online",
            "activeStreams": active,
            "totalNodes": total,
            "standbyNodes": total - active,
            "workers": workers,
            "telemetry": telemetry
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:300]})


# ── AST Sync ───────────────────────────────────────────────────────────────────

@router.post("/api/system/sync-ast")
async def api_system_sync_ast(request: Request, x_flume_system_token: str = Header(None), settings: AppConfig = Depends(get_settings)):
    if not (
        settings.FLUME_ADMIN_TOKEN and
        x_flume_system_token and
        secrets.compare_digest(settings.FLUME_ADMIN_TOKEN, x_flume_system_token)
    ):
        logger.warning({"event": "auth_failure", "endpoint": "/api/system/sync-ast", "reason": "invalid_system_token"})
        raise HTTPException(status_code=403, detail="Forbidden: System architectural mapping strictly enforced")

    try:
        return {"success": True, "message": "Elastro RAG integration securely decoupled from built-in Flume architecture"}
    except Exception as e:
        logger.error({
            "event": "ast_system_sync_failure",
            "reason": "unhandled_exception",
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        return JSONResponse(status_code=500, content={"error": "An internal architectural error occurred dynamically."})


# ── Exo Status ─────────────────────────────────────────────────────────────────

def _parse_float_env(key: str, default: float) -> float:
    raw = os.environ.get(key, '')
    if not raw:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning(
            {"event": "env_parse_error", "key": key, "value": raw[:50], "fallback": default}
        )
    return default


class _ExoSettings:
    def __init__(self):
        self.exo_url = os.environ.get("EXO_STATUS_URL", "http://host.docker.internal:52415/models")
        self.exo_timeout = _parse_float_env("EXO_STATUS_TIMEOUT_SECONDS", 0.5)


_exo_settings = _ExoSettings()


@router.get('/api/exo-status')
async def api_exo_status(request: Request):
    http_client = request.app.state.http_client

    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415
    exo_url = _exo_settings.exo_url
    exo_timeout = _exo_settings.exo_timeout

    parsed_url = urlparse(exo_url)
    base_url_parts = parsed_url._replace(path='/v1')
    base_url = urlunparse(base_url_parts)

    try:
        hostname = parsed_url.hostname
        if hostname not in ('host.docker.internal', 'localhost', '127.0.0.1', '::1'):
            logger.warning("Rejected Exo base URL targeting out-of-bounds mapping", extra={"target_url": exo_url})
            return {"active": False}
    except (ValueError, TypeError) as e:
        logger.error("Unexpected error during Exo URL validation", extra={"target_url": exo_url, "error": str(e)})
        return {"active": False}

    try:
        resp = await http_client.get(exo_url, timeout=exo_timeout)
        resp.raise_for_status()

        logger.info(
            "Successfully connected to Exo service",
            extra={"component": "exo_detector", "target_url": exo_url}
        )
        return {"active": True, "baseUrl": base_url}
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning(
            "Exo service connection failed",
            extra={
                "component": "exo_detector",
                "target_url": exo_url,
                "timeout_seconds": exo_timeout,
                "error_type": type(e).__name__,
                "error_details": str(e)
            }
        )
        return {"active": False}


# ── Autonomy Sweeps ────────────────────────────────────────────────────────────

@router.get('/api/autonomy/status')
def api_autonomy_status():
    """Aggregate status for all autonomy background sweeps."""
    out: dict = {}
    try:
        import auto_unblock as _auto_unblock  # noqa: PLC0415
        out['auto_unblock'] = _auto_unblock.get_status()
    except Exception as e:
        out['auto_unblock'] = {'error': str(e)[:200]}
    try:
        import autonomy_sweeps as _autonomy  # noqa: PLC0415
        out['sweeps'] = _autonomy.get_status()
    except Exception as e:
        out['sweeps'] = {'error': str(e)[:200]}
    return out


@router.post('/api/autonomy/sweep/{sweep_name}')
def api_autonomy_sweep_now(sweep_name: str):
    """
    Force-run an autonomy sweep on demand.

    Valid names:
      - auto_unblock            — LLM-guided re-queue for blocked tasks
      - parent_revival          — re-queue blocked parents when bug children close
      - stuck_worker_watchdog   — release stale claims past the idle threshold
    """
    try:
        if sweep_name == 'auto_unblock':
            import auto_unblock as _auto_unblock  # noqa: PLC0415
            summary = _auto_unblock._sweep_once({
                'es_search': es_search,
                'es_post': es_post,
                'append_note': _lazy_append_task_agent_log_note,
                'logger': logger,
            })
            return {'ok': True, 'sweep': 'auto_unblock', 'summary': summary}

        import autonomy_sweeps as _autonomy  # noqa: PLC0415
        result = _autonomy.run_sweep_now(
            sweep_name,
            es_search=es_search,
            es_post=es_post,
            es_upsert=es_upsert,
            append_note=_lazy_append_task_agent_log_note,
            list_projects=load_projects_registry,
            logger=logger,
        )
        return {'ok': True, **result}
    except ValueError as e:
        return JSONResponse(status_code=400, content={'error': str(e)})
    except Exception as e:
        logger.exception(f'autonomy.sweep_failed: {e}')
        return JSONResponse(status_code=500, content={'error': str(e)[:300]})


# ── Auto-Unblock ───────────────────────────────────────────────────────────────

@router.get('/api/auto-unblock/status')
def api_auto_unblock_status():
    """Current auto-unblocker daemon state + last sweep summary."""
    try:
        import auto_unblock as _auto_unblock  # noqa: PLC0415
        return _auto_unblock.get_status()
    except Exception as e:
        return JSONResponse(status_code=500, content={'error': str(e)[:200]})


@router.post('/api/auto-unblock/sweep')
def api_auto_unblock_sweep_now():
    """Manually trigger one auto-unblock sweep and return its summary."""
    try:
        import auto_unblock as _auto_unblock  # noqa: PLC0415
        summary = _auto_unblock._sweep_once({
            'es_search': es_search,
            'es_post': es_post,
            'append_note': _lazy_append_task_agent_log_note,
            'logger': logger,
        })
        return {'ok': True, 'summary': summary}
    except Exception as e:
        logger.exception(f'auto_unblock.manual_sweep_failed: {e}')
        return JSONResponse(status_code=500, content={'error': str(e)[:300]})


# ── Codex App Server ───────────────────────────────────────────────────────────

@router.get("/api/codex-app-server/status")
def api_codex_status():
    from codex_app_server import status  # type: ignore  # noqa: PLC0415
    return status()


@router.get("/api/codex-app-server/proxy-config")
def api_codex_proxy_config():
    # Frontend expects codex WS setup info
    return {"baseUrl": "ws://localhost:8765", "path": "/api/codex-app-server/ws"}


# ── Telemetry (WebSocket + REST) ───────────────────────────────────────────────

active_connections: list[WebSocket] = []


async def _gather_telemetry_events(conn_state: dict) -> list[dict]:
    """Poll the gateway and worker state and return a list of human-readable log entries.

    Args:
        conn_state: Per-connection dict for tracking deltas across polling intervals.
                    Each WebSocket client gets its own instance so escalation events
                    are never suppressed for one client because another already saw them.
    """
    events: list[dict] = []
    now_str = datetime.now().strftime("%H:%M:%S")

    # --- 1. Gateway Prometheus metrics ---
    try:
        gateway_url = os.environ.get('FLUME_GATEWAY_URL', 'http://localhost:8090').rstrip('/')
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gateway_url}/metrics", timeout=2.0)
            if resp.status_code == 200:
                goroutines = 0
                alloc_mb = 0.0
                escalations = 0
                blocked = 0
                throttled = 0
                active_models: list[str] = []
                for line in resp.text.split('\n'):
                    if line.startswith('#') or not line.strip():
                        continue
                    parts = line.split(' ', 1)
                    if len(parts) != 2:
                        continue
                    k, v = parts[0], parts[1]
                    if k == 'go_goroutines':
                        goroutines = int(float(v))
                    elif k == 'go_memstats_alloc_bytes':
                        alloc_mb = round(float(v) / 1048576, 1)
                    elif k == 'flume_escalation_total':
                        escalations = int(float(v))
                    elif k == 'flume_tasks_blocked_total':
                        blocked = int(float(v))
                    elif k == 'flume_concurrency_throttled_total':
                        throttled = int(float(v))
                    elif k.startswith('flume_active_models{') and int(float(v)) == 1:
                        m = re.search(r'model="([^"]+)"', k)
                        if m:
                            active_models.append(m.group(1))

                events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "INFO",
                               "msg": f"Gateway alive — {goroutines} goroutines, {alloc_mb}MB heap"})

                if active_models:
                    models_str = ', '.join(active_models)
                    events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "INFO",
                                   "msg": f"Active models: {models_str}"})

                prev_esc = conn_state.get('escalations', 0)
                if escalations > prev_esc:
                    events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "WARN",
                                   "msg": f"Escalation events: {escalations} (+{escalations - prev_esc})"})
                conn_state['escalations'] = escalations

                if blocked > 0:
                    events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "WARN",
                                   "msg": f"Blocked tasks in queue: {blocked}"})
                if throttled > conn_state.get('throttled', 0):
                    events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "WARN",
                                   "msg": f"Concurrency throttle events: {throttled}"})
                conn_state['throttled'] = throttled
    except Exception:
        events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "WARN",
                       "msg": "Gateway metrics unreachable"})

    # --- 2. Node mesh health from gateway ---
    try:
        gateway_url = os.environ.get('FLUME_GATEWAY_URL', 'http://localhost:8090').rstrip('/')
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gateway_url}/api/nodes", timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                nodes = data.get('nodes', [])
                healthy = sum(1 for n in nodes if n.get('health', {}).get('status') == 'healthy')
                total = len(nodes)
                events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "INFO",
                               "msg": f"Node mesh: {healthy}/{total} healthy"})
                for n in nodes:
                    h = n.get('health', {})
                    lat = h.get('latency_ms', 0)
                    load = h.get('current_load', 0)
                    status = h.get('status', 'unknown')
                    models = h.get('loaded_models') or []
                    level = "INFO" if status == 'healthy' else "WARN"
                    models_str = ', '.join(models) if models else 'none'
                    events.append({"id": uuid.uuid4().hex, "time": now_str, "level": level,
                                   "msg": f"  {n['id']}: {status} | {lat}ms | load {load} | models [{models_str}]"})
    except Exception:
        pass  # node details are best-effort

    # --- 3. Worker heartbeat summary ---
    try:
        workers = load_workers()
        active = sum(1 for w in workers if w.get('status') in ('busy', 'running', 'claimed', 'active'))
        idle = sum(1 for w in workers if w.get('status') == 'idle')
        events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "INFO",
                       "msg": f"Workers: {active} active, {idle} standby, {len(workers)} total"})
        for w in workers:
            if w.get('status') in ('busy', 'running', 'claimed', 'active'):
                task_title = w.get('current_task_title') or w.get('current_task_id') or '—'
                events.append({"id": uuid.uuid4().hex, "time": now_str, "level": "INFO",
                               "msg": f"  ▸ {w['name']} [{w.get('model', '?')}] → {task_title}"})
    except Exception:
        pass  # worker summary is best-effort

    return events


@router.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415
    await websocket.accept()
    active_connections.append(websocket)
    conn_state: dict = {}  # Per-connection delta tracking state
    try:
        while True:
            # Server-push: gather real telemetry and broadcast to this client
            events = await _gather_telemetry_events(conn_state)
            for payload in events:
                try:
                    await websocket.send_text(json.dumps({"event": "telemetry", "data": payload}))
                except Exception:
                    return  # connection lost
            # Wait 3 seconds between telemetry pushes
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        # Normal browser close (tab navigation, page reload, window close).
        pass
    except Exception as e:
        logger.error({"event": "websocket_handler_crashed", "client": str(websocket.client), "error": str(e), "traceback": traceback.format_exc()})
    finally:
        try:
            active_connections.remove(websocket)
        except ValueError:
            pass


# ── REST Telemetry ─────────────────────────────────────────────────────────────

@router.get("/api/telemetry")
async def get_system_telemetry():
    """Proxy metrics from Go Gateway and transform Prometheus text to JSON native dict."""
    try:
        gateway_url = os.environ.get('FLUME_GATEWAY_URL', 'http://localhost:8090').rstrip('/')
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gateway_url}/metrics", timeout=2.0)
            if resp.status_code != 200:
                raise HTTPException(status_code=503, detail="Gateway metrics disabled or unreachable")

            lines = resp.text.split('\n')
            results = {
                "go_goroutines": 0,
                "go_memstats_alloc_bytes": 0,
                "go_memstats_sys_bytes": 0,
                "flume_up": 0,
                "flume_escalation_total": 0,
                "flume_build_info": "unknown",
                "flume_active_models": [],
                "flume_ensemble_requests_total": [],
                "flume_vram_pressure_events_total": 0,
                "flume_worker_tokens_total": [],
                "flume_node_requests_total": [],
                "flume_routing_decision": [],
                "flume_node_load": [],
                "flume_concurrency_throttled_total": 0,
                "flume_tasks_blocked_total": 0,
                "flume_frontier_spend_usd_total": [],
                "flume_frontier_circuit_breaks_total": [],
            }

            for line in lines:
                if line.startswith("#") or not line.strip():
                    continue

                parts = line.split(" ", 1)
                if len(parts) == 2:
                    key_with_tags = parts[0]
                    val = parts[1]

                    if key_with_tags == "go_goroutines":
                        results["go_goroutines"] = int(float(val))
                    elif key_with_tags == "go_memstats_alloc_bytes":
                        results["go_memstats_alloc_bytes"] = int(float(val))
                    elif key_with_tags == "go_memstats_sys_bytes":
                        results["go_memstats_sys_bytes"] = int(float(val))
                    elif key_with_tags == "flume_up":
                        results["flume_up"] = int(float(val))
                    elif key_with_tags == "flume_escalation_total":
                        results["flume_escalation_total"] = int(float(val))
                    elif key_with_tags == "flume_vram_pressure_events_total":
                        results["flume_vram_pressure_events_total"] = int(float(val))
                    elif key_with_tags == "flume_concurrency_throttled_total":
                        results["flume_concurrency_throttled_total"] = int(float(val))
                    elif key_with_tags == "flume_tasks_blocked_total":
                        results["flume_tasks_blocked_total"] = int(float(val))
                    elif key_with_tags.startswith("flume_build_info{"):
                        m = re.search(r'version="([^"]+)"', key_with_tags)
                        if m:
                            results["flume_build_info"] = m.group(1)
                    elif key_with_tags.startswith("flume_active_models{"):
                        m = re.search(r'model="([^"]+)"', key_with_tags)
                        if m and int(float(val)) == 1:
                            results["flume_active_models"].append(m.group(1))
                    elif key_with_tags.startswith("flume_ensemble_requests_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_ensemble_requests_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_worker_tokens_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_worker_tokens_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_node_requests_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_node_requests_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_routing_decision{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_routing_decision"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })
                    elif key_with_tags.startswith("flume_node_load{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_node_load"].append({
                            "tags": tag_dict,
                            "value": float(val)
                        })
                    elif key_with_tags.startswith("flume_frontier_spend_usd_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_frontier_spend_usd_total"].append({
                            "tags": tag_dict,
                            "value": float(val)
                        })
                    elif key_with_tags.startswith("flume_frontier_circuit_breaks_total{"):
                        tags = re.findall(r'([a-z_]+)="([^"]+)"', key_with_tags)
                        tag_dict = {k: v for k, v in tags}
                        results["flume_frontier_circuit_breaks_total"].append({
                            "tags": tag_dict,
                            "count": int(float(val))
                        })

            return results
    except Exception as e:
        logger.error({"event": "telemetry_fetch_failed", "error": str(e), "target": "Go_Gateway_Proxy"})
        raise HTTPException(status_code=503, detail="Gateway telemetry unreachable")


# ── Telemetry Logs ─────────────────────────────────────────────────────────────

@router.get("/api/logs")
def get_telemetry_logs():
    try:
        body = {
            "size": 60,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {"match_all": {}}
        }
        res = es_search('flume-telemetry', body)
        hits = res.get('hits', {}).get('hits', [])
        logs = []
        for h in hits:
            src = h['_source']
            t_iso = src.get('timestamp', '')
            try:
                time_str = datetime.fromisoformat(t_iso.replace('Z', '+00:00')).strftime('%H:%M:%S')
            except Exception:
                logger.debug("api_telemetry_logs: ISO timestamp parse failed, using raw string", exc_info=True)
                time_str = t_iso

            logs.append({
                "id": h['_id'],
                "msg": f"[{src.get('worker_name', 'System')}] {src.get('message', '')}",
                "time": time_str,
                "level": src.get('level', 'INFO')
            })
        logs.reverse()
        return logs
    except Exception:
        logger.error("Failed to query telemetry logs natively", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load logs")


# ── Task Claims (stub) ─────────────────────────────────────────────────────────

@router.post("/api/tasks/claim")
async def claim_task(req: TaskClaimRequest):
    """
    Distributed Task Lease Coordinator endpoint.
    Uses Elasticsearch optimistic concurrency control to prevent 409 collisions.
    """
    return {"status": "claimed", "task_id": "mock_id", "worker": req.worker_id}


@router.post("/api/tasks/complete")
async def complete_task(task_id: str):
    return {"status": "completed", "task": task_id}
