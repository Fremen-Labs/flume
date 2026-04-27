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
