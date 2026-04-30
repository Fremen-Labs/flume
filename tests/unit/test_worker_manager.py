"""
Unit tests for worker-manager/manager.py — Concurrency and Sweep Engine.

Tests the node concurrency cap calculation, block/resume sweep logic,
and emergency brake ramp-down without requiring running infrastructure.
"""
from unittest.mock import patch
import time

# Add worker-manager to path directly since it has a hyphen in its name
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'src' / 'worker-manager'))

import manager

@patch('manager.es_request')
def test_fetch_node_concurrency_caps_heterogeneous(mock_es):
    """Tests that heterogeneous node caps and emergency brakes are correctly scaled"""
    
    mock_es.return_value = {
        'hits': {
            'hits': [
                {
                    '_source': {
                        'id': 'mac-studio',
                        'concurrency_cap': 8,
                        'health': {'latency_ms': 150} # Healthy
                    }
                },
                {
                    '_source': {
                        'id': 'mac-mini-1',
                        'concurrency_cap': 2,
                        'health': {'latency_ms': 25000} # Severe Latency! Should ramp down
                    }
                }
            ]
        }
    }
    
    caps = manager._fetch_node_concurrency_caps()
    assert caps['mac-studio'] == 8
    # Baseline for mac-mini-1 was 2. With 25s latency (> 20s), Emergency Brake ramps down by 1 -> 1
    assert caps['mac-mini-1'] == 1
    # Fallback missing localhost
    assert caps['localhost'] == 4
    
    # Test strict clamp to 1 natively (does not drop below 1)
    mock_es.return_value['hits']['hits'][1]['_source']['concurrency_cap'] = 1
    caps2 = manager._fetch_node_concurrency_caps()
    assert caps2['mac-mini-1'] == 1

@patch('manager.es_request')
def test_execute_block_sweep(mock_es):
    """Tests that tasks are pushed to blocked only when total capacity is saturated"""
    
    mock_es.return_value = {'updated': 1}
    node_loads = {'mac-studio': 4, 'mac-mini-1': 2}
    node_caps = {'mac-studio': 8, 'mac-mini-1': 2}
    
    # Total Load (6) < Total Cap (10). Should NOT sweep.
    manager._execute_block_sweep(node_loads, node_caps, {'openai'})
    mock_es.assert_not_called()
    
    # Total Load (10) >= Total Cap (10). Should sweep!
    node_loads['mac-studio'] = 8
    manager._execute_block_sweep(node_loads, node_caps, {'openai'})
    mock_es.assert_called_once()
    
    call_args = mock_es.call_args[0]
    assert '/_update_by_query' in call_args[0]
    assert call_args[1]['script']['id'] == 'flume-task-block'

@patch('manager.es_request')
def test_execute_resume_sweep_jitter(mock_es):
    """Tests that Jitter correctly prevents immediate cyclical sweeps natively"""
    
    mock_es.return_value = {'updated': 1}
    manager.last_resume_timestamp = time.time()
    
    # Called immediately after another sweep. Should abort due to 60s + 1-15s Jitter
    manager._execute_resume_sweep()
    mock_es.assert_not_called()
    
    # Fake time passing by 100 seconds
    manager.last_resume_timestamp = time.time() - 100
    manager._execute_resume_sweep()
    mock_es.assert_called_once()
    
    call_args = mock_es.call_args[0]
    assert '/_update_by_query' in call_args[0]
    assert call_args[1]['script']['id'] == 'flume-task-resume'
