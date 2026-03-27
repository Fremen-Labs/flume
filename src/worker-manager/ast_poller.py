import time
import os
import subprocess
import logging
from opensearchpy import OpenSearch

logger = logging.getLogger("AstPoller")

def init_es_client() -> OpenSearch:
    target = os.environ.get('ES_URL', 'http://localhost:9200')
    api_key = os.environ.get('ES_API_KEY', '')
    
    headers = {}
    if api_key: headers['Authorization'] = f'ApiKey {api_key}'
    
    return OpenSearch(
        hosts=[target],
        headers=headers,
        verify_certs=False,
        ssl_show_warn=False
    )

def poll_and_sync():
    """
    Deterministically polls the agent-task-records index looking for tasks 
    that are 'completed' but have not yet been 'ast_synced'.
    """
    logger.info("Initializing Deterministic AST Poller Daemon...")
    
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
                    
                logger.info(f"[AST Poller] Found newly completed task {task_id}. Initiating native AST batch index on {repo_path}...")
                try:
                    subprocess.run(["elastro", "rag", "update", repo_path], check=True, capture_output=True, timeout=120)
                    logger.info(f"[AST Poller] Flawlessly updated AST boundary for {repo_path} natively.")
                except subprocess.CalledProcessError as e:
                    logger.error(f"[AST Poller] subprocess trace failed. stdout: {e.stdout} stderr: {e.stderr}")
                except Exception as e:
                    logger.error(f"[AST Poller] elastro rag update failed on {repo_path}: {e}")
                    
                client.update(index="agent-task-records", id=task_id, body={"doc": {"ast_synced": True}})
                
        except Exception as e:
            logger.error(f"[AST Poller] Elasticsearch trace failure: {e}")
            
        time.sleep(15)

def start_poller_thread():
    import threading
    t = threading.Thread(target=poll_and_sync, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    poll_and_sync()
