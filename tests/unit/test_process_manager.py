"""
Unit tests for core/process_manager.py — Worker Agent Lifecycle Orchestration.

Tests the process manager's agent start/stop lifecycle, task requeuing,
CLI resolution, and auto-start gating without requiring running infrastructure.
All subprocess, ES, and OS-level dependencies are mocked.

Follows Google's Table-Driven Test Pattern for deterministic validation.
"""
import os
import signal
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, timezone

from core.process_manager import (
    _find_worker_pids,
    agents_status,
    _requeue_running_tasks,
    agents_stop,
    agents_start,
    _resolve_flume_cli,
    restart_flume_services,
    maybe_auto_start_workers,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _find_worker_pids — Deprecation Verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindWorkerPids:
    """Validates the deprecated pid-finder returns empty sentinel."""

    def test_returns_empty_sentinel(self):
        """Deprecated function should return empty manager/handler lists."""
        result = _find_worker_pids()
        assert result == {'manager': [], 'handlers': []}

    def test_return_type(self):
        assert isinstance(_find_worker_pids(), dict)


# ═══════════════════════════════════════════════════════════════════════════════
# agents_status — ES Heartbeat Aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentsStatus:
    """Validates the agent cluster status aggregation from ES heartbeats."""

    @patch('core.process_manager.es_search')
    def test_healthy_agents(self, mock_search):
        """Active heartbeats within 30s should report running=True."""
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        mock_search.side_effect = [
            # Cluster config query
            {'hits': {'hits': [{'_source': {'status': 'running'}}]}},
            # Workers heartbeat query
            {'hits': {'hits': [{'_source': {'updated_at': now}}]}},
        ]
        result = agents_status()
        assert result['running'] is True
        assert result['manager_running'] is True
        assert result['cluster_status'] == 'running'

    @patch('core.process_manager.es_search')
    def test_no_heartbeats(self, mock_search):
        """No worker heartbeats should report running=False."""
        mock_search.side_effect = [
            {'hits': {'hits': [{'_source': {'status': 'running'}}]}},
            {'hits': {'hits': []}},
        ]
        result = agents_status()
        assert result['running'] is False
        assert result['manager_running'] is False

    @patch('core.process_manager.es_search')
    def test_paused_cluster(self, mock_search):
        """Paused cluster status should report running=False even with active nodes."""
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        mock_search.side_effect = [
            {'hits': {'hits': [{'_source': {'status': 'paused'}}]}},
            {'hits': {'hits': [{'_source': {'updated_at': now}}]}},
        ]
        result = agents_status()
        assert result['running'] is False
        assert result['cluster_status'] == 'paused'

    @patch('core.process_manager.es_search')
    def test_stale_heartbeats(self, mock_search):
        """Heartbeats older than 30s should not count as active."""
        stale = '2020-01-01T00:00:00Z'
        mock_search.side_effect = [
            {'hits': {'hits': [{'_source': {'status': 'running'}}]}},
            {'hits': {'hits': [{'_source': {'updated_at': stale}}]}},
        ]
        result = agents_status()
        assert result['running'] is False

    @patch('core.process_manager.es_search')
    def test_es_failure_returns_error(self, mock_search):
        """ES failure should return running=False with error message."""
        mock_search.side_effect = Exception('Connection refused')
        result = agents_status()
        assert result['running'] is False
        assert 'error' in result


# ═══════════════════════════════════════════════════════════════════════════════
# _requeue_running_tasks — Task Status Reset
# ═══════════════════════════════════════════════════════════════════════════════

class TestRequeueRunningTasks:
    """Validates the task requeue logic after worker shutdown."""

    @patch('core.process_manager.es_post')
    @patch('core.process_manager.es_search')
    def test_requeue_developer_tasks_to_ready(self, mock_search, mock_post):
        """Developer tasks should requeue to 'ready'."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_id': 'task-1', '_source': {'assigned_agent_role': 'developer'}},
            ]}
        }
        mock_post.return_value = {}
        count = _requeue_running_tasks()
        assert count == 1
        call_args = mock_post.call_args[0]
        assert 'ready' in str(call_args[1])

    @patch('core.process_manager.es_post')
    @patch('core.process_manager.es_search')
    def test_requeue_tester_tasks_to_review(self, mock_search, mock_post):
        """Tester tasks should requeue to 'review'."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_id': 'task-1', '_source': {'assigned_agent_role': 'tester'}},
            ]}
        }
        mock_post.return_value = {}
        count = _requeue_running_tasks()
        assert count == 1
        call_args = mock_post.call_args[0]
        assert 'review' in str(call_args[1])

    @patch('core.process_manager.es_post')
    @patch('core.process_manager.es_search')
    def test_requeue_pm_tasks_to_planned(self, mock_search, mock_post):
        """PM tasks should requeue to 'planned'."""
        mock_search.return_value = {
            'hits': {'hits': [
                {'_id': 'task-1', '_source': {'assigned_agent_role': 'pm'}},
            ]}
        }
        mock_post.return_value = {}
        count = _requeue_running_tasks()
        assert count == 1
        call_args = mock_post.call_args[0]
        assert 'planned' in str(call_args[1])

    @patch('core.process_manager.es_post')
    @patch('core.process_manager.es_search')
    def test_requeue_no_tasks(self, mock_search, mock_post):
        """No running tasks should return 0 requeued."""
        mock_search.return_value = {'hits': {'hits': []}}
        count = _requeue_running_tasks()
        assert count == 0
        mock_post.assert_not_called()

    @patch('core.process_manager.es_search')
    def test_requeue_es_failure_returns_zero(self, mock_search):
        """ES failure during requeue should return 0, not crash."""
        mock_search.side_effect = Exception('ES unreachable')
        count = _requeue_running_tasks()
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# agents_stop — Worker Shutdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentsStop:
    """Validates the agent stop lifecycle."""

    @patch('core.process_manager._requeue_running_tasks', return_value=3)
    @patch('core.process_manager._find_worker_pids', return_value={'manager': [], 'handlers': []})
    def test_stop_returns_ok(self, mock_pids, mock_requeue):
        """Stop should return ok=True with requeue count."""
        result = agents_stop()
        assert result['ok'] is True
        assert result['requeued_tasks'] == 3

    @patch('core.process_manager._requeue_running_tasks', return_value=0)
    @patch('core.process_manager._find_worker_pids', return_value={'manager': [], 'handlers': []})
    def test_stop_no_pids(self, mock_pids, mock_requeue):
        """Stop with no running processes should still succeed."""
        result = agents_stop()
        assert result['ok'] is True
        assert result['killed_pids'] == []


# ═══════════════════════════════════════════════════════════════════════════════
# agents_start — Worker Launch
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentsStart:
    """Validates the agent start lifecycle."""

    @patch('core.process_manager.subprocess.Popen')
    @patch('core.process_manager._find_worker_pids', return_value={'manager': [], 'handlers': []})
    def test_start_launches_both(self, mock_pids, mock_popen):
        """Start should launch manager and handlers when neither is running."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc
        result = agents_start()
        assert result['ok'] is True
        assert len(result['started']) == 2
        assert result['started'][0]['role'] == 'manager'
        assert result['started'][1]['role'] == 'handlers'

    @patch('core.process_manager.subprocess.Popen')
    @patch('core.process_manager._find_worker_pids', return_value={'manager': [999], 'handlers': [1000]})
    def test_start_already_running(self, mock_pids, mock_popen):
        """Start should not launch processes when already running."""
        result = agents_start()
        assert result['ok'] is True
        assert result['already_running'] is True
        mock_popen.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# _resolve_flume_cli — CLI Path Resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveFlumeCli:
    """Validates the flume CLI resolution fallback chain."""

    @patch('core.process_manager.WORKSPACE_ROOT', Path('/tmp/test-workspace'))
    def test_not_found(self):
        """Missing flume CLI should return None."""
        result = _resolve_flume_cli()
        assert result is None

    @patch('core.process_manager.WORKSPACE_ROOT')
    def test_found_in_workspace(self, mock_root, tmp_path):
        """Flume CLI in workspace root should be found."""
        (tmp_path / 'flume').touch()
        mock_root.resolve.return_value = tmp_path
        result = _resolve_flume_cli()
        assert result is not None
        assert result.name == 'flume'


# ═══════════════════════════════════════════════════════════════════════════════
# maybe_auto_start_workers — Env Var Gating
# ═══════════════════════════════════════════════════════════════════════════════

class TestMaybeAutoStartWorkers:
    """Validates the auto-start env var gating logic."""

    @pytest.mark.parametrize("env_val", ['0', 'false', 'no', 'off', 'False', 'NO', 'OFF'])
    @patch('core.process_manager.agents_start')
    def test_disabled_values(self, mock_start, env_val):
        """Workers should NOT start when FLUME_AUTO_START_WORKERS is a disable value."""
        with patch.dict(os.environ, {'FLUME_AUTO_START_WORKERS': env_val}):
            maybe_auto_start_workers()
            mock_start.assert_not_called()

    @pytest.mark.parametrize("env_val", ['1', 'true', 'yes'])
    @patch('core.process_manager.agents_start', return_value={'started': [{'role': 'manager', 'pid': 1}]})
    def test_enabled_values(self, mock_start, env_val):
        """Workers SHOULD start when FLUME_AUTO_START_WORKERS is an enable value."""
        with patch.dict(os.environ, {'FLUME_AUTO_START_WORKERS': env_val}):
            maybe_auto_start_workers()
            mock_start.assert_called_once()

    @patch('core.process_manager.agents_start', return_value={'started': [{'role': 'manager', 'pid': 1}]})
    def test_default_enabled(self, mock_start):
        """Workers should start by default when env var is not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FLUME_AUTO_START_WORKERS', None)
            maybe_auto_start_workers()
            mock_start.assert_called_once()

    @patch('core.process_manager.agents_start', side_effect=Exception('boom'))
    def test_start_failure_does_not_crash(self, mock_start):
        """Failure during auto-start should be caught, not propagated."""
        with patch.dict(os.environ, {'FLUME_AUTO_START_WORKERS': '1'}):
            maybe_auto_start_workers()  # Should not raise
