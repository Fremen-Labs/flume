"""
Unit tests for core/system_status.py — Snapshot Cache and Status Aggregation.

Tests the TTL-based snapshot cache, task hit merging, worker role extraction,
and local repo loading without requiring running infrastructure.
All ES and filesystem dependencies are mocked.

Follows Google's Table-Driven Test Pattern for deterministic validation.
"""
import time
import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

from core.system_status import (
    _SnapshotCache,
    _merge_recent_task_hits_with_blocked,
    _extract_worker_role,
    load_repos,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _SnapshotCache — TTL Cache Behavior
# ═══════════════════════════════════════════════════════════════════════════════

class TestSnapshotCache:
    """Validates the TTL-based dashboard snapshot cache."""

    def test_empty_cache_returns_none(self):
        """Fresh cache should return None."""
        cache = _SnapshotCache(ttl=2.0)
        assert cache.get() is None

    def test_set_and_get(self):
        """Setting data should make it retrievable."""
        cache = _SnapshotCache(ttl=2.0)
        data = {'workers': [], 'projects': []}
        cache.set(data)
        result = cache.get()
        assert result is data

    def test_ttl_expiration(self):
        """Data should expire after TTL elapses."""
        cache = _SnapshotCache(ttl=0.05)  # 50ms TTL
        cache.set({'workers': []})
        assert cache.get() is not None
        time.sleep(0.1)  # Wait for TTL to expire
        assert cache.get() is None

    def test_set_resets_ttl(self):
        """Setting new data should reset the TTL timer."""
        cache = _SnapshotCache(ttl=0.1)
        cache.set({'first': True})
        time.sleep(0.06)
        cache.set({'second': True})
        time.sleep(0.06)
        # Second set was 60ms ago, TTL is 100ms — should still be valid
        result = cache.get()
        assert result is not None
        assert result.get('second') is True

    def test_zero_ttl_always_expired(self):
        """A cache with TTL=0 should always miss (after any measurable time)."""
        cache = _SnapshotCache(ttl=0)
        cache.set({'data': True})
        time.sleep(0.001)
        assert cache.get() is None


# ═══════════════════════════════════════════════════════════════════════════════
# _merge_recent_task_hits_with_blocked — Hit Deduplication
# ═══════════════════════════════════════════════════════════════════════════════

class TestMergeRecentTaskHitsWithBlocked:
    """Validates the task hit merging and deduplication logic."""

    def test_empty_inputs(self):
        assert _merge_recent_task_hits_with_blocked([], []) == []

    def test_recent_only(self):
        recent = [
            {'_id': 'r1', '_source': {'id': 'task-1'}},
            {'_id': 'r2', '_source': {'id': 'task-2'}},
        ]
        result = _merge_recent_task_hits_with_blocked(recent, [])
        assert len(result) == 2

    def test_blocked_only(self):
        blocked = [
            {'_id': 'b1', '_source': {'id': 'task-blocked-1'}},
        ]
        result = _merge_recent_task_hits_with_blocked([], blocked)
        assert len(result) == 1

    def test_deduplication(self):
        """Blocked tasks already in recent should not be duplicated."""
        recent = [{'_id': 'r1', '_source': {'id': 'task-1'}}]
        blocked = [{'_id': 'b1', '_source': {'id': 'task-1'}}]  # Same task ID
        result = _merge_recent_task_hits_with_blocked(recent, blocked)
        assert len(result) == 1

    def test_blocked_appended_after_recent(self):
        """Blocked tasks not in recent should appear after recent tasks."""
        recent = [{'_id': 'r1', '_source': {'id': 'task-1'}}]
        blocked = [{'_id': 'b1', '_source': {'id': 'task-blocked-1'}}]
        result = _merge_recent_task_hits_with_blocked(recent, blocked)
        assert len(result) == 2
        assert result[0]['_source']['id'] == 'task-1'
        assert result[1]['_source']['id'] == 'task-blocked-1'

    def test_order_preservation(self):
        """Recent task order should be preserved."""
        recent = [
            {'_id': 'r1', '_source': {'id': 'task-3'}},
            {'_id': 'r2', '_source': {'id': 'task-1'}},
            {'_id': 'r3', '_source': {'id': 'task-2'}},
        ]
        result = _merge_recent_task_hits_with_blocked(recent, [])
        ids = [h['_source']['id'] for h in result]
        assert ids == ['task-3', 'task-1', 'task-2']

    def test_missing_source_with_id_fallback(self):
        """Hits without _source but with _id should use _id as key."""
        recent = [
            {'_id': 'r1'},  # No _source — falls back to _id
            {'_id': 'r2', '_source': {'id': 'task-1'}},
        ]
        result = _merge_recent_task_hits_with_blocked(recent, [])
        # Both hits are processed: r1 has no _source so id falls back to _id 'r1'
        assert len(result) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_worker_role — Aggregation Bucket Parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractWorkerRole:
    """Validates worker role extraction from ES aggregation buckets."""

    def test_with_role_bucket(self):
        bucket = {'role': {'buckets': [{'key': 'developer'}]}}
        assert _extract_worker_role(bucket) == 'developer'

    def test_empty_role_buckets(self):
        bucket = {'role': {'buckets': []}}
        assert _extract_worker_role(bucket) == 'unknown'

    def test_missing_role_key(self):
        bucket = {}
        assert _extract_worker_role(bucket) == 'unknown'

    def test_multiple_role_buckets_uses_first(self):
        """First bucket should be used when multiple exist."""
        bucket = {'role': {'buckets': [
            {'key': 'senior_developer'},
            {'key': 'developer'},
        ]}}
        assert _extract_worker_role(bucket) == 'senior_developer'


# ═══════════════════════════════════════════════════════════════════════════════
# load_repos — Local Project Enumeration
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadRepos:
    """Validates the local repo loading from project registry."""

    def test_empty_registry(self):
        """Empty registry should return empty list."""
        result = asyncio.run(load_repos(registry=[]))
        assert result == []

    def test_filters_non_local_projects(self):
        """Projects without clone_status='local' should be filtered out."""
        registry = [
            {'id': 'proj-1', 'path': '/path/1', 'clone_status': 'cloning'},
            {'id': 'proj-2', 'path': '/path/2', 'clone_status': 'failed'},
            {'id': 'proj-3', 'path': '', 'clone_status': 'local'},
        ]
        result = asyncio.run(load_repos(registry=registry))
        assert result == []

    def test_filters_missing_path(self):
        """Projects without a path should be filtered out."""
        registry = [
            {'id': 'proj-1', 'path': '', 'clone_status': 'local'},
            {'id': 'proj-2', 'clone_status': 'local'},
        ]
        result = asyncio.run(load_repos(registry=registry))
        assert result == []

    @patch('core.system_status.git_repo_info', new_callable=AsyncMock)
    def test_loads_local_projects(self, mock_git_info):
        """Projects with clone_status='local' and a path should be loaded."""
        mock_git_info.return_value = {'id': 'proj-1', 'branches': ['main']}
        registry = [
            {'id': 'proj-1', 'path': '/some/path', 'clone_status': 'local'},
        ]
        result = asyncio.run(load_repos(registry=registry))
        assert len(result) == 1
        mock_git_info.assert_called_once()
