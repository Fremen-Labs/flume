"""
Unit tests for core/projects_store.py — Async API Surface.

Tests the async project CRUD operations (load, upsert, update, delete)
with mocked Elasticsearch to verify correct delegation, error handling,
and gitflow default backfilling.

All ES dependencies are mocked via conftest.py.

Follows Google's Table-Driven Test Pattern for deterministic validation.
"""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

from core.projects_store import (
    async_load_projects_registry,
    async_upsert_project,
    async_update_project_field,
    async_delete_project,
    _utcnow_iso,
    _update_project_registry_field,
    _delete_project_from_es,
    PROJECTS_INDEX,
    _MAX_PROJECTS_SEARCH_SIZE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _utcnow_iso — Timestamp Format
# ═══════════════════════════════════════════════════════════════════════════════

class TestUtcnowIso:
    """Validates the UTC timestamp format helper."""

    def test_returns_string(self):
        result = _utcnow_iso()
        assert isinstance(result, str)

    def test_ends_with_z(self):
        """ISO timestamp should end with 'Z' suffix, not '+00:00'."""
        result = _utcnow_iso()
        assert result.endswith('Z')
        assert '+00:00' not in result

    def test_parseable(self):
        """Timestamp should be parseable by fromisoformat."""
        result = _utcnow_iso()
        parsed = datetime.fromisoformat(result.replace('Z', '+00:00'))
        assert parsed.tzinfo is not None


# ═══════════════════════════════════════════════════════════════════════════════
# async_load_projects_registry — Non-blocking Load
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncLoadProjectsRegistry:
    """Validates the async project registry load from ES."""

    @patch('core.projects_store.async_es_search', new_callable=AsyncMock)
    def test_empty_index(self, mock_search):
        """Empty ES index should return empty list."""
        mock_search.return_value = {'hits': {'hits': []}}
        result = asyncio.run(async_load_projects_registry())
        assert result == []
        mock_search.assert_called_once_with(PROJECTS_INDEX, {
            'size': _MAX_PROJECTS_SEARCH_SIZE,
            'query': {'match_all': {}},
            'sort': [{'created_at': {'order': 'asc'}}],
        })

    @patch('core.projects_store.async_es_search', new_callable=AsyncMock)
    def test_returns_projects_with_gitflow_defaults(self, mock_search):
        """Projects should be returned with gitflow defaults backfilled."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_source': {'id': 'proj-1', 'name': 'Test Project'}},
            ]}
        }
        result = asyncio.run(async_load_projects_registry())
        assert len(result) == 1
        assert result[0]['id'] == 'proj-1'
        assert 'gitflow' in result[0]
        assert result[0]['gitflow']['integrationBranch'] == 'develop'

    @patch('core.projects_store.async_es_search', new_callable=AsyncMock)
    def test_filters_hits_without_source(self, mock_search):
        """Hits without _source should be filtered out."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_id': 'no-source'},  # No _source
                {'_source': {'id': 'proj-1', 'name': 'Valid'}},
            ]}
        }
        result = asyncio.run(async_load_projects_registry())
        assert len(result) == 1

    @patch('core.projects_store.async_es_search', new_callable=AsyncMock)
    def test_es_failure_returns_empty(self, mock_search):
        """ES failure should return empty list, not crash."""
        mock_search.side_effect = Exception('Connection refused')
        result = asyncio.run(async_load_projects_registry())
        assert result == []

    @patch('core.projects_store.async_es_search', new_callable=AsyncMock)
    def test_multiple_projects_sorted(self, mock_search):
        """Multiple projects should be returned in sort order."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_source': {'id': 'proj-1', 'name': 'Alpha'}},
                {'_source': {'id': 'proj-2', 'name': 'Beta'}},
                {'_source': {'id': 'proj-3', 'name': 'Gamma'}},
            ]}
        }
        result = asyncio.run(async_load_projects_registry())
        assert len(result) == 3
        ids = [p['id'] for p in result]
        assert ids == ['proj-1', 'proj-2', 'proj-3']


# ═══════════════════════════════════════════════════════════════════════════════
# async_upsert_project — Non-blocking Upsert
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncUpsertProject:
    """Validates the async project upsert."""

    @patch('core.projects_store.async_es_upsert', new_callable=AsyncMock)
    def test_upsert_adds_updated_at(self, mock_upsert):
        """Upsert should add an updated_at timestamp to the entry."""
        mock_upsert.return_value = {'result': 'created'}
        entry = {'id': 'proj-1', 'name': 'Test'}
        asyncio.run(async_upsert_project(entry))
        assert 'updated_at' in entry
        assert entry['updated_at'].endswith('Z')

    @patch('core.projects_store.async_es_upsert', new_callable=AsyncMock)
    def test_upsert_calls_es_with_correct_index(self, mock_upsert):
        """Upsert should call async_es_upsert with the projects index."""
        mock_upsert.return_value = {'result': 'created'}
        entry = {'id': 'proj-1', 'name': 'Test'}
        asyncio.run(async_upsert_project(entry))
        mock_upsert.assert_called_once_with(PROJECTS_INDEX, 'proj-1', entry)

    @patch('core.projects_store.async_es_upsert', new_callable=AsyncMock)
    def test_upsert_returns_es_response(self, mock_upsert):
        """Upsert should return the ES response dict."""
        mock_upsert.return_value = {'result': 'updated', '_id': 'proj-1'}
        entry = {'id': 'proj-1', 'name': 'Test'}
        result = asyncio.run(async_upsert_project(entry))
        assert result['result'] == 'updated'


# ═══════════════════════════════════════════════════════════════════════════════
# async_update_project_field — Surgical Field Update
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncUpdateProjectField:
    """Validates the atomic field-level project update."""

    @patch('core.projects_store.async_es_post', new_callable=AsyncMock)
    def test_update_calls_es_update_api(self, mock_post):
        """Update should use the ES _update API with doc_as_upsert."""
        mock_post.return_value = {}
        asyncio.run(
            async_update_project_field('proj-1', clone_status='cloned', path='/path')
        )
        call_args = mock_post.call_args
        path = call_args[0][0]
        body = call_args[0][1]
        assert f'{PROJECTS_INDEX}/_update/proj-1' in path
        assert 'refresh=wait_for' in path
        assert body['doc']['clone_status'] == 'cloned'
        assert body['doc']['path'] == '/path'
        assert 'updated_at' in body['doc']
        assert body['doc_as_upsert'] is True

    @patch('core.projects_store.async_es_post', new_callable=AsyncMock)
    def test_update_failure_does_not_crash(self, mock_post):
        """ES failure during update should log warning, not crash."""
        mock_post.side_effect = Exception('ES timeout')
        # Should not raise
        asyncio.run(
            async_update_project_field('proj-1', clone_status='failed')
        )


# ═══════════════════════════════════════════════════════════════════════════════
# async_delete_project — Non-blocking Delete
# ═══════════════════════════════════════════════════════════════════════════════

class TestAsyncDeleteProject:
    """Validates the async project deletion."""

    @patch('core.projects_store.async_es_delete_doc', new_callable=AsyncMock)
    def test_delete_returns_true(self, mock_delete):
        """Successful delete should return True."""
        mock_delete.return_value = True
        result = asyncio.run(async_delete_project('proj-1'))
        assert result is True
        mock_delete.assert_called_once_with(PROJECTS_INDEX, 'proj-1')

    @patch('core.projects_store.async_es_delete_doc', new_callable=AsyncMock)
    def test_delete_idempotent_404(self, mock_delete):
        """Deleting a non-existent project should return True (idempotent)."""
        mock_delete.return_value = True  # 404 returns True in the implementation
        result = asyncio.run(async_delete_project('nonexistent'))
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Helpers — Backward Compatibility
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncUpdateProjectField:
    """Validates the synchronous field update helper."""

    @patch('core.projects_store._es_projects_request')
    def test_update_adds_timestamp(self, mock_req):
        """Sync update should add updated_at to the field payload."""
        mock_req.return_value = {}
        _update_project_registry_field('proj-1', clone_status='cloned')
        call_args = mock_req.call_args
        body = call_args[0][1]
        assert 'updated_at' in body['doc']

    @patch('core.projects_store._es_projects_request')
    def test_update_uses_refresh_wait_for(self, mock_req):
        """Sync update should use refresh=wait_for for immediate visibility."""
        mock_req.return_value = {}
        _update_project_registry_field('proj-1', status='active')
        call_args = mock_req.call_args
        path = call_args[0][0]
        assert 'refresh=wait_for' in path


class TestSyncDeleteProject:
    """Validates the synchronous project delete."""

    @patch('core.projects_store._es_projects_request')
    def test_delete_calls_es_delete(self, mock_req):
        """Delete should call ES with DELETE method."""
        mock_req.return_value = {}
        _delete_project_from_es('proj-1')
        call_args = mock_req.call_args
        assert call_args[1]['method'] == 'DELETE'
        assert 'proj-1' in call_args[0][0]

    @patch('core.projects_store._es_projects_request')
    def test_delete_failure_does_not_crash(self, mock_req):
        """ES failure during delete should not crash."""
        mock_req.side_effect = TimeoutError('Connection timeout')
        _delete_project_from_es('proj-1')  # Should not raise
