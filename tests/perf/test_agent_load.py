"""
Performance tests for the Worker Pool Manager — Deadlock Resistance.

Validates that the worker pool can handle concurrent task injection up to
its hardware concurrency limit without crashing, blocking, or deadlocking.

Usage:
    pytest tests/perf/ -m perf -v

These tests use mocked ES to avoid infrastructure dependency while
stress-testing the concurrency primitives.
"""
import time
import threading
import pytest
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

# Add worker-manager to path since it has a hyphen in its name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'src' / 'worker-manager'))

import manager


@pytest.mark.perf
class TestWorkerPoolDeadlockResistance:
    """Validates the Worker Pool manager's concurrency safety under load."""

    @patch('manager.es_request')
    def test_concurrent_cap_fetches(self, mock_es):
        """Multiple concurrent _fetch_node_concurrency_caps calls should not deadlock."""
        mock_es.return_value = {
            'hits': {
                'hits': [
                    {'_source': {'id': 'mac-studio', 'concurrency_cap': 8, 'health': {'latency_ms': 100}}},
                    {'_source': {'id': 'mac-mini-1', 'concurrency_cap': 4, 'health': {'latency_ms': 200}}},
                ]
            }
        }
        results = []
        errors = []

        def fetch():
            try:
                caps = manager._fetch_node_concurrency_caps()
                results.append(caps)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fetch) for _ in range(20)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        elapsed = time.time() - start

        assert len(errors) == 0, f"Concurrent fetches produced errors: {errors}"
        assert len(results) == 20
        assert elapsed < 5.0, f"Concurrent fetches took too long: {elapsed:.1f}s"

    @patch('manager.es_request')
    def test_rapid_block_sweep_no_race(self, mock_es):
        """Rapid sequential block sweeps should not produce race conditions."""
        mock_es.return_value = {'updated': 1}
        node_loads = {'mac-studio': 8}
        node_caps = {'mac-studio': 8}

        for _ in range(100):
            manager._execute_block_sweep(node_loads, node_caps, {'openai'})

        # Should have called ES exactly 100 times (1 call per sweep)
        assert mock_es.call_count == 100

    @patch('manager.es_request')
    def test_resume_sweep_jitter_prevents_thundering_herd(self, mock_es):
        """Resume sweep jitter should prevent all calls from executing immediately."""
        mock_es.return_value = {'updated': 1}

        # Set last resume to now — all subsequent calls should be jitter-blocked
        manager.last_resume_timestamp = time.time()

        executed_count = 0
        for _ in range(50):
            manager._execute_resume_sweep()
            if mock_es.called:
                executed_count += 1
                mock_es.reset_mock()

        # With 60s + jitter cooldown, none should execute
        assert executed_count == 0

    @patch('manager.es_request')
    def test_heterogeneous_node_scaling(self, mock_es):
        """Tests emergency brake ramp-down across many heterogeneous nodes."""
        nodes = []
        for i in range(10):
            latency = 100 if i < 5 else 25000  # 5 healthy, 5 degraded
            nodes.append({
                '_source': {
                    'id': f'node-{i}',
                    'concurrency_cap': 8,
                    'health': {'latency_ms': latency},
                }
            })
        mock_es.return_value = {'hits': {'hits': nodes}}

        caps = manager._fetch_node_concurrency_caps()

        # Healthy nodes should keep their cap
        for i in range(5):
            assert caps[f'node-{i}'] == 8

        # Degraded nodes should have ramped down (25s > 20s threshold)
        for i in range(5, 10):
            assert caps[f'node-{i}'] < 8
            assert caps[f'node-{i}'] >= 1  # Never below 1
