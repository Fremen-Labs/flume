import urllib.request, json
ES_URL = "http://127.0.0.1:9200"
headers = {"Content-Type": "application/json"}
req = urllib.request.Request(f"{ES_URL}/agent-task-records/_search?size=100", headers=headers, method="GET")
with urllib.request.urlopen(req) as res:
    hits = json.loads(res.read().decode())['hits']['hits']

for h in hits:
    if h['_source'].get('queue_state') == 'active':
        doc_id = h['_id']
        url = f"{ES_URL}/agent-task-records/_update/{doc_id}"
        body = json.dumps({"doc": {"queue_state": "queued", "active_worker": None, "status": "ready"}}).encode()
        try:
            r = urllib.request.Request(url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(r)
            print(f"Unlocked {doc_id}")
        except Exception as e:
            print(e)
print("Done unlocking")
