"""
Shared unit test configuration.

Provides isolated module mocking for dashboard source imports.
Each test module can import from `core.*` and `utils.*` without
needing a running Elasticsearch, OpenBao, or worker-manager.
"""
import sys
import os
from unittest.mock import MagicMock
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

# Mock core.elasticsearch with the attributes that other modules import directly
_es_mock = MagicMock()
_es_mock.es_search = MagicMock(return_value={'hits': {'hits': []}})
_es_mock.es_post = MagicMock(return_value={})
_es_mock.es_upsert = MagicMock(return_value={})
_es_mock.ES_URL = 'http://localhost:9200'
_es_mock.ES_API_KEY = ''
_es_mock.ctx = None
_es_mock.find_task_doc_by_logical_id = MagicMock(return_value=(None, None))
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
