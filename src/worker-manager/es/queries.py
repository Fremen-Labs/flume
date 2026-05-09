"""Reusable Elasticsearch query primitives for the Flume worker-manager.

Phase 7 Priority 9: Extracted from orchestration/sweeps.py and orchestration/claim.py.
Pure ES aggregation and count queries with no coupling to the orchestration
loop or claim logic. These are consumed by sweeps.py (promote_planned),
claim.py (_compute_saturated_scopes), and the cycle() pre-flight checks.

Functions:
    count_available_by_status — Pre-flight msearch counts (ready/review/planned)
    count_active_per_repo     — Aggregation: in-flight tasks per repo
    count_active_per_story    — Aggregation: in-flight tasks per story
    task_stale_seconds        — Timestamp staleness helper
"""
import json
from datetime import datetime, timezone
from typing import Optional

from config import TASK_INDEX
from es.client import es_request, es_request_raw
from utils.logger import get_logger

logger = get_logger('es.queries')


# ── Shared Query Fragment ────────────────────────────────────────────────────
# This bool query identifies "in-flight" leaf tasks (those whose branches are
# still live and unmerged). Used by both per-repo and per-story aggregations.

def _in_flight_leaf_query() -> dict:
    """Return the bool query filter for leaf tasks with in-flight branches.

    Includes running/review tasks and blocked tasks that still have a
    branch/commit_sha (awaiting rebase or reconciliation). Excludes
    rollup item types (epic/feature/story) and PM-owned tasks.
    """
    return {'bool': {
        'should': [
            {'terms': {'status': ['ready', 'running', 'review']}},
            {'bool': {
                'must': [
                    {'term': {'status': 'blocked'}},
                    {'bool': {'should': [
                        {'exists': {'field': 'branch'}},
                        {'exists': {'field': 'commit_sha'}},
                    ], 'minimum_should_match': 1}},
                ],
                'must_not': [{'term': {'pr_merged': True}}],
            }},
        ],
        'minimum_should_match': 1,
        'must_not': [
            {'terms': {'item_type': ['epic', 'feature', 'story']}},
            {'term': {'owner': 'pm'}},
            {'term': {'assigned_agent_role': 'pm'}},
        ],
    }}


# ── Staleness Helper ────────────────────────────────────────────────────────

def task_stale_seconds(src: dict) -> Optional[float]:
    """Seconds since updated_at or last_update, or None if not parseable."""
    for k in ('updated_at', 'last_update'):
        t = src.get(k)
        if not t:
            continue
        s = str(t).replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - parsed).total_seconds()
        except Exception:
            continue
    return None


# ── Aggregation Queries ─────────────────────────────────────────────────────

def count_active_per_repo() -> dict:
    """Return {repo_id: count} of leaf tasks with an in-flight branch.

    Includes ``blocked`` tasks that still have a branch/commit_sha: a task
    blocked on a merge conflict (awaiting pr_reconcile rebase) still owns an
    unmerged branch, and promoting another ready task on top of it is what
    produces the multi-branch conflict cascade.
    """
    try:
        res = es_request(
            f'/{TASK_INDEX}/_search',
            {
                'size': 0,
                'query': _in_flight_leaf_query(),
                'aggs': {'by_repo': {'terms': {'field': 'repo', 'size': 500}}},
            },
            method='POST',
        )
    except Exception:
        return {}
    out = {}
    for b in (res.get('aggregations', {}).get('by_repo', {}).get('buckets', []) or []):
        key = b.get('key')
        if key:
            out[key] = int(b.get('doc_count', 0) or 0)
    return out


def count_active_per_story() -> dict:
    """Return {parent_id: count} of leaf tasks with an in-flight branch.

    Mirrors ``count_active_per_repo`` — includes blocked-with-branch so a
    merge-conflict task still occupies its story's parallelism slot.
    """
    try:
        res = es_request(
            f'/{TASK_INDEX}/_search',
            {
                'size': 0,
                'query': _in_flight_leaf_query(),
                'aggs': {'by_parent': {'terms': {'field': 'parent_id.keyword', 'size': 1000, 'missing': ''}}},
            },
            method='POST',
        )
    except Exception:
        return {}
    out = {}
    for b in (res.get('aggregations', {}).get('by_parent', {}).get('buckets', []) or []):
        key = b.get('key')
        if key:
            out[key] = int(b.get('doc_count', 0) or 0)
    return out


# ── Pre-flight Availability Cache (Phase 1.1) ───────────────────────────────

def count_available_by_status() -> dict:
    """Return {status: count} for claimable task statuses in a single msearch.

    Uses ES _msearch to batch three count queries (ready, review, planned)
    into one HTTP roundtrip. Workers check this before attempting atomic claims.
    """
    counts = {'ready': 0, 'review': 0, 'planned': 0}
    try:
        lines = []
        for status in ('ready', 'review', 'planned'):
            lines.append(json.dumps({'index': TASK_INDEX}))
            lines.append(json.dumps({
                'size': 0,
                'query': {'term': {'status': status}},
                'track_total_hits': True,
            }))
        raw_body = '\n'.join(lines) + '\n'
        res = es_request_raw('/_msearch', raw_body, method='POST')
        responses = res.get('responses', [])
        for status, resp in zip(('ready', 'review', 'planned'), responses):
            total = resp.get('hits', {}).get('total', {})
            counts[status] = total.get('value', 0) if isinstance(total, dict) else int(total or 0)
    except Exception as e:
        # On failure, assume tasks exist so we don't accidentally skip claims
        logger.error(f"pre-flight count failed, assuming tasks available: {e}")
        counts = {'ready': 999, 'review': 999, 'planned': 999}
    return counts
