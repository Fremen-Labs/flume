"""Nodes API router — Gateway proxy for Node Mesh and Routing Policy.

All writes are forwarded to the Go Gateway, which persists to Elasticsearch
(flume-node-registry). The dashboard never writes node docs directly.

Extracted from server.py as part of the modular router decomposition.
"""
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import httpx
import json

from utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _gateway_base() -> str:
    from config import get_settings
    return get_settings().GATEWAY_URL.rstrip('/')


# ── Node Mesh ──────────────────────────────────────────────────────────────────

@router.get('/api/nodes')
async def api_nodes_list(request: Request):
    """Proxy GET /api/nodes to the Go Gateway and return the node mesh inventory."""
    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/nodes", timeout=5.0)
        logger.info(
            "node_mesh: fetched node list from gateway",
            extra={"component": "node_mesh_api", "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "node_mesh: failed to fetch nodes from gateway",
            extra={"component": "node_mesh_api", "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@router.post('/api/nodes')
async def api_nodes_add(request: Request):
    """Register a new Ollama node in the mesh via the Go Gateway."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        logger.debug("api_nodes_add: invalid JSON body", exc_info=True)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{gw_url}/api/nodes", json=body, timeout=5.0)
        logger.info(
            "node_mesh: registered node via gateway",
            extra={"component": "node_mesh_api", "node_id": body.get("id", "unknown"), "status": resp.status_code}
        )
        # Phase 2.3: Invalidate the worker-manager's node cap cache so the
        # new node starts receiving work immediately instead of after 15s TTL.
        if resp.status_code == 201:
            try:
                from manager import force_refresh_node_caps  # noqa: PLC0415
                force_refresh_node_caps()
            except Exception as _e:
                logger.debug("node_mesh: could not invalidate node cap cache: %s", _e)
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "node_mesh: failed to register node",
            extra={"component": "node_mesh_api", "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@router.delete('/api/nodes/{node_id}')
async def api_nodes_delete(node_id: str, request: Request):
    """Remove an Ollama node from the mesh via the Go Gateway."""
    # Basic validation — mirrors the gateway's isValidNodeID check.
    if not re.fullmatch(r'[a-z0-9\-]{1,64}', node_id):
        logger.warning(
            "node_mesh: rejected delete for invalid node_id",
            extra={"component": "node_mesh_api", "node_id": node_id}
        )
        return JSONResponse(status_code=400, content={"error": "Invalid node ID format"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{gw_url}/api/nodes/{node_id}", timeout=5.0)
        logger.info(
            "node_mesh: deleted node via gateway",
            extra={"component": "node_mesh_api", "node_id": node_id, "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "node_mesh: failed to delete node",
            extra={"component": "node_mesh_api", "node_id": node_id, "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@router.post('/api/nodes/{node_id}/test')
async def api_nodes_test(node_id: str, request: Request):
    """Probe an Ollama node's connectivity and discover available models via the Go Gateway."""
    if not re.fullmatch(r'[a-z0-9\-]{1,64}', node_id):
        logger.warning(
            "node_mesh: rejected test for invalid node_id",
            extra={"component": "node_mesh_api", "node_id": node_id}
        )
        return JSONResponse(status_code=400, content={"error": "Invalid node ID format"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{gw_url}/api/nodes/{node_id}/test", timeout=15.0)
        logger.info(
            "node_mesh: tested node via gateway",
            extra={"component": "node_mesh_api", "node_id": node_id, "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "node_mesh: failed to test node",
            extra={"component": "node_mesh_api", "node_id": node_id, "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


# ── Routing Policy ─────────────────────────────────────────────────────────────

@router.get('/api/routing-policy')
async def api_routing_policy_get(request: Request):
    """Proxy GET /api/routing-policy to the Go Gateway."""
    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/routing-policy", timeout=5.0)
        logger.info(
            "routing_policy: fetched policy from gateway",
            extra={"component": "routing_policy_api", "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "routing_policy: failed to fetch policy",
            extra={"component": "routing_policy_api", "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


@router.put('/api/routing-policy')
async def api_routing_policy_put(request: Request):
    """Proxy PUT /api/routing-policy to the Go Gateway."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        logger.debug("api_routing_policy_put: invalid JSON body", exc_info=True)
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.put(f"{gw_url}/api/routing-policy", json=body, timeout=5.0)
        logger.info(
            "routing_policy: updated policy via gateway",
            extra={"component": "routing_policy_api", "mode": body.get("mode", "unknown"), "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "routing_policy: failed to update policy",
            extra={"component": "routing_policy_api", "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


# ── Frontier Models ────────────────────────────────────────────────────────────

@router.get('/api/frontier-models')
async def api_frontier_models(request: Request):
    """Proxy GET /api/frontier-models to the Go Gateway."""
    try:
        gw_url = _gateway_base()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/frontier-models", timeout=5.0)
        logger.info(
            "routing_policy: fetched frontier catalog from gateway",
            extra={"component": "routing_policy_api", "status": resp.status_code}
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.RequestError as e:
        logger.error(
            "routing_policy: failed to fetch frontier catalog",
            extra={"component": "routing_policy_api", "error": str(e)[:200]}
        )
        return JSONResponse(status_code=503, content={"error": "Gateway unreachable", "detail": str(e)[:200]})


# ── Phase 2.3: Node Utilization Metrics ──────────────────────────────────────

@router.get('/api/nodes/utilization')
async def api_nodes_utilization(request: Request):
    """Aggregate real-time node utilization metrics for the Analytics dashboard.

    Phase 2.3: Queries flume-task-events for per-node task distribution and
    flume-node-registry for capacity data. Returns a unified view of node
    workload, task throughput, and worker pool efficiency.
    """
    from core.elasticsearch import async_es_post

    utilization: dict = {
        'nodes': [],
        'totals': {
            'tasks_completed': 0,
            'tasks_running': 0,
            'total_capacity': 0,
            'worker_pool_size': 0,
            'utilization_pct': 0.0,
        },
    }

    try:
        # 1. Fetch node registry for capacity data
        nodes_resp = await async_es_post(
            'flume-node-registry/_search',
            {'size': 100, 'query': {'match_all': {}}},
        )
        nodes = (nodes_resp or {}).get('hits', {}).get('hits', [])

        # 2. Fetch running task count per node (from worker state)
        workers_resp = await async_es_post(
            'agent-system-workers/_search',
            {'size': 100, 'query': {'match_all': {}}},
        )
        worker_hits = (workers_resp or {}).get('hits', {}).get('hits', [])

        # Build worker utilization map
        node_running_tasks: dict = {}
        total_workers = 0
        for wh in worker_hits:
            ws = wh.get('_source', {})
            workers = ws.get('workers', [])
            total_workers += len(workers)
            for w in workers:
                if w.get('status') == 'claimed':
                    node_id = w.get('node_id') or wh.get('_id', 'unknown')
                    node_running_tasks[node_id] = node_running_tasks.get(node_id, 0) + 1

        # 3. Fetch completed task counts (last 24h)
        completed_resp = await async_es_post(
            'flume-task-events/_search',
            {
                'size': 0,
                'query': {
                    'bool': {
                        'must': [
                            {'term': {'event_type': 'doc_update'}},
                            {'range': {'timestamp': {'gte': 'now-24h'}}},
                        ]
                    }
                },
                'aggs': {
                    'by_status': {
                        'terms': {'field': 'details.status.keyword', 'size': 10}
                    }
                }
            },
        )
        status_buckets = (
            (completed_resp or {})
            .get('aggregations', {})
            .get('by_status', {})
            .get('buckets', [])
        )
        completed_count = sum(
            b.get('doc_count', 0) for b in status_buckets
            if b.get('key') == 'done'
        )
        running_count = sum(
            b.get('doc_count', 0) for b in status_buckets
            if b.get('key') == 'running'
        )

        # 4. Build per-node utilization
        total_cap = 0
        for n in nodes:
            src = n.get('_source', {})
            node_id = src.get('id', n.get('_id', 'unknown'))
            cap = src.get('concurrency_cap', 4)
            total_cap += cap
            health = src.get('health', {})
            running = node_running_tasks.get(node_id, 0)

            utilization['nodes'].append({
                'id': node_id,
                'host': src.get('host', ''),
                'model': src.get('model_tag', ''),
                'capacity': cap,
                'running_tasks': running,
                'utilization_pct': round((running / cap * 100) if cap > 0 else 0, 1),
                'health_status': health.get('status', 'unknown'),
                'latency_ms': health.get('latency_ms', 0),
                'current_load': health.get('current_load', 0.0),
            })

        utilization['totals'] = {
            'tasks_completed_24h': completed_count,
            'tasks_running': running_count,
            'total_capacity': total_cap,
            'worker_pool_size': total_workers,
            'utilization_pct': round(
                (sum(node_running_tasks.values()) / total_cap * 100)
                if total_cap > 0 else 0,
                1,
            ),
        }

    except Exception as e:
        logger.error(
            "node_utilization: failed to aggregate metrics",
            extra={"component": "node_utilization_api", "error": str(e)[:200]}
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to aggregate utilization metrics", "detail": str(e)[:200]}
        )

    return utilization
