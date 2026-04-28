"""
Auto-unblocker daemon.

Scans ``agent-task-records`` for tasks in status=blocked that the user has NOT
pinned to human (needs_human != True) and tries to get them moving again by:

  1. Gathering the last few agent_log/execution_thoughts entries, the most
     recent failure record, handoff, and reviewer verdict.
  2. Asking the configured LLM for a concise recovery plan (≤ ~1200 chars).
     On LLM failure, falls back to a canned recovery hint — we still re-queue
     so the agent can re-try with fresh context.
  3. Appending the plan as an ``[Auto-recovery]`` note on agent_log.
  4. Flipping the task back to ``status=ready`` with the same owner it held
     when it blocked, clearing active_worker/queue_state so it is re-claimable.

Each attempt increments ``auto_unblock_attempts``. Once a per-task cap is hit,
the task is marked ``needs_human=True`` with an explanatory note so it stops
being touched by the daemon and shows up prominently in the UI.

Configuration (env):

  FLUME_AUTO_UNBLOCK_ENABLED       default "1"  — master switch
  FLUME_AUTO_UNBLOCK_INTERVAL_SEC  default 180  — sweep period
  FLUME_AUTO_UNBLOCK_GRACE_SEC     default 120  — don't touch tasks that
                                                   just transitioned to blocked
  FLUME_AUTO_UNBLOCK_MAX           default 3    — attempts before escalation
  FLUME_AUTO_UNBLOCK_BATCH         default 10   — max tasks processed per sweep
  FLUME_AUTO_UNBLOCK_LLM_TIMEOUT   default 45   — seconds per LLM call

The module is deliberately self-contained: no FastAPI imports, no direct DB
writes outside of the helpers it receives from the dashboard server module.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from utils.exceptions import SAFE_EXCEPTIONS
from utils.logger import get_logger

logger = get_logger("auto_unblock")


# ─── Constants ─────────────────────────────────────────────────────────────────

# Environment variable names.
ENV_ENABLED = "FLUME_AUTO_UNBLOCK_ENABLED"
ENV_INTERVAL_SEC = "FLUME_AUTO_UNBLOCK_INTERVAL_SEC"
ENV_GRACE_SEC = "FLUME_AUTO_UNBLOCK_GRACE_SEC"
ENV_MAX_ATTEMPTS = "FLUME_AUTO_UNBLOCK_MAX"
ENV_BATCH = "FLUME_AUTO_UNBLOCK_BATCH"
ENV_LLM_TIMEOUT = "FLUME_AUTO_UNBLOCK_LLM_TIMEOUT"

# Defaults.
DEFAULT_INTERVAL_SEC = 180
DEFAULT_GRACE_SEC = 120
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BATCH = 10
DEFAULT_LLM_TIMEOUT = 45
DEFAULT_INITIAL_SLEEP_DIVISOR = 6
MIN_LOOP_SLEEP_SEC = 15
MIN_INITIAL_SLEEP_SEC = 5
MAX_INITIAL_SLEEP_SEC = 30

# Task statuses.
STATUS_BLOCKED = "blocked"
STATUS_READY = "ready"
STATUS_ARCHIVED = "archived"
QUEUE_STATE_QUEUED = "queued"

# Elasticsearch index names.
ES_TASK_INDEX = "agent-task-records"
ES_FAILURE_INDEX = "agent-failure-records"
ES_HANDOFF_INDEX = "agent-handoff-records"
ES_REVIEW_INDEX = "agent-review-records"

# Event telemetry keys.
EVENT_REQUEUED = "auto_unblock.requeued"
EVENT_ESCALATED = "auto_unblock.escalated"
EVENT_QUERY_FAILED = "auto_unblock.query_failed"
EVENT_ESCALATE_FAILED = "auto_unblock.escalate_failed"
EVENT_PROCESS_FAILED = "auto_unblock.process_failed"
EVENT_SWEEP = "auto_unblock.sweep"
EVENT_STARTED = "auto_unblock.started"
EVENT_SWEEP_CRASHED = "auto_unblock.sweep_crashed"

# Rollup item types that unblock when children do — never auto-unblocked.
_SKIP_ITEM_TYPES = frozenset({"epic", "feature", "story"})

# LLM constraints.
_LLM_MAX_TOKENS = 512
_LLM_TEMPERATURE = 0.2
_LLM_PLAN_HARD_LIMIT = 1800
_CLIP_OBJECTIVE_LIMIT = 600
_CLIP_THOUGHT_LIMIT = 400
_CLIP_FAILURE_LIMIT = 400
_CLIP_HANDOFF_LIMIT = 300
_CLIP_REVIEW_LIMIT = 300

# Context collection limits.
_AGENT_LOG_TAIL_SIZE = 6
_EXECUTION_THOUGHTS_TAIL_SIZE = 4

# Skip reason constants.
SKIP_NOT_BLOCKED = "not_blocked"
SKIP_NEEDS_HUMAN = "needs_human"
SKIP_MERGE_CONFLICT = "merge_conflict_pending_reconcile"
SKIP_MAX_ATTEMPTS = "max_attempts_reached"
SKIP_GRACE_WINDOW = "within_grace_window"


# ─── Pydantic Models ──────────────────────────────────────────────────────────


class AutoUnblockConfig(BaseModel):
    """Validated daemon configuration read from environment variables."""

    interval_sec: int = DEFAULT_INTERVAL_SEC
    grace_sec: int = DEFAULT_GRACE_SEC
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    batch: int = DEFAULT_BATCH
    llm_timeout_sec: int = DEFAULT_LLM_TIMEOUT

    @classmethod
    def from_env(cls) -> AutoUnblockConfig:
        return cls(
            interval_sec=_env_int(ENV_INTERVAL_SEC, DEFAULT_INTERVAL_SEC),
            grace_sec=_env_int(ENV_GRACE_SEC, DEFAULT_GRACE_SEC),
            max_attempts=_env_int(ENV_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS),
            batch=_env_int(ENV_BATCH, DEFAULT_BATCH),
            llm_timeout_sec=_env_int(ENV_LLM_TIMEOUT, DEFAULT_LLM_TIMEOUT),
        )


class SweepSummary(BaseModel):
    """Structured result of a single daemon sweep iteration."""

    scanned: int = 0
    skipped: int = 0
    unblocked: int = 0
    escalated: int = 0
    errors: int = 0
    skip_reasons: dict[str, int] = Field(default_factory=dict)
    ids_unblocked: list[Optional[str]] = Field(default_factory=list)
    ids_escalated: list[Optional[str]] = Field(default_factory=list)


# ─── Module-Level Observability State ──────────────────────────────────────────

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "thread_alive": False,
    "last_sweep_at": None,
    "last_sweep_duration_ms": None,
    "last_sweep_summary": None,
    "sweep_count": 0,
    "unblocked_total": 0,
    "escalated_total": 0,
    "errors_total": 0,
    "last_error": None,
    "config": {},
}


# ─── Utility Functions ────────────────────────────────────────────────────────


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


def _tail(items: list, n: int) -> list:
    if not isinstance(items, list):
        return []
    return items[-n:] if len(items) > n else list(items)


def _clip(text: str, limit: int) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars]"


# ─── Task Skip Logic ──────────────────────────────────────────────────────────


def _should_skip(src: dict, grace_sec: int, max_attempts: int) -> str | None:
    """Return a reason string if this task should be skipped, else None."""
    if (src.get("status") or "").lower() != STATUS_BLOCKED:
        return SKIP_NOT_BLOCKED
    if src.get("needs_human"):
        return SKIP_NEEDS_HUMAN
    it = (src.get("item_type") or "").lower()
    if it in _SKIP_ITEM_TYPES:
        # Rollups should flow via compute_ready_for_repo when children finish.
        return f"rollup_item_type={it}"
    # Defer merge-conflict blocks to the pr_reconcile sweep. Re-queueing a
    # task that is blocked purely because its PR can't merge just loops —
    # the reviewer will re-approve identical code and the auto-merge will
    # fail again. Only pr_reconcile can actually rebase/resolve the branch,
    # so let it own these. Once the conflict is resolved upstream the flags
    # are cleared and this rule no longer skips.
    if src.get("merge_conflict") is True:
        return SKIP_MERGE_CONFLICT
    attempts = int(src.get("auto_unblock_attempts") or 0)
    if attempts >= max_attempts:
        return SKIP_MAX_ATTEMPTS
    # Honor grace period after most recent transition.
    last_ts = _parse_iso(src.get("updated_at") or src.get("last_update") or "")
    if last_ts is not None and (time.time() - last_ts) < grace_sec:
        return SKIP_GRACE_WINDOW
    return None


# ─── ES Context Collection ────────────────────────────────────────────────────


def _fetch_latest_record(
    es_search: Callable[[str, dict], dict],
    index: str,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """Fetch the most recent record from an ES index for a given task_id."""
    try:
        res = es_search(index, {
            "size": 1,
            "sort": [{"created_at": {"order": "desc"}}],
            "query": {"term": {"task_id": task_id}},
        })
        hits = res.get("hits", {}).get("hits", [])
        if hits:
            return hits[0].get("_source") or {}
    except SAFE_EXCEPTIONS:
        pass
    return None


def _collect_context(src: dict, es_search: Callable[[str, dict], dict]) -> dict:
    """Pull signal from related ES indices — best-effort, never raises."""
    task_id = src.get("id") or ""
    return {
        "agent_log_tail": _tail(src.get("agent_log") or [], _AGENT_LOG_TAIL_SIZE),
        "execution_thoughts_tail": _tail(
            src.get("execution_thoughts") or [], _EXECUTION_THOUGHTS_TAIL_SIZE
        ),
        "last_failure": _fetch_latest_record(es_search, ES_FAILURE_INDEX, task_id),
        "last_handoff": _fetch_latest_record(es_search, ES_HANDOFF_INDEX, task_id),
        "last_review": _fetch_latest_record(es_search, ES_REVIEW_INDEX, task_id),
    }


# ─── LLM Recovery Plan ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior engineer triaging a blocked automation task. "
    "Given the task JSON and recent signals, write a short, actionable "
    "recovery plan the implementer agent can immediately follow. "
    "Keep it under 900 characters. "
    "Use 2–5 terse bullets. "
    "Be specific: point to the failing check, the file/function to inspect, "
    "and the next verification step (command to run, test to add, etc.). "
    "Do NOT restate the whole task. Do NOT apologise. Do NOT say 'consult humans'."
)

_CANNED_PLAN = (
    "Re-queued automatically after a blocked state.\n"
    "- Read the last entries in agent_log and execution_thoughts above "
    "and identify the concrete failure.\n"
    "- Fix the root cause rather than the symptom; touch only the files "
    "named in the failure/handoff.\n"
    "- Re-run the relevant tests locally before handing off; add a "
    "regression test if one is missing.\n"
    "- If the root cause is truly external (missing secret, flaky network), "
    "say so explicitly in your final message so the next attempt is cheaper."
)


def _build_user_prompt(src: dict, ctx: dict) -> str:
    slim_task = {
        "id": src.get("id"),
        "item_type": src.get("item_type"),
        "title": src.get("title"),
        "objective": _clip(src.get("objective") or "", _CLIP_OBJECTIVE_LIMIT),
        "acceptance_criteria": src.get("acceptance_criteria") or [],
        "owner": src.get("owner") or src.get("assigned_agent_role"),
        "branch": src.get("branch"),
        "commit_sha": src.get("commit_sha"),
        "attempts": src.get("auto_unblock_attempts") or 0,
    }
    slim_ctx = {
        "agent_log_tail": ctx.get("agent_log_tail") or [],
        "execution_thoughts_tail": [
            _clip(x.get("content") if isinstance(x, dict) else str(x), _CLIP_THOUGHT_LIMIT)
            for x in (ctx.get("execution_thoughts_tail") or [])
        ],
        "last_failure": {
            k: _clip(str(v), _CLIP_FAILURE_LIMIT)
            for k, v in (ctx.get("last_failure") or {}).items()
            if k in ("summary", "root_cause", "error_class", "fix_applied")
        },
        "last_handoff": {
            k: _clip(str(v), _CLIP_HANDOFF_LIMIT)
            for k, v in (ctx.get("last_handoff") or {}).items()
            if k in ("from_role", "to_role", "reason", "status_hint")
        },
        "last_review": {
            k: _clip(str(v), _CLIP_REVIEW_LIMIT)
            for k, v in (ctx.get("last_review") or {}).items()
            if k in ("verdict", "summary", "issues")
        },
    }
    return (
        "TASK:\n"
        + json.dumps(slim_task, ensure_ascii=False)
        + "\n\nSIGNALS:\n"
        + json.dumps(slim_ctx, ensure_ascii=False)
        + "\n\nRespond with ONLY the plan (no preamble)."
    )


def _llm_recovery_plan(src: dict, ctx: dict, timeout_seconds: int) -> tuple[str, bool]:
    """Returns (plan, llm_ok). On any LLM failure returns a canned plan + False."""
    try:
        from utils import llm_client  # lazy: imports settings
    except SAFE_EXCEPTIONS:
        return _CANNED_PLAN, False

    prompt_user = _build_user_prompt(src, ctx)
    try:
        reply = llm_client.chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt_user},
            ],
            temperature=_LLM_TEMPERATURE,
            max_tokens=_LLM_MAX_TOKENS,
            timeout_seconds=timeout_seconds,
            agent_role="auto_unblocker",
        )
    except SAFE_EXCEPTIONS as e:
        return _CANNED_PLAN + f"\n\n(LLM unavailable: {str(e)[:120]})", False

    plan = (reply or "").strip()
    if not plan:
        return _CANNED_PLAN, False
    # Safety clamp — should rarely trigger given max_tokens constraint.
    if len(plan) > _LLM_PLAN_HARD_LIMIT:
        plan = plan[:_LLM_PLAN_HARD_LIMIT] + "… [truncated]"
    return plan, True


# ─── Task Mutation Handlers ────────────────────────────────────────────────────


def _requeue_task(
    *,
    es_id: str,
    src: dict,
    plan: str,
    llm_ok: bool,
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
) -> None:
    attempts = int(src.get("auto_unblock_attempts") or 0) + 1
    header = f"[Auto-recovery #{attempts}]" + ("" if llm_ok else " (LLM fallback)")
    append_note(es_id, f"{header}\n{plan}")

    now = _now_iso()
    owner = src.get("owner") or src.get("assigned_agent_role")
    doc = {
        "status": STATUS_READY,
        "queue_state": QUEUE_STATE_QUEUED,
        "active_worker": None,
        "needs_human": False,
        "auto_unblock_attempts": attempts,
        "auto_unblock_last_at": now,
        "updated_at": now,
        "last_update": now,
        "implementer_consecutive_llm_failures": 0,
    }
    if owner:
        doc["owner"] = owner
        doc["assigned_agent_role"] = owner
    es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": doc})
    logger.info(
        "Task requeued by auto-unblocker",
        extra={"structured_data": {
            "event": EVENT_REQUEUED,
            "task_id": src.get("id"),
            "attempts": attempts,
            "llm_ok": llm_ok,
            "owner": owner,
        }},
    )


def _escalate_task(
    *,
    es_id: str,
    src: dict,
    max_attempts: int,
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
) -> None:
    note = (
        f"[Auto-recovery] Giving up after {max_attempts} automatic attempts. "
        "Needs human guidance — use the Unblock dialog to provide direction "
        "or mark the task done/archived."
    )
    append_note(es_id, note)
    now = _now_iso()
    doc = {
        "needs_human": True,
        "auto_unblock_last_at": now,
        "updated_at": now,
        "last_update": now,
    }
    es_post(f"{ES_TASK_INDEX}/_update/{es_id}", {"doc": doc})
    logger.info(
        "Task escalated to human by auto-unblocker",
        extra={"structured_data": {
            "event": EVENT_ESCALATED,
            "task_id": src.get("id"),
            "attempts": int(src.get("auto_unblock_attempts") or 0),
        }},
    )


# ─── Sweep Orchestration ──────────────────────────────────────────────────────


def _sweep_once(
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
) -> SweepSummary:
    """Execute a single sweep. Returns a validated summary."""
    config = AutoUnblockConfig.from_env()
    summary = SweepSummary()

    try:
        res = es_search(ES_TASK_INDEX, {
            "size": 200,
            "sort": [{"updated_at": {"order": "asc"}}],
            "query": {
                "bool": {
                    "must": [{"term": {"status": STATUS_BLOCKED}}],
                    "must_not": [
                        {"term": {"needs_human": True}},
                        {"term": {"status": STATUS_ARCHIVED}},
                    ],
                }
            },
        })
    except SAFE_EXCEPTIONS as e:
        logger.warning(
            "Auto-unblock ES query failed",
            extra={"structured_data": {"event": EVENT_QUERY_FAILED, "error": str(e)[:200]}},
        )
        summary.errors += 1
        return summary

    hits = res.get("hits", {}).get("hits", []) or []
    summary.scanned = len(hits)

    processed = 0
    for h in hits:
        if processed >= config.batch:
            break
        es_id = h.get("_id")
        src = h.get("_source") or {}
        reason = _should_skip(src, grace_sec=config.grace_sec, max_attempts=config.max_attempts)
        if reason == SKIP_MAX_ATTEMPTS:
            try:
                _escalate_task(
                    es_id=es_id,
                    src=src,
                    max_attempts=config.max_attempts,
                    es_post=es_post,
                    append_note=append_note,
                )
                summary.escalated += 1
                summary.ids_escalated.append(src.get("id"))
                processed += 1
            except SAFE_EXCEPTIONS as e:
                summary.errors += 1
                logger.warning(
                    "Auto-unblock escalation failed",
                    extra={"structured_data": {
                        "event": EVENT_ESCALATE_FAILED,
                        "task_id": src.get("id"),
                        "error": str(e)[:200],
                    }},
                )
            continue
        if reason:
            summary.skipped += 1
            summary.skip_reasons[reason] = summary.skip_reasons.get(reason, 0) + 1
            continue

        try:
            ctx = _collect_context(src, es_search)
            plan, llm_ok = _llm_recovery_plan(src, ctx, timeout_seconds=config.llm_timeout_sec)
            _requeue_task(
                es_id=es_id,
                src=src,
                plan=plan,
                llm_ok=llm_ok,
                es_post=es_post,
                append_note=append_note,
            )
            summary.unblocked += 1
            summary.ids_unblocked.append(src.get("id"))
            processed += 1
        except SAFE_EXCEPTIONS as e:
            summary.errors += 1
            logger.warning(
                "Auto-unblock task processing failed",
                extra={"structured_data": {
                    "event": EVENT_PROCESS_FAILED,
                    "task_id": src.get("id"),
                    "error": str(e)[:300],
                }},
            )

    return summary


# ─── Daemon Thread ─────────────────────────────────────────────────────────────


def _loop(
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
) -> None:
    interval = _env_int(ENV_INTERVAL_SEC, DEFAULT_INTERVAL_SEC)
    # Small initial delay so it never races with lifespan startup tasks.
    time.sleep(min(MAX_INITIAL_SLEEP_SEC, max(MIN_INITIAL_SLEEP_SEC, interval // DEFAULT_INITIAL_SLEEP_DIVISOR)))
    while True:
        started = time.time()
        summary: Optional[SweepSummary] = None
        try:
            summary = _sweep_once(
                es_search=es_search,
                es_post=es_post,
                append_note=append_note,
            )
        except SAFE_EXCEPTIONS as e:
            with _STATE_LOCK:
                _STATE["errors_total"] += 1
                _STATE["last_error"] = str(e)[:300]
            logger.exception(
                "Auto-unblock sweep crashed",
                extra={"structured_data": {"event": EVENT_SWEEP_CRASHED, "error": str(e)[:300]}},
            )
        finally:
            duration_ms = int((time.time() - started) * 1000)
            summary_dict = summary.model_dump() if summary else None
            with _STATE_LOCK:
                _STATE["running"] = False
                _STATE["last_sweep_at"] = _now_iso()
                _STATE["last_sweep_duration_ms"] = duration_ms
                _STATE["last_sweep_summary"] = summary_dict
                _STATE["sweep_count"] += 1
                _STATE["unblocked_total"] += summary.unblocked if summary else 0
                _STATE["escalated_total"] += summary.escalated if summary else 0
                _STATE["errors_total"] += summary.errors if summary else 0
            if summary and (summary.unblocked or summary.escalated or summary.errors):
                logger.info(
                    "Auto-unblock sweep completed",
                    extra={"structured_data": {
                        "event": EVENT_SWEEP,
                        **summary.model_dump(),
                        "duration_ms": duration_ms,
                    }},
                )

        # Re-read interval on each loop so it can be tuned without a restart.
        interval = _env_int(ENV_INTERVAL_SEC, DEFAULT_INTERVAL_SEC)
        time.sleep(max(MIN_LOOP_SLEEP_SEC, interval))


# ─── Public API ────────────────────────────────────────────────────────────────


def maybe_start(
    *,
    es_search: Callable[[str, dict], dict],
    es_post: Callable[[str, dict], Any],
    append_note: Callable[[str, str], bool],
) -> bool:
    """Start the daemon thread once. Idempotent — repeat calls are no-ops."""
    with _STATE_LOCK:
        if _STATE["thread_alive"]:
            return True
        enabled = _env_bool(ENV_ENABLED, True)
        config = AutoUnblockConfig.from_env()
        _STATE["enabled"] = enabled
        _STATE["config"] = config.model_dump()
        if not enabled:
            logger.info(
                "Auto-unblock disabled",
                extra={"structured_data": {"event": "auto_unblock.disabled"}},
            )
            return False
        _STATE["thread_alive"] = True

    t = threading.Thread(
        target=_loop,
        kwargs={"es_search": es_search, "es_post": es_post, "append_note": append_note},
        daemon=True,
        name="flume-auto-unblock",
    )
    t.start()
    logger.info(
        "Auto-unblock daemon started",
        extra={"structured_data": {"event": EVENT_STARTED, "config": config.model_dump()}},
    )
    return True


def get_status() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)
