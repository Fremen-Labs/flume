import time
import os
import subprocess
import json
from datetime import datetime, timezone
from pathlib import Path
from opensearchpy import OpenSearch
from utils.workspace import resolve_safe_workspace

import sys
from utils.logger import get_logger

logger = get_logger("ast-poller")

def json_log(level: str, msg: str, **kwargs):
    extra = {'structured_data': kwargs} if kwargs else {}
    if level.upper() == "INFO":
        logger.info(msg, extra=extra)
    elif level.upper() == "ERROR":
        logger.error(msg, extra=extra)
    else:
        logger.warning(msg, extra=extra)

def init_es_client() -> OpenSearch:
    target = os.environ.get('ES_URL', 'http://localhost:9200').rstrip('/')
    api_key = os.environ.get('ES_API_KEY', '')
    verify_certs = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
    ca_certs = os.environ.get('ES_CA_CERTS', '').strip()
    
    headers = {}
    if api_key:
        headers['Authorization'] = f'ApiKey {api_key}'
    
    kwargs = {
        "hosts": [target],
        "headers": headers,
        "verify_certs": verify_certs,
        "ssl_show_warn": False
    }
    if verify_certs and ca_certs:
        kwargs["ca_certs"] = ca_certs

    return OpenSearch(**kwargs)

def process_batch(client: OpenSearch, hits: list, timeout_sec: int):
    for doc in hits:
        task_id = doc['_id']
        source = doc['_source']
        repo_path = source.get('repo_path')
        attempts = source.get('ast_sync_attempts', 0)
        
        if not repo_path:
            client.update(index="agent-task-records", id=task_id, body={"doc": {"ast_synced": True, "ast_sync_status": "ignored"}})
            continue
            
        update_body = {"doc": {"ast_sync_attempts": attempts + 1}}
        
        try:
            safe_path = Path(repo_path).resolve()
            ws = resolve_safe_workspace()
            if not safe_path.is_relative_to(ws):
                json_log("ERROR", "Path Injection Trap activated: Repo path outside bounds.", task_id=task_id, raw_path=repo_path)
                update_body["doc"]["ast_synced"] = True
                update_body["doc"]["ast_sync_status"] = "failed_terminal"
                update_body["doc"]["ast_sync_error"] = "path_injection_locked"
                client.update(index="agent-task-records", id=task_id, body=update_body)
                continue
        except Exception as e:
            json_log("ERROR", "Malformed Path logic exception", task_id=task_id, raw_path=repo_path, error_type=type(e).__name__, error_message=str(e))
            update_body["doc"]["ast_synced"] = True
            update_body["doc"]["ast_sync_status"] = "failed_terminal"
            update_body["doc"]["ast_sync_error"] = "malformed_path"
            client.update(index="agent-task-records", id=task_id, body=update_body)
            continue
            
        json_log("INFO", "Initiating native AST batch index.", task_id=task_id, repo=str(safe_path), attempt=attempts+1)
        try:
            subprocess.run(["elastro", "rag", "update", str(safe_path)], check=True, capture_output=True, timeout=timeout_sec)
            json_log("INFO", "Flawlessly updated AST boundary natively.", repo=str(safe_path))
            update_body["doc"]["ast_synced"] = True
            update_body["doc"]["ast_sync_status"] = "success"
        except subprocess.CalledProcessError as e:
            json_log("ERROR", "elastro subprocess trace failed", repo=str(safe_path), stdout=e.stdout.decode('utf-8', errors='replace'), stderr=e.stderr.decode('utf-8', errors='replace'))
            if attempts >= 2:
                update_body["doc"]["ast_synced"] = True
                update_body["doc"]["ast_sync_status"] = "failed_terminal"
            else:
                update_body["doc"]["ast_sync_status"] = "failed"
            update_body["doc"]["ast_sync_error"] = "subprocess_exception"
        except Exception as e:
            json_log("ERROR", "elastro rag update unhandled exception", repo=str(safe_path), error_type=type(e).__name__, error_message=str(e))
            if attempts >= 2:
                update_body["doc"]["ast_synced"] = True
                update_body["doc"]["ast_sync_status"] = "failed_terminal"
            else:
                update_body["doc"]["ast_sync_status"] = "failed"
            update_body["doc"]["ast_sync_error"] = str(e)
            
        client.update(index="agent-task-records", id=task_id, body=update_body)


def poll_and_sync():
    json_log("INFO", "Initializing Deterministic AST Poller Daemon...")
    client = init_es_client()
    
    POLL_INTERVAL_SECONDS = int(os.environ.get('AST_POLLER_INTERVAL_SECONDS', '15'))
    BATCH_SIZE = int(os.environ.get('AST_POLLER_BATCH_SIZE', '10'))
    SUBPROCESS_TIMEOUT_SECONDS = int(os.environ.get('AST_POLLER_SUBPROCESS_TIMEOUT', '120'))
    
    while True:
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"status": "completed"}}
                        ],
                        "must_not": [
                            {"term": {"ast_synced": True}},
                            {"term": {"ast_sync_status": "failed_terminal"}}
                        ]
                    }
                },
                "size": BATCH_SIZE
            }
            
            res = client.search(index="agent-task-records", body=query)
            hits = res.get('hits', {}).get('hits', [])
            
            if hits:
                process_batch(client, hits, SUBPROCESS_TIMEOUT_SECONDS)
                
        except Exception as e:
            json_log("ERROR", "AST poller main loop exception", error_type=type(e).__name__, error_message=str(e))
            
        time.sleep(POLL_INTERVAL_SECONDS)

def start_poller_thread():
    import threading
    t = threading.Thread(target=poll_and_sync, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    poll_and_sync()
