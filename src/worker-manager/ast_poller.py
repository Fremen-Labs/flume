import time
import os
import subprocess
import logging
import json
from datetime import datetime, timezone
from opensearchpy import OpenSearch

def json_log(level: str, msg: str, **kwargs):
    doc = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "message": str(msg),
        "service": "ast-poller",
        "pid": os.getpid()
    }
    if kwargs:
        doc.update(kwargs)
    print(json.dumps(doc), flush=True)

def init_es_client() -> OpenSearch:
    target = os.environ.get('ES_URL', 'http://localhost:9200').rstrip('/')
    api_key = os.environ.get('ES_API_KEY', '')
    verify_certs = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
    ca_certs = os.environ.get('ES_CA_CERTS', '').strip()
    
    headers = {}
    if api_key: headers['Authorization'] = f'ApiKey {api_key}'
    
    kwargs = {
        "hosts": [target],
        "headers": headers,
        "verify_certs": verify_certs,
        "ssl_show_warn": False
    }
    if verify_certs and ca_certs:
        kwargs["ca_certs"] = ca_certs

    return OpenSearch(**kwargs)

def poll_and_sync():
    """
    Deterministically polls the agent-task-records index looking for tasks 
    that are 'completed' but have not yet been 'ast_synced'.
    """
    json_log("INFO", "Initializing Deterministic AST Poller Daemon...")
    
    while True:
        try:
            client = init_es_client()
            # Query ES for completed tasks without ast_synced
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"status.keyword": "completed"}}
                        ],
                        "must_not": [
                            {"term": {"ast_synced": True}}
                        ]
                    }
                },
                "size": 10
            }
            
            res = client.search(index="agent-task-records", body=query)
            hits = res.get('hits', {}).get('hits', [])
            
            for doc in hits:
                task_id = doc['_id']
                source = doc['_source']
                repo_path = source.get('repo_path')
                
                if not repo_path:
                    client.update(index="agent-task-records", id=task_id, body={"doc": {"ast_synced": True}})
                    continue
                    
                json_log("INFO", f"Found newly completed task. Initiating native AST batch index.", task_id=task_id, repo=repo_path)
                try:
                    subprocess.run(["elastro", "rag", "update", repo_path], check=True, capture_output=True, timeout=120)
                    json_log("INFO", "Flawlessly updated AST boundary natively.", repo=repo_path)
                except subprocess.CalledProcessError as e:
                    json_log("ERROR", "subprocess trace failed.", repo=repo_path, stdout=e.stdout.decode('utf-8', errors='replace'), stderr=e.stderr.decode('utf-8', errors='replace'))
                except Exception as e:
                    json_log("ERROR", f"elastro rag update failed on repo.", repo=repo_path, error=str(e))
                    
                client.update(index="agent-task-records", id=task_id, body={"doc": {"ast_synced": True}})
                
        except Exception as e:
            json_log("ERROR", "Elasticsearch trace failure.", error=str(e))
            
        time.sleep(15)

def start_poller_thread():
    import threading
    t = threading.Thread(target=poll_and_sync, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    poll_and_sync()
