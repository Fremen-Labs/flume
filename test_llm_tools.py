import sys
from pathlib import Path
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")
from flume_secrets import apply_runtime_config  # noqa: E402
apply_runtime_config(Path("/app"))
from utils import llm_client  # noqa: E402
try:
    messages = [{"role": "user", "content": "Hello"}]
    tools = [{'type': 'function', 'function': {'name': 'hello', 'description': 'desc', 'parameters': {'type': 'object', 'properties': {}}}}]
    print("Testing chat_with_tools...")
    res = llm_client.chat_with_tools(messages, tools, model="gemma4:26b")
    print("Success:", res)
except Exception:
    import traceback
    print("Exception!")
    traceback.print_exc()
