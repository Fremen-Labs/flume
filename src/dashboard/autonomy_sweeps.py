"""
Autonomy sweeps: background sweeps that keep work flowing without human intervention.

Runs on a single daemon thread. Each sweep has its own cadence and state so a
slow sweep never starves the others.

Sweeps shipped here:

  • parent_revival
        When a child task (typically a `bug-*`) transitions to `done`, find its
        parent (via `parent_id` / `origin_task_id`, or by parsing the child id
        prefix `bug-<parent>-<n>`). If the parent is `blocked` without
        `needs_human`, append an `[Auto-recovery]` note pointing at the resolved
        child and transition the parent to `ready`. Idempotent via a
        `parent_revived_at` marker stamped on the child doc.

  • stuck_worker_watchdog
        Find tasks in `queue_state=active` (i.e. claimed) whose `last_update`
        is older than `FLUME_STUCK_TASK_MINUTES` (default 25). Clear the claim,
        drop an `[Auto-recovery]` note describing the timeout, and re-queue as
        `status=ready`. Respects `needs_human` and caps per-task retries via
        `stuck_recovery_count`.

Configuration (env):

  FLUME_AUTONOMY_ENABLED             default "1"
  FLUME_AUTONOMY_INTERVAL_SEC        default 60   — loop tick; each sweep also
                                                    has its own cadence.
  FLUME_PARENT_REVIVAL_INTERVAL_SEC  default 90
  FLUME_PARENT_REVIVAL_LOOKBACK_MIN  default 180  — window for "recently-done"
                                                    child tasks to consider.
  FLUME_PARENT_REVIVAL_MAX           default 25   — children processed per tick
  FLUME_STUCK_TASK_MINUTES           default 25   — claim idle threshold
  FLUME_STUCK_TASK_INTERVAL_SEC      default 120
  FLUME_STUCK_TASK_MAX               default 25   — rows processed per tick
  FLUME_STUCK_TASK_RETRY_CAP         default 3    — per-task recoveries before
                                                    we flag needs_human=true

The module is deliberately self-contained: dashboard server.py passes the ES
helpers and logger on startup.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from agent_models_settings import AGENT_ROLE_IDS  # type: ignore[import-untyped]
from utils.exceptions import SAFE_EXCEPTIONS
from utils.logger import get_logger

logger = get_logger("autonomy_sweeps")

# ─── Constants ─────────────────────────────────────────────────────────────────

# ES index.
ES_TASK_INDEX = "agent-task-records"

# Task statuses.
STATUS_BLOCKED = "blocked"
STATUS_READY = "ready"
STATUS_DONE = "done"
STATUS_ARCHIVED = "archived"
STATUS_CANCELLED = "cancelled"
STATUS_RUNNING = "running"
STATUS_REVIEW = "review"
STATUS_PLANNED = "planned"

# Queue states.
QUEUE_STATE_QUEUED = "queued"
QUEUE_STATE_ACTIVE = "active"

# Default role when owner is unknown or invalid.
DEFAULT_ROLE = "implementer"

# Canonical set of valid agent roles — derived from agent_models_settings.
_VALID_ROLES = frozenset(AGENT_ROLE_IDS)

# Terminal statuses — tasks in these states are "finished".
_TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_ARCHIVED, STATUS_CANCELLED})

# Actionable statuses — tasks a worker can claim.
_ACTIONABLE_STATUSES = frozenset({STATUS_READY, STATUS_RUNNING, STATUS_REVIEW})

# Rollup item types that resolve when children do.
_ROLLUP_ITEM_TYPES = frozenset({"epic", "feature", "story"})

# Plan-check task ID prefix.
_PLAN_CHECK_PREFIX = "plan-check-"

# ─── Telemetry Event Constants ─────────────────────────────────────────────────

EVENT_STARTED = "autonomy_sweeps.started"

# parent_revival
EVENT_PARENT_REVIVED = "parent_revival.revived"
EVENT_PARENT_QUERY_FAILED = "parent_revival.query_failed"
EVENT_PARENT_LOOKUP_FAILED = "parent_revival.parent_lookup_failed"
EVENT_PARENT_PROCESS_FAILED = "parent_revival.process_failed"
EVENT_PARENT_SWEEP = "parent_revival.sweep"

# stuck_worker_watchdog
EVENT_STUCK_RECOVERED = "stuck_worker.recovered"
EVENT_STUCK_ESCALATED = "stuck_worker.escalated"
EVENT_STUCK_QUERY_FAILED = "stuck_worker.query_failed"
EVENT_STUCK_ESCALATE_FAILED = "stuck_worker.escalate_failed"
EVENT_STUCK_PROCESS_FAILED = "stuck_worker.process_failed"
EVENT_STUCK_SWEEP = "stuck_worker.sweep"

# plan_progress_scan
EVENT_PLAN_LIST_FAILED = "plan_progress.list_projects_failed"
EVENT_PLAN_QUERY_FAILED = "plan_progress.query_failed"
EVENT_PLAN_IDEMPOTENCY_FAILED = "plan_progress.idempotency_check_failed"
EVENT_PLAN_NUDGED = "plan_progress.nudged"
EVENT_PLAN_UPSERT_FAILED = "plan_progress.upsert_failed"
EVENT_PLAN_SWEEP = "plan_progress.sweep"

# branch_gc
EVENT_BRANCH_LIST_FAILED = "branch_gc.list_projects_failed"
EVENT_BRANCH_IMPORT_FAILED = "branch_gc.import_failed"
EVENT_BRANCH_QUERY_FAILED = "branch_gc.query_failed"
EVENT_BRANCH_CLIENT_FAILED = "branch_gc.client_failed"
EVENT_BRANCH_STAMP_FAILED = "branch_gc.stamp_failed"
EVENT_BRANCH_DELETE_FAILED = "branch_gc.delete_failed"
EVENT_BRANCH_DELETE_UNEXPECTED = "branch_gc.delete_unexpected"
EVENT_BRANCH_SWEEP = "branch_gc.sweep"

# pr_reconcile
EVENT_PR_LIST_FAILED = "pr_reconcile.list_projects_failed"
EVENT_PR_IMPORT_FAILED = "pr_reconcile.import_failed"
EVENT_PR_CLIENT_FAILED = "pr_reconcile.client_failed"
EVENT_PR_LIST_PRS_FAILED = "pr_reconcile.list_failed"
EVENT_PR_MERGED = "pr_reconcile.merged"
EVENT_PR_REBASED = "pr_reconcile.rebased"
EVENT_PR_MERGE_FAILED = "pr_reconcile.merge_failed"
EVENT_PR_BRANCH_DELETE_FAILED = "pr_reconcile.branch_delete_failed"
EVENT_PR_CLONE_FAILED = "pr_reconcile.clone_failed"
EVENT_PR_FETCH_FAILED = "pr_reconcile.fetch_failed"
EVENT_PR_CHECKOUT_FAILED = "pr_reconcile.checkout_failed"
EVENT_PR_PUSH_FAILED = "pr_reconcile.push_failed"
EVENT_PR_SYNCED = "pr_reconcile.synced"
EVENT_PR_SYNC_QUERY_FAILED = "pr_reconcile.sync_query_failed"
EVENT_PR_SYNC_UPDATE_FAILED = "pr_reconcile.sync_update_failed"
EVENT_PR_SWEEP = "pr_reconcile.sweep"

# orphan_heal
EVENT_ORPHAN_QUERY_FAILED = "orphan_heal.query_failed"
EVENT_ORPHAN_BLOCKED = "orphan_heal.blocked"
EVENT_ORPHAN_ROUTED = "orphan_heal.routed"
EVENT_ORPHAN_UPDATE_FAILED = "orphan_heal.update_failed"
EVENT_ORPHAN_SWEEP = "orphan_heal.sweep"


# ─── Pydantic Models ──────────────────────────────────────────────────────────


class AutonomySweepsConfig(BaseModel):
    """Immutable per-tick configuration — validated once, read everywhere."""

    enabled: bool = True
    loop_interval_sec: int = 60

    # parent_revival
    parent_revival_interval_sec: int = 90
    parent_revival_lookback_min: int = 180
    parent_revival_max: int = 25

    # stuck_worker_watchdog
    stuck_task_minutes: int = 25
    stuck_task_interval_sec: int = 120
    stuck_task_max: int = 25
    stuck_task_retry_cap: int = 3

    # plan_progress_scan
    plan_scan_interval_sec: int = 600
    plan_scan_cooldown_min: int = 60

    # branch_gc
    branch_gc_interval_sec: int = 180
    branch_gc_max_per_tick: int = 50

    # pr_reconcile
    pr_reconcile_interval_sec: int = 120
    pr_reconcile_max_per_tick: int = 20

    # orphan_heal
    orphan_heal_interval_sec: int = 30
    orphan_heal_max_per_tick: int = 50

    # Loop caps (mirrors worker_handlers.py defaults).
    max_reviewer_rework: int = 3
    max_tester_rejections: int = 3
    tester_retry_cap: int = 5
    reviewer_block_cap: int = 3
    max_implementer_exceptions: int = 3

    @classmethod
    def from_env(cls) -> "AutonomySweepsConfig":
        """Build config from environment variables with safe defaults."""
        return cls(
            enabled=_env_bool("FLUME_AUTONOMY_ENABLED", True),
            loop_interval_sec=_env_int("FLUME_AUTONOMY_INTERVAL_SEC", 60),
            parent_revival_interval_sec=_env_int("FLUME_PARENT_REVIVAL_INTERVAL_SEC", 90),
            parent_revival_lookback_min=_env_int("FLUME_PARENT_REVIVAL_LOOKBACK_MIN", 180),
            parent_revival_max=_env_int("FLUME_PARENT_REVIVAL_MAX", 25),
            stuck_task_minutes=_env_int("FLUME_STUCK_TASK_MINUTES", 25),
            stuck_task_interval_sec=_env_int("FLUME_STUCK_TASK_INTERVAL_SEC", 120),
            stuck_task_max=_env_int("FLUME_STUCK_TASK_MAX", 25),
            stuck_task_retry_cap=_env_int("FLUME_STUCK_TASK_RETRY_CAP", 3),
            plan_scan_interval_sec=_env_int("FLUME_PLAN_SCAN_INTERVAL_SEC", 600),
            plan_scan_cooldown_min=_env_int("FLUME_PLAN_SCAN_COOLDOWN_MIN", 60),
            branch_gc_interval_sec=_env_int("FLUME_BRANCH_GC_INTERVAL_SEC", 180),
            branch_gc_max_per_tick=_env_int("FLUME_BRANCH_GC_MAX_PER_TICK", 50),
            pr_reconcile_interval_sec=_env_int("FLUME_PR_RECONCILE_INTERVAL_SEC", 120),
            pr_reconcile_max_per_tick=_env_int("FLUME_PR_RECONCILE_MAX_PER_TICK", 20),
            orphan_heal_interval_sec=_env_int("FLUME_ORPHAN_HEAL_INTERVAL_SEC", 30),
            orphan_heal_max_per_tick=_env_int("FLUME_ORPHAN_HEAL_MAX_PER_TICK", 50),
            max_reviewer_rework=_env_int("FLUME_MAX_REVIEWER_REWORK", 3),
            max_tester_rejections=_env_int("FLUME_MAX_TESTER_REJECTIONS", 3),
            tester_retry_cap=_env_int("FLUME_TESTER_RETRY_CAP", 5),
            reviewer_block_cap=_env_int("FLUME_REVIEWER_BLOCK_CAP", 3),
            max_implementer_exceptions=_env_int("FLUME_MAX_IMPLEMENTER_EXCEPTIONS", 3),
        )


class SweepResult(BaseModel):
    """Shared return contract for all sweep functions."""

    scanned: int = 0
    errors: int = 0

    # Per-sweep action counts (unused fields stay 0).
    revived: int = 0
    recovered: int = 0
    escalated: int = 0
    nudged: int = 0
    deleted: int = 0
    merged: int = 0
    rebased: int = 0
    healed: int = 0
    blocked: int = 0
    skipped: int = 0
    conflicts_recorded: int = 0
    synced_to_merged: int = 0

    # Diagnostics.
    already_processed: int = 0
    parent_not_blocked: int = 0
    parent_not_found: int = 0
    skip_reasons: dict[str, int] = Field(default_factory=dict)
    action_ids: list[str] = Field(default_factory=list)
    actions: list[dict] = Field(default_factory=list)


# ─── Module State ──────────────────────────────────────────────────────────────

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "enabled": False,
    "thread_alive": False,
    "config": {},
    "sweeps": {
        "parent_revival": {
            "last_run_at": None,
            "last_duration_ms": None,
            "last_summary": None,
            "runs": 0,
            "revived_total": 0,
            "errors_total": 0,
            "last_error": None,
        },
        "stuck_worker_watchdog": {
            "last_run_at": None,
            "last_duration_ms": None,
            "last_summary": None,
            "runs": 0,
            "recovered_total": 0,
            "escalated_total": 0,
            "errors_total": 0,
            "last_error": None,
        },
        "plan_progress_scan": {
            "last_run_at": None,
            "last_duration_ms": None,
            "last_summary": None,
            "runs": 0,
            "nudged_total": 0,
            "errors_total": 0,
            "last_error": None,
        },
        "branch_gc": {
            "last_run_at": None,
            "last_duration_ms": None,
            "last_summary": None,
            "runs": 0,
            "deleted_total": 0,
            "errors_total": 0,
            "last_error": None,
        },
        "pr_reconcile": {
            "last_run_at": None,
            "last_duration_ms": None,
            "last_summary": None,
            "runs": 0,
            "merged_total": 0,
            "rebased_total": 0,
            "errors_total": 0,
            "last_error": None,
        },
        "orphan_heal": {
            "last_run_at": None,
            "last_duration_ms": None,
            "last_summary": None,
            "runs": 0,
            "healed_total": 0,
            "blocked_total": 0,
            "errors_total": 0,
            "last_error": None,
        },
    },
}


# ─── Utility Functions ─────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except SAFE_EXCEPTIONS:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _status_for_role(role: str | None) -> str:
    """Return the ``status`` value a given role is expected to claim from.

    Mirrors the claim logic in ``worker-manager/manager.py``:
      - pm picks up ``planned``
      - tester / reviewer pick up ``review``
      - everything else picks up ``ready``

    This is used by recovery sweeps to avoid creating unreachable task
    states (e.g. ``status=ready`` with ``owner=reviewer``, which no worker
    will ever claim).
    """
    r = (role or "").strip().lower()
    if r == "pm":
        return STATUS_PLANNED
    if r in ("tester", "reviewer"):
        return STATUS_REVIEW
    return STATUS_READY


def _normalize_requeue_doc(doc: dict, owner: str | None) -> None:
    """Populate ``doc`` with ``status`` / ``owner`` / ``assigned_agent_role``
    in a way the claim loop can actually pick up again.

    Writes *into* the supplied doc dict so callers don't have to restructure
    their existing update payload.  Preserves the owner when it is a known
    worker role; otherwise falls back to ``implementer`` (the default
    ready-queue role).
    """
    role = (owner or "").strip().lower() or DEFAULT_ROLE
    if role not in _VALID_ROLES:
        role = DEFAULT_ROLE
    doc["status"] = _status_for_role(role)
    doc["owner"] = role
    doc["assigned_agent_role"] = role


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts: str) -> float | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except SAFE_EXCEPTIONS:
        return None


_BUG_ID_RE = re.compile(r"^bug-(?P<parent>.+?)-\d+$")


def _infer_parent_id(child_src: dict) -> str | None:
    pid = (child_src.get("parent_id") or child_src.get("origin_task_id") or "").strip()
    if pid:
        return pid
    cid = (child_src.get("id") or "").strip()
    m = _BUG_ID_RE.match(cid)
    if m:
        return m.group("parent")
    return None


# ─── Sweep: parent_revival ─────────────────────────────────────────────────────


def _parent_revival_sweep(deps: dict, config: AutonomySweepsConfig) -> SweepResult:
    es_search = deps["es_search"]
    es_post = deps["es_post"]
    append_note = deps["append_note"]

    result = SweepResult()

    since_ts = time.time() - config.parent_revival_lookback_min * 60
    since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    # Query: recently-done children (bugs or tasks) with NO parent_revived_at stamp yet.
    body = {
        "size": config.parent_revival_max * 2,
        "sort": [{"updated_at": {"order": "desc"}}],
        "query": {
            "bool": {
                "must": [
                    {"term": {"status": STATUS_DONE}},
                    {"range": {"updated_at": {"gte": since_iso}}},
                ],
                "should": [
                    {"term": {"item_type": "bug"}},
                    {"prefix": {"id": "bug-"}},
                    {"exists": {"field": "parent_id"}},
                ],
                "minimum_should_match": 1,
                "must_not": [
                    {"exists": {"field": "parent_revived_at"}},
                ],
            }
        },
    }
    try:
        res = es_search(ES_TASK_INDEX, body)
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning(
            "Parent revival query failed",
            extra={"structured_data": {"event": EVENT_PARENT_QUERY_FAILED, "error": str(e)[:200]}},
        )
        return result

    hits = res.get("hits", {}).get("hits", []) or []
    result.scanned = len(hits)

    processed = 0
    for h in hits:
        if processed >= config.parent_revival_max:
            break
        child_id = h.get("_id")
        child_src = h.get("_source") or {}
        parent_logical_id = _infer_parent_id(child_src)
        if not parent_logical_id:
            # Stamp so we don't rescan this row forever.
            try:
                es_post(
                    f"{ES_TASK_INDEX}/_update/{child_id}",
                    {"doc": {"parent_revived_at": _now_iso(), "parent_revival_reason": "no_parent_id"}},
                )
            except SAFE_EXCEPTIONS:
                pass
            result.parent_not_found += 1
            processed += 1
            continue

        # Look up the parent doc.
        try:
            pres = es_search(ES_TASK_INDEX, {
                "size": 1,
                "query": {"bool": {"should": [
                    {"ids": {"values": [parent_logical_id]}},
                    {"term": {"id": parent_logical_id}},
                    {"term": {"id.keyword": parent_logical_id}},
                ], "minimum_should_match": 1}},
            })
            phits = pres.get("hits", {}).get("hits", [])
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning(
                "Parent lookup failed",
                extra={"structured_data": {
                    "event": EVENT_PARENT_LOOKUP_FAILED,
                    "child": child_src.get("id"),
                    "parent_id": parent_logical_id,
                    "error": str(e)[:200],
                }},
            )
            continue

        if not phits:
            try:
                es_post(
                    f"{ES_TASK_INDEX}/_update/{child_id}",
                    {"doc": {"parent_revived_at": _now_iso(), "parent_revival_reason": "parent_missing"}},
                )
            except SAFE_EXCEPTIONS:
                pass
            result.parent_not_found += 1
            processed += 1
            continue

        parent_hit = phits[0]
        parent_es_id = parent_hit.get("_id")
        parent_src = parent_hit.get("_source") or {}
        pstatus = (parent_src.get("status") or "").lower()
        pneeds_human = bool(parent_src.get("needs_human"))

        if pstatus != STATUS_BLOCKED or pneeds_human:
            # Stamp the child so we skip it on subsequent sweeps.
            try:
                es_post(
                    f"{ES_TASK_INDEX}/_update/{child_id}",
                    {"doc": {
                        "parent_revived_at": _now_iso(),
                        "parent_revival_reason": (
                            "parent_not_blocked" if pstatus != STATUS_BLOCKED else "parent_needs_human"
                        ),
                    }},
                )
            except SAFE_EXCEPTIONS:
                pass
            result.parent_not_blocked += 1
            processed += 1
            continue

        # Revive the parent.
        try:
            append_note(
                parent_es_id,
                (
                    f"[Auto-recovery] Child {child_src.get('id')} "
                    f"({child_src.get('item_type') or 'task'}) resolved "
                    f"\"{(child_src.get('title') or '')[:120]}\". Re-queuing parent; "
                    "verify the original failure is gone and re-run tests."
                ),
            )
            owner = parent_src.get("owner") or parent_src.get("assigned_agent_role")
            now = _now_iso()
            doc = {
                "status": STATUS_READY,
                "queue_state": QUEUE_STATE_QUEUED,
                "active_worker": None,
                "needs_human": False,
                "updated_at": now,
                "last_update": now,
                "implementer_consecutive_llm_failures": 0,
                "last_child_revived_from": child_src.get("id"),
            }
            if owner:
                doc["owner"] = owner
                doc["assigned_agent_role"] = owner
            es_post(f"{ES_TASK_INDEX}/_update/{parent_es_id}", {"doc": doc})
            es_post(
                f"{ES_TASK_INDEX}/_update/{child_id}",
                {"doc": {"parent_revived_at": _now_iso(), "parent_revival_reason": "revived"}},
            )
            result.revived += 1
            result.action_ids.append(parent_src.get("id"))
            processed += 1
            logger.info(
                "Parent revived",
                extra={"structured_data": {
                    "event": EVENT_PARENT_REVIVED,
                    "parent": parent_src.get("id"),
                    "child": child_src.get("id"),
                }},
            )
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning(
                "Parent revival processing failed",
                extra={"structured_data": {
                    "event": EVENT_PARENT_PROCESS_FAILED,
                    "parent": parent_src.get("id"),
                    "child": child_src.get("id"),
                    "error": str(e)[:300],
                }},
            )

    return result


# ---- sweep: stuck_worker_watchdog -------------------------------------------


def _stuck_worker_watchdog(deps: dict, config: AutonomySweepsConfig) -> SweepResult:
    es_search = deps["es_search"]
    es_post = deps["es_post"]
    append_note = deps["append_note"]

    result = SweepResult()
    cutoff_ts = time.time() - config.stuck_task_minutes * 60

    body = {
        "size": config.stuck_task_max * 3,
        "sort": [{"last_update": {"order": "asc", "missing": "_last"}}],
        "query": {
            "bool": {
                "must": [
                    {"exists": {"field": "active_worker"}},
                ],
                "should": [
                    {"term": {"queue_state": QUEUE_STATE_ACTIVE}},
                    {"term": {"status": STATUS_RUNNING}},
                    {"term": {"status": STATUS_REVIEW}},
                ],
                "minimum_should_match": 1,
                "must_not": [
                    {"term": {"needs_human": True}},
                    {"term": {"status": STATUS_DONE}},
                    {"term": {"status": STATUS_ARCHIVED}},
                    {"term": {"status": STATUS_CANCELLED}},
                    {"term": {"status": STATUS_BLOCKED}},
                ],
            }
        },
    }
    try:
        res = es_search(ES_TASK_INDEX, body)
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning(
            "Stuck worker query failed",
            extra={"structured_data": {"event": EVENT_STUCK_QUERY_FAILED, "error": str(e)[:200]}},
        )
        return result

    hits = res.get("hits", {}).get("hits", []) or []
    result.scanned = len(hits)

    processed = 0
    for h in hits:
        if processed >= config.stuck_task_max:
            break
        es_id = h.get("_id")
        src = h.get("_source") or {}
        last_raw = src.get("last_update") or src.get("updated_at") or src.get("created_at")
        last_ts = _parse_iso(last_raw) if last_raw else None
        if last_ts is None:
            # Treat missing timestamp as stale only if active_worker is set.
            if not src.get("active_worker"):
                continue
        else:
            if last_ts >= cutoff_ts:
                continue  # recent update — not stuck

        recovery_count = int(src.get("stuck_recovery_count") or 0)
        aw = src.get("active_worker")
        task_id = src.get("id")
        now = _now_iso()

        if recovery_count >= config.stuck_task_retry_cap:
            try:
                append_note(
                    es_id,
                    (
                        f"[Auto-recovery] Task stuck (no update for > {config.stuck_task_minutes} min on worker {aw}). "
                        f"Giving up after {recovery_count} recoveries — needs human review."
                    ),
                )
                es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": {
                    "needs_human": True,
                    "active_worker": None,
                    "queue_state": QUEUE_STATE_QUEUED,
                    "stuck_recovery_count": recovery_count,
                    "stuck_last_recovery_at": now,
                    "updated_at": now,
                    "last_update": now,
                }})
                result.escalated += 1
                result.action_ids.append(task_id)
                processed += 1
                logger.info(
                    "Stuck task escalated",
                    extra={"structured_data": {
                        "event": EVENT_STUCK_ESCALATED,
                        "task_id": task_id,
                        "idle_for_min": int((time.time() - (last_ts or cutoff_ts)) / 60),
                        "active_worker": aw,
                    }},
                )
            except SAFE_EXCEPTIONS as e:
                result.errors += 1
                logger.warning(
                    "Stuck task escalation failed",
                    extra={"structured_data": {
                        "event": EVENT_STUCK_ESCALATE_FAILED,
                        "task_id": task_id,
                        "error": str(e)[:200],
                    }},
                )
            continue

        try:
            idle_for = int((time.time() - (last_ts or cutoff_ts)) / 60)
            append_note(
                es_id,
                (
                    f"[Auto-recovery] Watchdog: no update for {idle_for} min on worker {aw}. "
                    "Releasing claim and re-queueing; the next implementer should continue from here."
                ),
            )
            owner = src.get("owner") or src.get("assigned_agent_role")
            doc = {
                "queue_state": QUEUE_STATE_QUEUED,
                "active_worker": None,
                "needs_human": False,
                "stuck_recovery_count": recovery_count + 1,
                "stuck_last_recovery_at": now,
                "updated_at": now,
                "last_update": now,
                "implementer_consecutive_llm_failures": 0,
            }
            # Critical: a tester/reviewer-owned task must go to `review`, not
            # `ready`, or no worker will ever claim it. Previously we set
            # status=ready regardless of owner, which silently orphaned tasks.
            _normalize_requeue_doc(doc, owner)
            es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": doc})
            result.recovered += 1
            result.action_ids.append(task_id)
            processed += 1
            logger.info(
                "Stuck task recovered",
                extra={"structured_data": {
                    "event": EVENT_STUCK_RECOVERED,
                    "task_id": task_id,
                    "idle_for_min": idle_for,
                    "active_worker": aw,
                    "attempts": recovery_count + 1,
                }},
            )
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning(
                "Stuck task processing failed",
                extra={"structured_data": {
                    "event": EVENT_STUCK_PROCESS_FAILED,
                    "task_id": task_id,
                    "error": str(e)[:300],
                }},
            )

    return result


# ─── Sweep: plan_progress_scan ─────────────────────────────────────────────────


def _plan_progress_scan(deps: dict, config: AutonomySweepsConfig) -> SweepResult:
    """Detect projects where the queue has drained while the plan is still
    incomplete, and emit a ``plan-check-*`` task so the PM dispatcher can decide
    whether to spawn follow-up stories, close out the plan, or escalate.
    """
    es_search = deps["es_search"]
    es_upsert = deps["es_upsert"]
    list_projects = deps.get("list_projects")
    if list_projects is None:
        return SweepResult()

    cooldown_ts = time.time() - config.plan_scan_cooldown_min * 60
    cooldown_iso = datetime.fromtimestamp(cooldown_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    result = SweepResult()

    try:
        projects = list_projects() or []
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning("Plan progress list_projects failed",
                        extra={"structured_data": {"event": EVENT_PLAN_LIST_FAILED, "error": str(e)[:200]}})
        return result

    for proj in projects:
        repo_id = proj.get("id") if isinstance(proj, dict) else None
        if not repo_id:
            continue
        result.scanned += 1
        try:
            res = es_search(ES_TASK_INDEX, {
                "size": 500,
                "query": {"bool": {
                    "must": [{"term": {"repo": repo_id}}],
                    "must_not": [{"term": {"status": STATUS_ARCHIVED}}],
                }},
                "_source": ["id", "item_type", "status", "needs_human", "updated_at"],
            })
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning("Plan progress query failed",
                            extra={"structured_data": {"event": EVENT_PLAN_QUERY_FAILED, "repo": repo_id, "error": str(e)[:200]}})
            continue

        hits = res.get("hits", {}).get("hits", []) or []
        docs = [h.get("_source") or {} for h in hits]
        if not docs:
            result.skipped += 1
            result.skip_reasons["empty_project"] = result.skip_reasons.get("empty_project", 0) + 1
            continue

        counts = {"actionable": 0, "done": 0, "open_rollup": 0, "blocked_auto": 0, "blocked_human": 0}
        for d in docs:
            st = (d.get("status") or "").lower()
            it = (d.get("item_type") or "").lower()
            if st in _ACTIONABLE_STATUSES:
                counts["actionable"] += 1
            if st == STATUS_DONE:
                counts["done"] += 1
            if st == STATUS_BLOCKED:
                if d.get("needs_human"):
                    counts["blocked_human"] += 1
                else:
                    counts["blocked_auto"] += 1
            if it in _ROLLUP_ITEM_TYPES and st not in _TERMINAL_STATUSES:
                counts["open_rollup"] += 1

        stalled = counts["actionable"] == 0 and counts["open_rollup"] > 0 and counts["done"] > 0
        if not stalled:
            result.skipped += 1
            reason = (
                "plan_complete" if counts["open_rollup"] == 0
                else ("never_started" if counts["done"] == 0 else "has_actionable")
            )
            result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
            continue

        # Idempotency: skip if there's an open plan-check task for this repo,
        # or if we nudged within the cooldown window.
        try:
            pres = es_search(ES_TASK_INDEX, {
                "size": 1,
                "query": {"bool": {
                    "must": [
                        {"term": {"repo": repo_id}},
                        {"prefix": {"id": _PLAN_CHECK_PREFIX}},
                    ],
                    "should": [
                        {"bool": {"must_not": [{"term": {"status": STATUS_DONE}}]}},
                        {"range": {"updated_at": {"gte": cooldown_iso}}},
                    ],
                    "minimum_should_match": 1,
                }},
            })
            if pres.get("hits", {}).get("hits") or []:
                result.skipped += 1
                result.skip_reasons["recent_plan_check"] = result.skip_reasons.get("recent_plan_check", 0) + 1
                continue
        except SAFE_EXCEPTIONS as e:
            logger.warning("Plan progress idempotency check failed",
                            extra={"structured_data": {"event": EVENT_PLAN_IDEMPOTENCY_FAILED, "repo": repo_id, "error": str(e)[:200]}})
            # Proceed — worst case we create a duplicate, which is recoverable.

        plan_check_id = f"{_PLAN_CHECK_PREFIX}{repo_id}-{int(time.time())}"
        now = _now_iso()
        objective = (
            "The work queue has drained but the plan still has open epics/features/stories. "
            f"Project: {proj.get('name') or repo_id}. "
            f"Open rollup items: {counts['open_rollup']}, done tasks: {counts['done']}, "
            f"blocked (auto): {counts['blocked_auto']}, blocked (human): {counts['blocked_human']}.\n\n"
            "Inspect the project's acceptance criteria vs. merged commits and either:\n"
            "  1. Close out the remaining rollups if the work is actually complete, or\n"
            "  2. Emit concrete follow-up task(s) that will close the remaining gaps.\n"
            "\nCall implementation_complete with a clear summary of your decision."
        )
        doc = {
            "id": plan_check_id,
            "title": f"Plan health check — {proj.get('name') or repo_id}",
            "objective": objective,
            "repo": repo_id,
            "item_type": "task",
            "owner": "pm-dispatcher",
            "assigned_agent_role": "pm-dispatcher",
            "status": STATUS_READY,
            "priority": "high",
            "depends_on": [],
            "acceptance_criteria": [],
            "artifacts": [],
            "needs_human": False,
            "risk": "low",
            "requires_code": False,
            "plan_check_signal": counts,
            "created_at": now,
            "updated_at": now,
            "last_update": now,
        }
        try:
            es_upsert(ES_TASK_INDEX, plan_check_id, doc)
            result.nudged += 1
            result.action_ids.append(plan_check_id)
            logger.info("Plan progress nudged",
                         extra={"structured_data": {"event": EVENT_PLAN_NUDGED, "repo": repo_id, "plan_check_id": plan_check_id, "counts": counts}})
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning("Plan progress upsert failed",
                            extra={"structured_data": {"event": EVENT_PLAN_UPSERT_FAILED, "repo": repo_id, "error": str(e)[:300]}})

    return result


# ─── Sweep: branch_gc ──────────────────────────────────────────────────────────


_BRANCH_GC_PROTECTED_PR_STATUSES = frozenset({"open", "merged", "merging", "draft"})
_BRANCH_GC_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})


def _branch_gc_is_shared_branch(branch: str) -> bool:
    """Shared story-scoped branches may be referenced by sibling tasks — keep them."""
    if not branch:
        return False
    return branch.startswith("feature/story-") or branch.startswith("bugfix/story-")


def _branch_gc_sweep(deps: dict, config: AutonomySweepsConfig) -> SweepResult:
    """Reap remote branches belonging to terminal tasks with no open PR."""
    es_search = deps["es_search"]
    es_post = deps["es_post"]
    list_projects = deps.get("list_projects")
    if list_projects is None:
        return SweepResult()

    result = SweepResult()

    try:
        projects = list_projects() or []
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning("Branch GC list_projects failed",
                        extra={"structured_data": {"event": EVENT_BRANCH_LIST_FAILED, "error": str(e)[:200]}})
        return result

    try:
        from utils.git_host_client import (  # noqa: PLC0415
            get_git_client, GitHostError, GitHostNotFoundError,
        )
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning("Branch GC import failed",
                        extra={"structured_data": {"event": EVENT_BRANCH_IMPORT_FAILED, "error": str(e)[:200]}})
        return result

    for proj in projects:
        if result.deleted >= config.branch_gc_max_per_tick:
            break
        if not isinstance(proj, dict):
            continue
        repo_id = proj.get("id")
        if not repo_id:
            continue
        repo_url = (proj.get("repoUrl") or proj.get("repo_url") or "").strip()
        if not repo_url:
            result.skipped += 1
            result.skip_reasons["no_repo_url"] = result.skip_reasons.get("no_repo_url", 0) + 1
            continue

        try:
            res = es_search(ES_TASK_INDEX, {
                "size": 200,
                "query": {"bool": {
                    "must": [
                        {"term": {"repo": repo_id}},
                        {"exists": {"field": "branch"}},
                        {"terms": {"status": sorted(_TERMINAL_STATUSES)}},
                    ],
                    "must_not": [
                        {"term": {"remote_branch_deleted": True}},
                    ],
                }},
                "_source": ["id", "branch", "status", "pr_url", "pr_status", "item_type", "owner"],
            })
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning("Branch GC query failed",
                            extra={"structured_data": {"event": EVENT_BRANCH_QUERY_FAILED, "repo": repo_id, "error": str(e)[:200]}})
            continue

        hits = res.get("hits", {}).get("hits", []) or []
        if not hits:
            continue

        client = None
        try:
            client = get_git_client(proj)
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning("Branch GC client failed",
                            extra={"structured_data": {"event": EVENT_BRANCH_CLIENT_FAILED, "repo": repo_id, "error": str(e)[:200]}})
            continue

        for h in hits:
            if result.deleted >= config.branch_gc_max_per_tick:
                break
            es_id = h.get("_id")
            src = h.get("_source") or {}
            result.scanned += 1
            branch = str(src.get("branch") or "").strip()
            if not branch or branch in _BRANCH_GC_PROTECTED_BRANCHES:
                result.skipped += 1
                result.skip_reasons["protected"] = result.skip_reasons.get("protected", 0) + 1
                continue
            if _branch_gc_is_shared_branch(branch):
                result.skipped += 1
                result.skip_reasons["shared_scope"] = result.skip_reasons.get("shared_scope", 0) + 1
                continue
            pr_status = str(src.get("pr_status") or "").lower()
            if pr_status in _BRANCH_GC_PROTECTED_PR_STATUSES:
                result.skipped += 1
                result.skip_reasons["pr_active"] = result.skip_reasons.get("pr_active", 0) + 1
                continue

            try:
                client.delete_remote_branch(branch)
                result.deleted += 1
                result.actions.append({"repo": repo_id, "branch": branch, "task": src.get("id")})
                try:
                    es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {
                        "doc": {
                            "remote_branch_deleted": True,
                            "remote_branch_deleted_at": _now_iso(),
                            "remote_branch_deleted_reason": "branch_gc_sweep",
                        }
                    })
                except SAFE_EXCEPTIONS as e:
                    logger.warning("Branch GC stamp failed",
                                    extra={"structured_data": {"event": EVENT_BRANCH_STAMP_FAILED, "task": src.get("id"), "error": str(e)[:200]}})
            except GitHostNotFoundError:
                # Branch already gone — still stamp so we don't rescan it.
                try:
                    es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {
                        "doc": {
                            "remote_branch_deleted": True,
                            "remote_branch_deleted_at": _now_iso(),
                            "remote_branch_deleted_reason": "already_absent",
                        }
                    })
                except SAFE_EXCEPTIONS:
                    pass
                result.skipped += 1
                result.skip_reasons["already_absent"] = result.skip_reasons.get("already_absent", 0) + 1
            except GitHostError as e:
                result.errors += 1
                logger.warning("Branch GC delete failed",
                                extra={"structured_data": {"event": EVENT_BRANCH_DELETE_FAILED, "repo": repo_id, "branch": branch, "error": str(e)[:200]}})
            except SAFE_EXCEPTIONS as e:
                result.errors += 1
                logger.warning("Branch GC delete unexpected",
                                extra={"structured_data": {"event": EVENT_BRANCH_DELETE_UNEXPECTED, "repo": repo_id, "branch": branch, "error": str(e)[:200]}})

    return result


# ─── Sweep: pr_reconcile ───────────────────────────────────────────────────────


_PR_RECONCILE_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})


def _pr_reconcile_find_task_by_pr(es_search, repo_id: str, pr_number: int) -> tuple[str | None, dict]:
    """Locate the task doc that owns this PR number in the given repo."""
    try:
        res = es_search(ES_TASK_INDEX, {
            "size": 1,
            "query": {"bool": {"must": [
                {"term": {"repo": repo_id}},
                {"term": {"pr_number": int(pr_number)}},
            ]}},
        })
    except SAFE_EXCEPTIONS:
        return None, {}
    hits = res.get("hits", {}).get("hits") or []
    if not hits:
        return None, {}
    return hits[0].get("_id"), (hits[0].get("_source") or {})


def _pr_reconcile_attempt_rebase(
    repo_id: str,
    proj: dict,
    feature_branch: str,
    base_branch: str,
) -> tuple[bool, str, list[str]]:
    """
    Shallow-clone the repo, merge origin/<base_branch> into <feature_branch>,
    and push. Returns (pushed, reason, conflicting_files).

    reason ∈ {
      'pushed'              # fast-forward merge into feature branch -> push succeeded
      'already_up_to_date'  # nothing to merge, develop hasn't moved
      'merge_conflict'      # real conflicts; files returned
      'clone_failed'        # couldn't clone the repo
      'push_failed'         # merged locally but push rejected
      'no_credentials'      # couldn't obtain a token
      'unknown'             # catchall
    }
    """
    import asyncio     # noqa: PLC0415
    import tempfile    # noqa: PLC0415
    import shutil      # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    from utils.async_subprocess import run_cmd_async  # noqa: PLC0415

    repo_url = (proj.get('repoUrl') or proj.get('repo_url') or '').strip()
    if not repo_url:
        return False, 'no_credentials', []

    try:
        from utils.git_host_client import _get_github_token  # type: ignore  # noqa: PLC0415
    except SAFE_EXCEPTIONS:
        _get_github_token = None

    auth_url = repo_url
    try:
        if _get_github_token and 'github.com' in repo_url:
            token = _get_github_token() or ''
            if token and '://' in repo_url:
                prefix, rest = repo_url.split('://', 1)
                auth_url = f'{prefix}://x-access-token:{token}@{rest}'
    except SAFE_EXCEPTIONS:
        pass

    tmp = _Path(tempfile.mkdtemp(prefix=f'flume-reconcile-{repo_id}-'))
    try:
        # --no-single-branch so subsequent fetches can create remote-tracking
        # refs for the integration and feature branches. --depth alone sets
        # single-branch mode in modern git which breaks `origin/<branch>`.
        rc, out, err = asyncio.run(run_cmd_async(
            'git', 'clone', '--no-tags', '--no-single-branch',
            '--depth=200', '--', auth_url, str(tmp),
            timeout=300
        ))
        if rc != 0:
            logger.warning("PR reconcile clone failed",
                            extra={"structured_data": {"event": EVENT_PR_CLONE_FAILED, "repo": repo_id, "error": (err or out)[:300]}})
            return False, "clone_failed", []

        # Minimal identity so merge commits have an author.
        asyncio.run(run_cmd_async('git', '-C', str(tmp), 'config', 'user.email', 'flume-bot@local', timeout=30))
        asyncio.run(run_cmd_async('git', '-C', str(tmp), 'config', 'user.name', 'Flume Reconciliation Bot', timeout=30))

        # Explicit refspec so the fetches force-update remote-tracking refs
        # even when the initial clone didn't set up tracking for every branch.
        rc, out, err = asyncio.run(run_cmd_async(
            'git', '-C', str(tmp), 'fetch', '--depth=200', 'origin',
            f'+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}',
            f'+refs/heads/{feature_branch}:refs/remotes/origin/{feature_branch}',
            timeout=120
        ))
        if rc != 0:
            logger.warning("PR reconcile fetch failed",
                            extra={"structured_data": {"event": EVENT_PR_FETCH_FAILED, "repo": repo_id, "feature": feature_branch, "base": base_branch, "error": (err or out)[:300]}})
            return False, "clone_failed", []

        rc, out, err = asyncio.run(run_cmd_async(
            'git', '-C', str(tmp), 'checkout', '-B', feature_branch, f'refs/remotes/origin/{feature_branch}',
            timeout=60
        ))
        if rc != 0:
            logger.warning("PR reconcile checkout failed",
                            extra={"structured_data": {"event": EVENT_PR_CHECKOUT_FAILED, "repo": repo_id, "feature": feature_branch, "error": (err or out)[:300]}})
            return False, "clone_failed", []

        # Already up to date?
        rc, ahead_behind, err = asyncio.run(run_cmd_async(
            'git', '-C', str(tmp), 'rev-list', '--left-right', '--count',
            f'refs/remotes/origin/{base_branch}...{feature_branch}',
            timeout=30
        ))
        try:
            parts = (ahead_behind or '').split()
            base_ahead = int(parts[0]) if parts else 0
        except SAFE_EXCEPTIONS:
            base_ahead = 1
        if base_ahead == 0:
            return False, 'already_up_to_date', []

        rc, out, err = asyncio.run(run_cmd_async(
            'git', '-C', str(tmp), 'merge', '--no-edit',
            '-m', f'chore: merge {base_branch} into {feature_branch} (Flume auto-reconcile)',
            f'refs/remotes/origin/{base_branch}',
            timeout=120
        ))
        if rc != 0:
            # Collect the conflicting file list before aborting.
            rc2, unmerged, err2 = asyncio.run(run_cmd_async(
                'git', '-C', str(tmp), 'diff', '--name-only', '--diff-filter=U',
                timeout=30
            ))
            files = [f.strip() for f in (unmerged or '').splitlines() if f.strip()]
            asyncio.run(run_cmd_async('git', '-C', str(tmp), 'merge', '--abort', timeout=30))
            return False, 'merge_conflict', files[:15]

        rc, out, err = asyncio.run(run_cmd_async(
            'git', '-C', str(tmp), 'push', 'origin', feature_branch,
            timeout=120
        ))
        if rc != 0:
            logger.warning("PR reconcile push failed",
                            extra={"structured_data": {"event": EVENT_PR_PUSH_FAILED, "repo": repo_id, "feature": feature_branch, "error": (err or out)[:300]}})
            return False, "push_failed", []

        return True, 'pushed', []
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except SAFE_EXCEPTIONS:
            pass


def _pr_reconcile_sweep(deps: dict, config: AutonomySweepsConfig) -> SweepResult:
    """Reconcile GitHub PR state with Flume task docs."""
    es_search = deps["es_search"]
    es_post = deps["es_post"]
    list_projects = deps.get("list_projects")
    if list_projects is None:
        return SweepResult()

    result = SweepResult()

    try:
        projects = list_projects() or []
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning("PR reconcile list_projects failed",
                        extra={"structured_data": {"event": EVENT_PR_LIST_FAILED, "error": str(e)[:200]}})
        return result

    try:
        from utils.git_host_client import (  # noqa: PLC0415
            get_git_client, GitHostError, GitHostNotFoundError,
        )
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning("PR reconcile import failed",
                        extra={"structured_data": {"event": EVENT_PR_IMPORT_FAILED, "error": str(e)[:200]}})
        return result

    processed = 0
    for proj in projects:
        if processed >= config.pr_reconcile_max_per_tick:
            break
        if not isinstance(proj, dict):
            continue
        repo_id = proj.get("id")
        repo_url = (proj.get("repoUrl") or proj.get("repo_url") or "").strip()
        if not repo_id or not repo_url:
            continue

        gitflow = proj.get("gitflow") or {}
        base_branch = (gitflow.get("integrationBranch") or "develop").strip() or "develop"

        try:
            client = get_git_client(proj)
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning("PR reconcile client failed",
                            extra={"structured_data": {"event": EVENT_PR_CLIENT_FAILED, "repo": repo_id, "error": str(e)[:200]}})
            continue

        try:
            pulls = client.list_pull_requests(state="open", base=base_branch, per_page=50) or []
        except GitHostError as e:
            result.errors += 1
            logger.warning("PR reconcile list PRs failed",
                            extra={"structured_data": {"event": EVENT_PR_LIST_PRS_FAILED, "repo": repo_id, "error": str(e)[:200]}})
            continue

        for pr in pulls:
            if processed >= config.pr_reconcile_max_per_tick:
                break
            processed += 1
            result.scanned += 1
            pr_number = pr.get("number")
            if not pr_number:
                continue
            head_ref = ((pr.get("head") or {}).get("ref") or "").strip()
            if not head_ref or head_ref in _PR_RECONCILE_PROTECTED_BRANCHES:
                continue

            mergeable = pr.get("mergeable")
            state = (pr.get("mergeable_state") or "").lower()
            if mergeable is None or state in ("", "unknown"):
                try:
                    fresh = client.get_pull_request(int(pr_number))
                    mergeable = fresh.get("mergeable")
                    state = (fresh.get("mergeable_state") or "").lower()
                except SAFE_EXCEPTIONS:
                    pass

            task_es_id, task_src = _pr_reconcile_find_task_by_pr(es_search, repo_id, int(pr_number))

            if state == "clean" and mergeable:
                try:
                    client.merge_pull_request(int(pr_number))
                    result.merged += 1
                    result.actions.append({"repo": repo_id, "pr": pr_number, "action": "merged", "branch": head_ref})
                    logger.info("PR merged",
                                 extra={"structured_data": {"event": EVENT_PR_MERGED, "repo": repo_id, "pr": pr_number, "branch": head_ref}})
                    try:
                        client.delete_remote_branch(head_ref)
                    except GitHostNotFoundError:
                        pass
                    except SAFE_EXCEPTIONS as e:
                        logger.warning("PR branch delete failed",
                                        extra={"structured_data": {"event": EVENT_PR_BRANCH_DELETE_FAILED, "repo": repo_id, "pr": pr_number, "branch": head_ref, "error": str(e)[:200]}})
                    if task_es_id:
                        try:
                            es_post(f"{ES_TASK_INDEX}/_update/{task_es_id}", {
                                "doc": {"pr_status": "merged", "remote_branch_deleted": True, "remote_branch_deleted_at": _now_iso(), "merge_conflict": False}
                            })
                        except SAFE_EXCEPTIONS:
                            pass
                except GitHostError as e:
                    err_msg = str(e).lower()
                    if "already merged" in err_msg:
                        if task_es_id:
                            try:
                                es_post(f"{ES_TASK_INDEX}/_update/{task_es_id}", {"doc": {"pr_status": "merged"}})
                            except SAFE_EXCEPTIONS:
                                pass
                        result.synced_to_merged += 1
                    else:
                        result.errors += 1
                        logger.warning("PR merge failed",
                                        extra={"structured_data": {"event": EVENT_PR_MERGE_FAILED, "repo": repo_id, "pr": pr_number, "error": str(e)[:200]}})
                continue

            if state == "dirty" or (mergeable is False and state != "blocked"):
                rebased, reason, files = _pr_reconcile_attempt_rebase(
                    repo_id, proj, head_ref, base_branch,
                )
                if rebased:
                    result.rebased += 1
                    result.actions.append({"repo": repo_id, "pr": pr_number, "action": "rebased", "branch": head_ref, "base": base_branch})
                    logger.info("PR rebased",
                                 extra={"structured_data": {"event": EVENT_PR_REBASED, "repo": repo_id, "pr": pr_number, "branch": head_ref, "base": base_branch}})
                    if task_es_id:
                        try:
                            es_post(f"{ES_TASK_INDEX}/_update/{task_es_id}", {
                                "doc": {"pr_status": "awaiting_integration_merge", "merge_conflict": False}
                            })
                        except SAFE_EXCEPTIONS:
                            pass
                    continue

                if reason == "merge_conflict":
                    result.conflicts_recorded += 1
                    result.actions.append({"repo": repo_id, "pr": pr_number, "action": "conflict", "branch": head_ref, "files": files})
                    if task_es_id:
                        try:
                            es_post(f"{ES_TASK_INDEX}/_update/{task_es_id}", {
                                "doc": {
                                    "pr_status": "conflict", "merge_conflict": True,
                                    "merge_conflict_pr_number": int(pr_number),
                                    "merge_conflict_pr_url": pr.get("html_url"),
                                    "merge_conflict_head_branch": head_ref,
                                    "merge_conflict_base_branch": base_branch,
                                    "merge_conflict_files_preview": files,
                                    "merge_conflict_last_error": f"auto-rebase aborted on files: {files[:5]}",
                                    "status": STATUS_BLOCKED, "needs_human": False,
                                }
                            })
                        except SAFE_EXCEPTIONS:
                            pass
                else:
                    result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
                continue

            result.skip_reasons[state or "unknown"] = result.skip_reasons.get(state or "unknown", 0) + 1

    # Phase 3: sync task docs whose pr_status=open refers to a PR that is
    # actually merged/closed on GitHub.
    try:
        sync_res = es_search(ES_TASK_INDEX, {
            "size": 200,
            "query": {"bool": {"must": [
                {"term": {"pr_status": "open"}},
                {"exists": {"field": "pr_number"}},
            ]}},
            "_source": ["id", "repo", "pr_number", "branch"],
        })
        sync_hits = sync_res.get("hits", {}).get("hits") or []
    except SAFE_EXCEPTIONS as e:
        sync_hits = []
        logger.warning("PR reconcile sync query failed",
                        extra={"structured_data": {"event": EVENT_PR_SYNC_QUERY_FAILED, "error": str(e)[:200]}})

    proj_cache: dict[str, Any] = {}
    for h in sync_hits:
        es_id = h.get("_id")
        src = h.get("_source") or {}
        repo_id = src.get("repo")
        pr_number = src.get("pr_number")
        if not repo_id or not pr_number:
            continue
        if repo_id not in proj_cache:
            proj_cache[repo_id] = next(
                (p for p in projects if isinstance(p, dict) and p.get("id") == repo_id), None,
            )
        proj = proj_cache.get(repo_id)
        if not proj:
            continue
        try:
            client = get_git_client(proj)
            fresh = client.get_pull_request(int(pr_number))
        except GitHostNotFoundError:
            continue
        except SAFE_EXCEPTIONS:
            continue
        state = (fresh.get("state") or "").lower()
        merged = bool(fresh.get("merged"))
        if state == "closed":
            new_status = "merged" if merged else "closed"
            try:
                es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": {"pr_status": new_status, "merge_conflict": False}})
                result.synced_to_merged += 1
                logger.info("PR synced",
                             extra={"structured_data": {"event": EVENT_PR_SYNCED, "task": src.get("id"), "pr": pr_number, "new_status": new_status}})
            except SAFE_EXCEPTIONS as e:
                logger.warning("PR sync update failed",
                                extra={"structured_data": {"event": EVENT_PR_SYNC_UPDATE_FAILED, "task": src.get("id"), "error": str(e)[:200]}})

    return result


def _record_summary(sweep_name: str, result: SweepResult, duration_ms: int) -> None:
    key_map = {
        "parent_revival": ("revived_total", "revived", None),
        "stuck_worker_watchdog": ("recovered_total", "recovered", "escalated"),
        "plan_progress_scan": ("nudged_total", "nudged", None),
        "branch_gc": ("deleted_total", "deleted", None),
        "pr_reconcile": ("merged_total", "merged", "rebased"),
        "orphan_heal": ("healed_total", "healed", "blocked"),
    }
    summary = result.model_dump() if isinstance(result, SweepResult) else result
    with _STATE_LOCK:
        st = _STATE["sweeps"].get(sweep_name)
        if st is None:
            return
        st["last_run_at"] = _now_iso()
        st["last_duration_ms"] = duration_ms
        st["last_summary"] = summary
        st["runs"] += 1
        st["errors_total"] += int(summary.get("errors", 0))
        totals_key, primary_key, secondary_key = key_map.get(sweep_name, (None, None, None))
        if totals_key:
            st[totals_key] += int(summary.get(primary_key, 0))
        if secondary_key:
            st[f"{secondary_key}_total"] += int(summary.get(secondary_key, 0))


def _orphan_heal_sweep(deps: dict, config: AutonomySweepsConfig) -> SweepResult:
    """Normalize tasks stuck in unreachable (status, owner) combinations."""
    es_search = deps["es_search"]
    es_post = deps["es_post"]
    append_note = deps.get("append_note") or (lambda _a, _b: True)

    result = SweepResult()

    body = {
        "size": config.orphan_heal_max_per_tick * 2,
        "query": {
            "bool": {
                "must_not": [
                    {"term": {"status": STATUS_ARCHIVED}},
                    {"term": {"status": STATUS_DONE}},
                    {"term": {"status": STATUS_CANCELLED}},
                    {"term": {"status": STATUS_BLOCKED}},
                ],
                "should": [
                    {"bool": {"must": [
                        {"term": {"status": STATUS_READY}},
                        {"terms": {"owner": ["reviewer", "tester", "pm"]}},
                    ]}},
                    {"bool": {"must": [
                        {"term": {"status": STATUS_READY}},
                        {"terms": {"assigned_agent_role": ["reviewer", "tester", "pm"]}},
                    ]}},
                    {"bool": {"must": [
                        {"term": {"status": STATUS_REVIEW}},
                        {"terms": {"owner": ["implementer", "pm"]}},
                    ]}},
                    {"bool": {"must": [
                        {"term": {"status": STATUS_PLANNED}},
                        {"terms": {"owner": ["implementer", "tester", "reviewer"]}},
                    ]}},
                ],
                "minimum_should_match": 1,
            }
        },
    }

    try:
        res = es_search(ES_TASK_INDEX, body)
    except SAFE_EXCEPTIONS as e:
        result.errors += 1
        logger.warning("Orphan heal query failed",
                        extra={"structured_data": {"event": EVENT_ORPHAN_QUERY_FAILED, "error": str(e)[:200]}})
        return result

    hits = (res.get("hits") or {}).get("hits") or []
    result.scanned = len(hits)
    now = _now_iso()

    # Loop cap thresholds (mirrors worker_handlers.py defaults).
    max_rework = config.max_reviewer_rework
    max_reject = config.max_tester_rejections
    max_tester_retry = config.tester_retry_cap
    max_reviewer_block = config.reviewer_block_cap
    max_impl_exc = config.max_implementer_exceptions

    processed = 0
    for h in hits:
        if processed >= config.orphan_heal_max_per_tick:
            break
        es_id = h.get("_id")
        src = h.get("_source") or {}
        task_id = src.get("id") or es_id

        rework = int(src.get("reviewer_rework_count") or 0)
        reject = int(src.get("tester_reject_count") or 0)
        retry = int(src.get("tester_retry_count") or 0)
        rv_blk = int(src.get("reviewer_block_count") or 0)
        impl_exc = int(src.get("implementer_exception_count") or 0)

        cap_reasons = []
        if max_rework > 0 and rework >= max_rework:
            cap_reasons.append(f"reviewer_rework={rework}>={max_rework}")
        if max_reject > 0 and reject >= max_reject:
            cap_reasons.append(f"tester_reject={reject}>={max_reject}")
        if max_tester_retry > 0 and retry >= max_tester_retry:
            cap_reasons.append(f"tester_retry={retry}>={max_tester_retry}")
        if max_reviewer_block > 0 and rv_blk >= max_reviewer_block:
            cap_reasons.append(f"reviewer_block={rv_blk}>={max_reviewer_block}")
        if max_impl_exc > 0 and impl_exc >= max_impl_exc:
            cap_reasons.append(f"implementer_exc={impl_exc}>={max_impl_exc}")

        owner = (src.get("owner") or src.get("assigned_agent_role") or DEFAULT_ROLE)
        owner = str(owner).strip().lower() or DEFAULT_ROLE
        if owner not in _VALID_ROLES:
            owner = DEFAULT_ROLE

        try:
            if cap_reasons:
                reason = ", ".join(cap_reasons)
                append_note(
                    es_id,
                    f"[Orphan heal] Task was in an unreachable state and has already "
                    f"exceeded loop caps ({reason}). Blocking for human review instead "
                    "of re-queueing so it cannot spin further.",
                )
                es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": {
                    "status": STATUS_BLOCKED, "needs_human": True,
                    "owner": owner, "assigned_agent_role": owner,
                    "active_worker": None, "queue_state": QUEUE_STATE_QUEUED,
                    "updated_at": now, "last_update": now,
                }})
                result.blocked += 1
                logger.info("Orphan heal blocked",
                             extra={"structured_data": {"event": EVENT_ORPHAN_BLOCKED, "task_id": task_id, "owner": owner, "caps_exceeded": cap_reasons}})
            else:
                new_status = _status_for_role(owner)
                es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": {
                    "status": new_status, "owner": owner,
                    "assigned_agent_role": owner, "active_worker": None,
                    "queue_state": QUEUE_STATE_QUEUED, "needs_human": False,
                    "updated_at": now, "last_update": now,
                }})
                result.healed += 1
                logger.info("Orphan heal routed",
                             extra={"structured_data": {"event": EVENT_ORPHAN_ROUTED, "task_id": task_id, "owner": owner, "from_status": src.get("status"), "to_status": new_status}})
            processed += 1
        except SAFE_EXCEPTIONS as e:
            result.errors += 1
            logger.warning("Orphan heal update failed",
                            extra={"structured_data": {"event": EVENT_ORPHAN_UPDATE_FAILED, "task_id": task_id, "error": str(e)[:200]}})



def _loop(deps: dict) -> None:
    last_run_at = {
        "parent_revival": 0.0,
        "stuck_worker_watchdog": 0.0,
        "plan_progress_scan": 0.0,
        "branch_gc": 0.0,
        "pr_reconcile": 0.0,
        "orphan_heal": 0.0,
    }

    # initial splay so sweeps don't all fire in the same second
    time.sleep(10)

    while True:
        # Reload config once per tick so env var changes take effect without restart.
        config = AutonomySweepsConfig.from_env()
        now = time.time()

        sweep_specs = [
            ("parent_revival", config.parent_revival_interval_sec, _parent_revival_sweep),
            ("stuck_worker_watchdog", config.stuck_task_interval_sec, _stuck_worker_watchdog),
            ("plan_progress_scan", config.plan_scan_interval_sec, _plan_progress_scan),
            ("branch_gc", config.branch_gc_interval_sec, _branch_gc_sweep),
            ("pr_reconcile", config.pr_reconcile_interval_sec, _pr_reconcile_sweep),
            ("orphan_heal", config.orphan_heal_interval_sec, _orphan_heal_sweep),
        ]

        for sweep_name, interval, fn in sweep_specs:
            if now - last_run_at[sweep_name] < interval:
                continue
            started = time.time()
            try:
                result = fn(deps, config)
            except SAFE_EXCEPTIONS as e:
                result = SweepResult(errors=1)
                logger.exception(f"{sweep_name}.crashed: {e}")
                with _STATE_LOCK:
                    _STATE["sweeps"].setdefault(sweep_name, {})["last_error"] = str(e)[:300]
            duration_ms = int((time.time() - started) * 1000)
            _record_summary(sweep_name, result, duration_ms)
            summary = result.model_dump() if isinstance(result, SweepResult) else result
            # Only log if there was meaningful activity.
            has_activity = any(summary.get(k, 0) for k in (
                "revived", "recovered", "escalated", "nudged", "deleted",
                "merged", "rebased", "healed", "blocked", "errors",
            ))
            if has_activity:
                logger.info(
                    f"Sweep {sweep_name} completed",
                    extra={"structured_data": {"event": f"{sweep_name}.sweep", **summary, "duration_ms": duration_ms}},
                )
            last_run_at[sweep_name] = time.time()

        time.sleep(max(15, config.loop_interval_sec))


# ---- public API -------------------------------------------------------------


def maybe_start(
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    es_upsert: Callable[[str, str, dict], Any],
    append_note: Callable[[str, str], bool],
    list_projects: Callable[[], list],
    logger,
) -> bool:
    """Idempotent start hook called from the dashboard lifespan."""
    config = AutonomySweepsConfig.from_env()
    with _STATE_LOCK:
        if _STATE["thread_alive"]:
            return True
        enabled = _env_bool("FLUME_AUTONOMY_ENABLED", True)
        _STATE["enabled"] = enabled
        _STATE["config"] = config.model_dump()
        if not enabled:
            logger.info("autonomy_sweeps: disabled via FLUME_AUTONOMY_ENABLED=0")
            return False
        _STATE["thread_alive"] = True

    deps = {
        "es_search": es_search,
        "es_post": es_post,
        "es_upsert": es_upsert,
        "append_note": append_note,
        "list_projects": list_projects,
        "logger": logger,
    }
    t = threading.Thread(target=_loop, args=(deps,), daemon=True, name="flume-autonomy")
    t.start()
    logger.info("Autonomy sweeps started",
                 extra={"structured_data": {"event": EVENT_STARTED, "config": _STATE["config"]}})
    return True


def get_status() -> dict:
    with _STATE_LOCK:
        # Return a deep-ish copy so callers can't mutate module state.
        return json.loads(json.dumps(_STATE))


def run_sweep_now(
    sweep_name: str,
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
    logger,
    es_upsert: Callable[[str, str, dict], Any] | None = None,
    list_projects: Callable[[], list] | None = None,
) -> dict:
    """Trigger a single sweep synchronously. Returns the summary."""
    deps = {
        "es_search": es_search,
        "es_post": es_post,
        "es_upsert": es_upsert,
        "append_note": append_note,
        "list_projects": list_projects,
        "logger": logger,
    }
    fn_map = {
        "parent_revival": _parent_revival_sweep,
        "stuck_worker_watchdog": _stuck_worker_watchdog,
        "plan_progress_scan": _plan_progress_scan,
        "branch_gc": _branch_gc_sweep,
        "pr_reconcile": _pr_reconcile_sweep,
        "orphan_heal": _orphan_heal_sweep,
    }
    fn = fn_map.get(sweep_name)
    if not fn:
        raise ValueError(f"unknown sweep: {sweep_name!r}")
    started = time.time()
    config = AutonomySweepsConfig.from_env()
    result = fn(deps, config)
    duration_ms = int((time.time() - started) * 1000)
    _record_summary(sweep_name, result, duration_ms)
    summary = result.model_dump() if isinstance(result, SweepResult) else result
    return {"sweep": sweep_name, "duration_ms": duration_ms, "summary": summary}
