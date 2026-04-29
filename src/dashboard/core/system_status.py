import os
import time
import asyncio
from pathlib import Path

from core.tasks import load_workers, git_repo_info
from core.projects_store import load_projects_registry
from core.elasticsearch import async_es_search, ES_API_KEY
from utils.logger import get_logger
from utils.exceptions import SAFE_EXCEPTIONS
from api.projects import _map_task_hit_for_api

logger = get_logger(__name__)

_SNAPSHOT_CACHE_DATA = None
_SNAPSHOT_CACHE_TIME = 0.0

async def load_repos(registry=None):
    """Return git_repo_info for locally-mounted projects only."""
    registry = registry if registry is not None else load_projects_registry()
    repos = []
    for p in registry:
        local_path = p.get('path') or ''
        cs = p.get('clone_status') or ''
        if not local_path or cs not in ('local',):
            continue
        repos.append(await git_repo_info(p['id'], Path(local_path)))
    return repos

def _merge_recent_task_hits_with_blocked(recent_hits: list, blocked_hits: list) -> list:
    by_id: dict = {}
    order: list = []
    for h in recent_hits:
        src = h.get('_source') or {}
        tid = src.get('id') or h.get('_id')
        if not tid:
            continue
        k = str(tid)
        if k not in by_id:
            order.append(k)
        by_id[k] = h
    for h in blocked_hits:
        src = h.get('_source') or {}
        tid = src.get('id') or h.get('_id')
        if not tid:
            continue
        k = str(tid)
        if k in by_id:
            continue
        order.append(k)
        by_id[k] = h
    return [by_id[k] for k in order]

async def _async_fetch_savings():
    try:
        agg_res = await async_es_search('agent-token-telemetry', {
            'size': 0,
            'aggs': {
                'total_elastro_savings': {'sum': {'field': 'savings'}},
                'total_baseline_tokens': {'sum': {'field': 'baseline_tokens'}},
                'total_baseline_full_context': {'sum': {'field': 'baseline_full_context_tokens'}},
                'total_actual_tokens': {'sum': {'field': 'actual_tokens_sent'}},
                'total_input_tokens': {'sum': {'field': 'input_tokens'}},
                'total_output_tokens': {'sum': {'field': 'output_tokens'}},
                'by_worker': {
                    'terms': {'field': 'worker_name', 'size': 100},
                    'aggs': {
                        'input': {'sum': {'field': 'input_tokens'}},
                        'output': {'sum': {'field': 'output_tokens'}},
                        'role': {'terms': {'field': 'worker_role'}}
                    }
                }
            }
        })
        aggs = agg_res.get('aggregations', {})
        cost_in = float(os.environ.get('FLUME_COST_PER_1K_INPUT', '0.002'))
        cost_out = float(os.environ.get('FLUME_COST_PER_1K_OUTPUT', '0.010'))
        t_in = int(aggs.get('total_input_tokens', {}).get('value', 0))
        t_out = int(aggs.get('total_output_tokens', {}).get('value', 0))
        
        historical_burn = []
        for b in aggs.get('by_worker', {}).get('buckets', []):
            historical_burn.append({
                'worker_name': b['key'],
                'input_tokens': int(b.get('input', {}).get('value', 0)),
                'output_tokens': int(b.get('output', {}).get('value', 0)),
                'role': b.get('role', {}).get('buckets', [{'key': 'unknown'}])[0]['key'] if len(b.get('role', {}).get('buckets', [])) > 0 else 'unknown'
            })

        return {
            'savings': int(aggs.get('total_elastro_savings', {}).get('value', 0)),
            'baseline_tokens': int(aggs.get('total_baseline_tokens', {}).get('value', 0)),
            'baseline_full_context_tokens': int(aggs.get('total_baseline_full_context', {}).get('value', 0)),
            'actual_tokens_sent': int(aggs.get('total_actual_tokens', {}).get('value', 0)),
            'total_input_tokens': t_in,
            'total_output_tokens': t_out,
            'estimated_cost_usd': round((t_in / 1000.0 * cost_in) + (t_out / 1000.0 * cost_out), 4),
            'historical_burn': historical_burn,
        }
    except SAFE_EXCEPTIONS:
        logger.debug("api_snapshot: token savings computation failed (best-effort)", exc_info=True)
        return {
            'savings': 0,
            'baseline_tokens': 0,
            'baseline_full_context_tokens': 0,
            'actual_tokens_sent': 0,
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            'estimated_cost_usd': 0.0,
            'historical_burn': [],
        }

async def load_snapshot():
    global _SNAPSHOT_CACHE_DATA, _SNAPSHOT_CACHE_TIME
    now = time.time()
    if _SNAPSHOT_CACHE_DATA and (now - _SNAPSHOT_CACHE_TIME) < 2.0:
        return _SNAPSHOT_CACHE_DATA

    if not ES_API_KEY or ES_API_KEY == 'AUTO_GENERATED_BY_INSTALLER':
        pass

    f_tasks = async_es_search('agent-task-records', {
        'size': 300,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {
            'bool': {
                'must': [{'match_all': {}}],
                'must_not': [{'term': {'status': 'archived'}}],
            }
        },
    })
    f_blocked_tasks = async_es_search('agent-task-records', {
        'size': 500,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {
            'bool': {
                'must': [{'term': {'status': 'blocked'}}],
                'must_not': [{'term': {'status': 'archived'}}],
            }
        },
    })
    f_reviews = async_es_search('agent-review-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'match_all': {}}
    })
    f_failures = async_es_search('agent-failure-records', {
        'size': 100,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'match_all': {}}
    })
    f_provenance = async_es_search('agent-provenance-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'match_all': {}}
    })
    f_savings = _async_fetch_savings()
    f_workers = asyncio.to_thread(load_workers)
    f_projects = asyncio.to_thread(load_projects_registry)

    (
        tasks_res_raw,
        blocked_tasks_res_raw,
        reviews_res_raw,
        failures_res_raw,
        provenance_res_raw,
        token_metrics,
        workers_res,
        projects_res
    ) = await asyncio.gather(
        f_tasks, f_blocked_tasks, f_reviews, f_failures, f_provenance,
        f_savings, f_workers, f_projects
    )

    tasks_recent = tasks_res_raw.get('hits', {}).get('hits', [])
    tasks_blocked_extra = blocked_tasks_res_raw.get('hits', {}).get('hits', [])
    tasks_res = _merge_recent_task_hits_with_blocked(tasks_recent, tasks_blocked_extra)
    reviews_res = reviews_res_raw.get('hits', {}).get('hits', [])
    failures_res = failures_res_raw.get('hits', {}).get('hits', [])
    provenance_res = provenance_res_raw.get('hits', {}).get('hits', [])

    repos_res = await load_repos(registry=projects_res)

    result = {
        'workers': workers_res,
        'tasks': [_map_task_hit_for_api(h) for h in tasks_res],
        'reviews': [{'_id': h.get('_id'), **h.get('_source', {})} for h in reviews_res],
        'failures': [{'_id': h.get('_id'), **h.get('_source', {})} for h in failures_res],
        'provenance': [{'_id': h.get('_id'), **h.get('_source', {})} for h in provenance_res],
        'repos': repos_res,
        'projects': projects_res,
        'elastro_savings': token_metrics['savings'],
        'token_metrics': token_metrics,
    }
    
    _SNAPSHOT_CACHE_DATA = result
    _SNAPSHOT_CACHE_TIME = now
    return result
