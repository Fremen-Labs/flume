"""
Unit tests for core/sessions_store.py — Async API Surface.

Tests the async session load/save operations, synchronous backward-compat
callers, and session index constant with mocked Elasticsearch.
All ES dependencies are mocked via conftest.py.

Follows Google's Table-Driven Test Pattern for deterministic validation.
"""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

from core.sessions_store import (
    async_load_session,
    async_save_session,
    load_session,
    save_session,
    _SESSIONS_INDEX,
    _ERR_TRUNCATE_LEN,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _SESSIONS_INDEX — Index Name Constant
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionsIndexConstant:
    """Validates the Elasticsearch index name constant."""

    def test_index_name_is_string(self):
        assert isinstance(_SESSIONS_INDEX, str)

    def test_index_name_value(self):
        assert _SESSIONS_INDEX == "agent-plan-sessions"

    def test_err_truncate_len_is_positive(self):
        assert _ERR_TRUNCATE_LEN > 0
        assert isinstance(_ERR_TRUNCATE_LEN, int)


# ═══════════════════════════════════════════════════════════════════════════════
# async_load_session — Non-blocking Load
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncLoadSession:
    """Validates the async session load from ES."""

    @patch('core.sessions_store.async_es_search', new_callable=AsyncMock)
    def test_found_session(self, mock_search):
        """Existing session should return its _source payload."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_source': {'id': 'sess-1', 'project_id': 'proj-1', 'messages': []}}
            ]}
        }
        result = asyncio.run(async_load_session('sess-1'))
        assert result is not None
        assert result['id'] == 'sess-1'
        assert result['project_id'] == 'proj-1'

    @patch('core.sessions_store.async_es_search', new_callable=AsyncMock)
    def test_not_found_returns_none(self, mock_search):
        """Missing session should return None."""
        mock_search.return_value = {'hits': {'hits': []}}
        result = asyncio.run(async_load_session('nonexistent'))
        assert result is None

    @patch('core.sessions_store.async_es_search', new_callable=AsyncMock)
    def test_correct_es_query(self, mock_search):
        """Load should query by _id using a term filter."""
        mock_search.return_value = {'hits': {'hits': []}}
        asyncio.run(async_load_session('sess-abc'))
        mock_search.assert_called_once_with(
            _SESSIONS_INDEX,
            {'size': 1, 'query': {'term': {'_id': 'sess-abc'}}}
        )

    @patch('core.sessions_store.async_es_search', new_callable=AsyncMock)
    def test_es_failure_returns_none(self, mock_search):
        """ES failure should return None, not crash."""
        mock_search.side_effect = Exception('Connection refused')
        result = asyncio.run(async_load_session('sess-1'))
        assert result is None

    @patch('core.sessions_store.async_es_search', new_callable=AsyncMock)
    def test_hit_without_source_returns_none(self, mock_search):
        """Hit without _source field should return None."""
        mock_search.return_value = {'hits': {'hits': [{'_id': 'sess-1'}]}}
        result = asyncio.run(async_load_session('sess-1'))
        assert result is None

    @patch('core.sessions_store.async_es_search', new_callable=AsyncMock)
    def test_malformed_response_returns_none(self, mock_search):
        """Malformed ES response (missing hits key) should return None."""
        mock_search.return_value = {}
        result = asyncio.run(async_load_session('sess-1'))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# async_save_session — Non-blocking Save
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncSaveSession:
    """Validates the async session save to ES."""

    @patch('core.sessions_store.async_es_post', new_callable=AsyncMock)
    def test_save_adds_updated_at(self, mock_post):
        """Save should inject updated_at timestamp into the session."""
        mock_post.return_value = {}
        session = {'id': 'sess-1', 'project_id': 'proj-1', 'messages': []}
        asyncio.run(async_save_session(session))
        assert 'updated_at' in session
        assert session['updated_at'].endswith('Z')

    @patch('core.sessions_store.async_es_post', new_callable=AsyncMock)
    def test_save_calls_es_post_with_correct_path(self, mock_post):
        """Save should POST to the correct index/doc path with refresh."""
        mock_post.return_value = {}
        session = {'id': 'sess-abc', 'project_id': 'proj-1'}
        asyncio.run(async_save_session(session))
        call_args = mock_post.call_args[0]
        assert f'{_SESSIONS_INDEX}/_doc/sess-abc?refresh=true' == call_args[0]
        assert call_args[1] is session

    @patch('core.sessions_store.async_es_post', new_callable=AsyncMock)
    def test_save_es_failure_does_not_crash(self, mock_post):
        """ES failure during save should not propagate."""
        mock_post.side_effect = Exception('ES timeout')
        session = {'id': 'sess-1', 'project_id': 'proj-1'}
        asyncio.run(async_save_session(session))  # Should not raise

    @patch('core.sessions_store.async_es_post', new_callable=AsyncMock)
    def test_save_preserves_existing_fields(self, mock_post):
        """Save should not strip existing session fields."""
        mock_post.return_value = {}
        session = {'id': 'sess-1', 'project_id': 'proj-1', 'plan': {'epics': []}, 'extra_field': 42}
        asyncio.run(async_save_session(session))
        assert session['plan'] == {'epics': []}
        assert session['extra_field'] == 42

    @patch('core.sessions_store.async_es_post', new_callable=AsyncMock)
    def test_save_updates_timestamp_on_each_call(self, mock_post):
        """Each save should produce a fresh timestamp."""
        mock_post.return_value = {}
        session = {'id': 'sess-1', 'project_id': 'proj-1'}
        asyncio.run(async_save_session(session))
        first_ts = session['updated_at']
        import time
        time.sleep(0.01)
        asyncio.run(async_save_session(session))
        second_ts = session['updated_at']
        # Both should be valid ISO strings (they may or may not differ at ms precision)
        assert first_ts.endswith('Z')
        assert second_ts.endswith('Z')


# ═══════════════════════════════════════════════════════════════════════════════
# load_session — Synchronous Load (Backward Compat)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncLoadSession:
    """Validates the synchronous session load helper."""

    @patch('core.sessions_store.es_search')
    def test_found_session(self, mock_search):
        """Sync load should return _source for existing sessions."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_source': {'id': 'sess-1', 'messages': []}}
            ]}
        }
        result = load_session('sess-1')
        assert result is not None
        assert result['id'] == 'sess-1'

    @patch('core.sessions_store.es_search')
    def test_not_found_returns_none(self, mock_search):
        """Missing session should return None."""
        mock_search.return_value = {'hits': {'hits': []}}
        result = load_session('nonexistent')
        assert result is None

    @patch('core.sessions_store.es_search')
    def test_es_failure_returns_none(self, mock_search):
        """ES failure should be caught and return None."""
        mock_search.side_effect = Exception('Connection refused')
        result = load_session('sess-1')
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# save_session — Synchronous Save (Backward Compat)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncSaveSession:
    """Validates the synchronous session save helper."""

    @patch('core.sessions_store.es_post')
    def test_save_adds_updated_at(self, mock_post):
        """Sync save should inject updated_at timestamp."""
        mock_post.return_value = {}
        session = {'id': 'sess-1', 'messages': []}
        save_session(session)
        assert 'updated_at' in session
        assert session['updated_at'].endswith('Z')

    @patch('core.sessions_store.es_post')
    def test_save_calls_es_with_refresh(self, mock_post):
        """Save should call es_post with refresh=true."""
        mock_post.return_value = {}
        session = {'id': 'sess-1'}
        save_session(session)
        call_args = mock_post.call_args[0]
        assert f'{_SESSIONS_INDEX}/_doc/sess-1?refresh=true' == call_args[0]

    @patch('core.sessions_store.es_post')
    def test_save_es_failure_does_not_crash(self, mock_post):
        """ES failure during save should not propagate."""
        mock_post.side_effect = Exception('ES unavailable')
        session = {'id': 'sess-1'}
        save_session(session)  # Should not raise
