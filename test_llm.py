import sys
import os
import json
from pathlib import Path

_WS = Path("/app")
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
_SRC = _WS / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from flume_secrets import apply_runtime_config
apply_runtime_config(_WS)

from utils import llm_client

try:
    print("Testing LLM chat...")
    messages = [{"role": "user", "content": "Hello"}]
    res = llm_client.chat(messages, model="gemma4:26b", temperature=0.2, max_tokens=100, ollama_think=False)
    print("Success:")
    print(res)
except Exception as e:
    import traceback
    print("Failed:")
    traceback.print_exc()

