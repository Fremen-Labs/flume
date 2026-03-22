import ast
import os
import urllib.request
import json
from pathlib import Path

def sync_ast():
    """Robust fallback traversing AST scopes natively exporting boundaries to ES"""
    es_url = os.environ.get("ES_URL", "http://elasticsearch:9200")
    for filepath in Path("/app/src").rglob("*.py"):
        try:
            tree = ast.parse(filepath.read_text())
            classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            headers = {"Content-Type": "application/json"}
            payload = json.dumps({"file": str(filepath.name), "classes": classes})
            url = f"{es_url}/flume-elastro-graph/_doc"
            req = urllib.request.Request(url, data=payload.encode(), headers=headers, method="POST")
            try:
                urllib.request.urlopen(req, timeout=2)
            except:
                pass
        except:
            pass
