import urllib.request, json, urllib.error
from src.flume_secrets import apply_runtime_config
apply_runtime_config()
from src.dashboard.llm_settings import _openbao_enabled, _openbao_secret_ref, _openbao_put_many, _openbao_get_all
from pathlib import Path

try:
    print("PUTTING...", _openbao_put_many(Path.cwd(), {'LLM_PROVIDER': 'gemini', 'LLM_MODEL': 'gemini-2.5-flash', 'LLM_API_KEY': 'AIza-test-dynamic-reload'}))
    print("GETTING...", _openbao_get_all(Path.cwd()))
except Exception as e:
    print(e)
