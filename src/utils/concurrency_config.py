"""Project concurrency / WIP configuration.

Centralized defaults + helpers for Flume's "smarter PM" dispatcher:
  - maxRunningPerRepo:       hard cap on simultaneous running+review tasks per repo
  - maxReadyPerRepo:         soft cap on tasks staged as `ready` per repo
  - storyParallelism:        max number of stories advancing in parallel per feature
  - serializeIntegrationMerge: serialize PR->develop merges per repo

Each key can be overridden per-project via the project doc's `concurrency`
sub-object, and each can be forced globally via an environment variable.

A value of 0 means "unlimited" (restores legacy behavior).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

CONFIG_KEY = "concurrency"

DEFAULTS: Dict[str, Any] = {
    "maxRunningPerRepo": 2,
    "maxReadyPerRepo": 4,
    "storyParallelism": 1,
    "serializeIntegrationMerge": True,
}

_ENV_OVERRIDES = {
    "maxRunningPerRepo": "FLUME_MAX_RUNNING_PER_REPO",
    "maxReadyPerRepo": "FLUME_MAX_READY_PER_REPO",
    "storyParallelism": "FLUME_STORY_PARALLELISM",
    "serializeIntegrationMerge": "FLUME_SERIALIZE_INTEGRATION_MERGE",
}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    return default


def ensure_concurrency_defaults(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate *entry* (a project doc) so it contains a complete `concurrency` block."""
    existing = entry.get(CONFIG_KEY)
    if not isinstance(existing, dict):
        existing = {}
    for key, default in DEFAULTS.items():
        if key not in existing:
            existing[key] = default
    entry[CONFIG_KEY] = existing
    return entry


def get_concurrency_config(project: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the effective concurrency config for *project* (env > project > defaults)."""
    cfg = dict(DEFAULTS)
    if isinstance(project, dict):
        proj_cfg = project.get(CONFIG_KEY)
        if isinstance(proj_cfg, dict):
            for key in DEFAULTS:
                if key in proj_cfg:
                    cfg[key] = proj_cfg[key]
    for key, env_name in _ENV_OVERRIDES.items():
        env_val = os.environ.get(env_name)
        if env_val is None or env_val == "":
            continue
        if key == "serializeIntegrationMerge":
            cfg[key] = _coerce_bool(env_val, cfg[key])
        else:
            cfg[key] = _coerce_int(env_val, cfg[key])
    cfg["maxRunningPerRepo"] = max(0, _coerce_int(cfg["maxRunningPerRepo"], DEFAULTS["maxRunningPerRepo"]))
    cfg["maxReadyPerRepo"] = max(0, _coerce_int(cfg["maxReadyPerRepo"], DEFAULTS["maxReadyPerRepo"]))
    cfg["storyParallelism"] = max(0, _coerce_int(cfg["storyParallelism"], DEFAULTS["storyParallelism"]))
    cfg["serializeIntegrationMerge"] = _coerce_bool(
        cfg["serializeIntegrationMerge"], DEFAULTS["serializeIntegrationMerge"]
    )
    return cfg


def max_running_for_repo(project: Optional[Dict[str, Any]]) -> int:
    return int(get_concurrency_config(project).get("maxRunningPerRepo") or 0)


def max_ready_for_repo(project: Optional[Dict[str, Any]]) -> int:
    return int(get_concurrency_config(project).get("maxReadyPerRepo") or 0)


def story_parallelism(project: Optional[Dict[str, Any]]) -> int:
    return int(get_concurrency_config(project).get("storyParallelism") or 0)


def serialize_integration_merge(project: Optional[Dict[str, Any]]) -> bool:
    return bool(get_concurrency_config(project).get("serializeIntegrationMerge"))
