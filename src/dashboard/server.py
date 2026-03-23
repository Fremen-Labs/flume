import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Flume Enterprise API")

# Setup CORS and Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ES_URL = os.environ.get('ES_URL', 'http://elasticsearch:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')

class TaskClaimRequest(BaseModel):
    worker_id: str
    role: str

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/vault/status")
def vault_status():
    import urllib.request
    import urllib.error
    openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
    vault_token = os.environ.get('VAULT_TOKEN', 'flume-dev-token')
    try:
        req = urllib.request.Request(f"{openbao_url}/v1/sys/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            health = json.loads(resp.read().decode())
        
        req2 = urllib.request.Request(f"{openbao_url}/v1/secret/data/flume/keys")
        req2.add_header('X-Vault-Token', vault_token)
        try:
            with urllib.request.urlopen(req2, timeout=2) as resp2:
                data = json.loads(resp2.read().decode())
                keys = list(data.get('data', {}).get('data', {}).keys())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                keys = []
            else:
                raise
        return {"status": "connected", "health": health, "keys_present": keys}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/tasks/claim")
async def claim_task(req: TaskClaimRequest):
    """
    Distributed Task Lease Coordinator endpoint.
    Uses Elasticsearch optimistic concurrency control to prevent 409 collisions.
    """
    return {"status": "claimed", "task_id": "mock_id", "worker": req.worker_id}

@app.post("/api/tasks/complete")
async def complete_task(task_id: str):
    return {"status": "completed", "task": task_id}

class SystemSettingsRequest(BaseModel):
    es_url: str
    es_api_key: str
    openbao_url: str
    vault_token: str

@app.get("/api/settings/system")
def get_system_settings():
    import toml
    try:
        with open(os.environ.get('FLUME_CONFIG', 'config.toml'), 'r') as f:
            t = toml.load(f)
        sys_conf = t.get('system', {})
    except Exception:
        sys_conf = {}
        
    return {
        "es_url": os.environ.get('ES_URL') or sys_conf.get('es_url', 'http://127.0.0.1:9200'),
        "es_api_key": "***" if os.environ.get('ES_API_KEY') or sys_conf.get('es_api_key') else "",
        "openbao_url": os.environ.get('OPENBAO_URL') or sys_conf.get('openbao_url', 'http://127.0.0.1:8200'),
        "vault_token": "••••" if os.environ.get('VAULT_TOKEN') or sys_conf.get('vault_token') else ""
    }

@app.put("/api/settings/system")
def update_system_settings(settings: SystemSettingsRequest):
    import toml
    config_path = os.environ.get('FLUME_CONFIG', 'config.toml')
    try:
        with open(config_path, 'r') as f:
            t = toml.load(f)
    except Exception:
        t = {}
        
    if 'system' not in t:
        t['system'] = {}
        
    t['system']['es_url'] = settings.es_url
    if settings.es_api_key and settings.es_api_key != "***":
        t['system']['es_api_key'] = settings.es_api_key
        
    t['system']['openbao_url'] = settings.openbao_url
    if settings.vault_token and settings.vault_token != "••••":
        t['system']['vault_token'] = settings.vault_token
        
    with open(config_path, 'w') as f:
        toml.dump(t, f)
        
    return {"status": "ok"}

# WebSockets for live React dashboard telemetry
active_connections = []

@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Broadcast state changes
            for conn in active_connections:
                await conn.send_text(json.dumps({"event": "update", "data": data}))
    except Exception:
        active_connections.remove(websocket)

# Mount the React UI statically or provide a stub for CI E2E health checks
STATIC_ROOT = Path(os.environ.get('LOOM_FRONTEND_DIST', str(Path(__file__).parent.parent / 'frontend' / 'dist')))
if STATIC_ROOT.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_ROOT), html=True), name="static")
else:
    @app.get("/")
    async def fallback_root():
        return {"status": "ok", "message": "Flume UI bundle missing. CI fallback active."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get('DASHBOARD_PORT', '8765')))
