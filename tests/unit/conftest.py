"""
Shared unit test configuration.

Provides isolated module mocking for dashboard source imports.
Each test module can import from `core.*` and `utils.*` without
needing a running Elasticsearch, OpenBao, or worker-manager.
"""
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path

# ─── Dashboard Source Path ───────────────────────────────────────────────────
DASHBOARD_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'dashboard')
DASHBOARD_SRC = os.path.abspath(DASHBOARD_SRC)

if DASHBOARD_SRC not in sys.path:
    sys.path.insert(0, DASHBOARD_SRC)

# ─── Shared Module Mocks ─────────────────────────────────────────────────────
# These are set ONCE before any test collection, ensuring all test files
# share a consistent mock environment.

# Mock the logger module
_logger_mock = MagicMock()
_logger_mock.get_logger = MagicMock(return_value=MagicMock())
sys.modules['utils.logger'] = _logger_mock

# Mock the workspace module
_workspace_mock = MagicMock()
_workspace_mock.resolve_safe_workspace = MagicMock(return_value=Path('/tmp/flume-test-workspace'))
sys.modules['utils.workspace'] = _workspace_mock

# Mock the exceptions module with real exception classes
class _MockGitOperationError(Exception):
    """Mock GitOperationError for unit tests."""
    def __init__(self, operation='', stderr='', returncode=1):
        self.operation = operation
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"Git operation '{operation}' failed (rc={returncode}): {stderr}")

_exceptions_mock = MagicMock()
_exceptions_mock.SAFE_EXCEPTIONS = (Exception,)
_exceptions_mock.GitOperationError = _MockGitOperationError
sys.modules['utils.exceptions'] = _exceptions_mock

# Mock core.elasticsearch with the attributes that other modules import directly
_es_mock = MagicMock()
_es_mock.es_search = MagicMock(return_value={'hits': {'hits': []}})
_es_mock.es_post = MagicMock(return_value={})
_es_mock.es_upsert = MagicMock(return_value={})
_es_mock.ES_URL = 'http://localhost:9200'
_es_mock.ES_API_KEY = ''
_es_mock.ctx = None
_es_mock.find_task_doc_by_logical_id = MagicMock(return_value=(None, None))
_es_mock.async_es_search = AsyncMock(return_value={'hits': {'hits': []}})
_es_mock.async_es_post = AsyncMock(return_value={})
_es_mock.async_es_upsert = AsyncMock(return_value={'result': 'created'})
_es_mock.async_es_delete_doc = AsyncMock(return_value=True)
_es_mock.get_es_url = MagicMock(return_value='http://localhost:9200')
_es_mock.get_ssl_context = MagicMock(return_value=None)
_es_mock._get_auth_headers = MagicMock(return_value={})
sys.modules['core.elasticsearch'] = _es_mock

# Mock core.counters
_counters_mock = MagicMock()
_counters_mock.get_next_id_sequence = MagicMock(return_value=1)
_counters_mock.es_counter_set_hwm = MagicMock()
sys.modules['core.counters'] = _counters_mock

# Mock concurrency config (imported inside _ensure_gitflow_defaults)
_concurrency_mock = MagicMock()
_concurrency_mock.ensure_concurrency_defaults = MagicMock(return_value=None)
sys.modules['utils.concurrency_config'] = _concurrency_mock

# Mock config module (deferred imports in elasticsearch.py and planning.py)
_config_mock = MagicMock()
_settings_mock = MagicMock()
_settings_mock.ES_URL = 'http://localhost:9200'
_settings_mock.ES_API_KEY = ''
_settings_mock.FLUME_ELASTIC_PASSWORD = ''
_settings_mock.ES_VERIFY_TLS = False
_settings_mock.FLUME_NATIVE_MODE = '0'
_settings_mock.LLM_PROVIDER = 'ollama'
_settings_mock.LLM_MODEL = 'qwen3.5:32b'
_settings_mock.LLM_BASE_URL = 'http://localhost:11434'
_settings_mock.LOCAL_OLLAMA_BASE_URL = 'http://localhost:11434'
_settings_mock.FLUME_PLANNER_TIMEOUT_SECONDS = 300
_settings_mock.FLUME_DEBUG_PLANNER = ''
_config_mock.get_settings = MagicMock(return_value=_settings_mock)
_config_mock.AppConfig = _settings_mock
sys.modules['config'] = _config_mock

# Mock llm_settings (deferred import in planning.py for PROVIDER_CATALOG)
_llm_settings_mock = MagicMock()
_llm_settings_mock.PROVIDER_CATALOG = [
    {'id': 'ollama', 'name': 'Ollama', 'baseUrlDefault': 'http://localhost:11434'},
    {'id': 'openai', 'name': 'OpenAI', 'baseUrlDefault': 'https://api.openai.com/v1'},
]
sys.modules['llm_settings'] = _llm_settings_mock
