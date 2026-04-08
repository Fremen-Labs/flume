import sys
import os
from pathlib import Path

_WS = Path("/app")
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
_SRC = _WS / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_WD = _SRC / "worker-manager"
if str(_WD) not in sys.path:
    sys.path.insert(0, str(_WD))
_DB = _SRC / "dashboard"
if str(_DB) not in sys.path:
    sys.path.insert(0, str(_DB))

from flume_secrets import apply_runtime_config
apply_runtime_config(_WS)

import worker_handlers

import urllib.request
import json
req = urllib.request.Request("http://elasticsearch:9200/flume-projects/_doc/proj-5bc17450")
res = urllib.request.urlopen(req).read()
data = json.loads(res.decode('utf-8'))
repo_url = data['_source']['repoUrl']
print("Original URL:", repo_url)
print("Auth URL:", worker_handlers._build_auth_clone_url(repo_url, "proj-5bc17450"))
