"""
Flume runtime configuration from OpenBao (preferred) with optional .env legacy support.

Out-of-box flow:
  1. Place flume.config.json at the repo root or next to src/ (see flume.config.example.json).
  2. Put only OpenBao address + auth in bootstrap (token via OPENBAO_TOKEN_FILE is recommended).
  3. Store ES_API_KEY, LLM_API_KEY, GH_TOKEN, and other install/runtime values in KV at mount/path.

Bootstrap file is non-secret JSON. Secrets live only in OpenBao KV (or process env from your orchestrator).
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Any

# Keys merged from process environment into Settings / effective pairs (after OpenBao hydration).
FLUME_ENV_KEYS = frozenset(
    {
        "ES_URL",
        "ES_API_KEY",
        "ES_VERIFY_TLS",
        "ES_INDEX_TASKS",
        "ES_INDEX_HANDOFFS",
        "ES_INDEX_FAILURES",
        "ES_INDEX_REVIEWS",
        "ES_INDEX_PROVENANCE",
        "ES_INDEX_MEMORY",
        "DASHBOARD_HOST",
        "DASHBOARD_PORT",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "OPENAI_OAUTH_STATE_FILE",
        "OPENAI_OAUTH_TOKEN_URL",
        "OPENAI_OAUTH_RESOURCE",
        "GH_TOKEN",
        "ADO_TOKEN",
        "ADO_ORG_URL",
        "GIT_USER_NAME",
        "GIT_USER_EMAIL",
        "EXECUTION_HOST",
        "WORKER_MANAGER_POLL_SECONDS",
        "WORKERS_PER_ROLE",
        "OPENBAO_ADDR",
        "OPENBAO_TOKEN",
        "OPENBAO_MOUNT",
        "OPENBAO_PATH",
    }
)

# Backward alias
_KNOWN_CONFIG_KEYS = FLUME_ENV_KEYS


def _expand_path(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p))).resolve()


def discover_bootstrap_paths(workspace_root: Path) -> list[Path]:
    """Search workspace dir first, then parent (repo root), for flume.config.json."""
    roots: list[Path] = []
    wr = workspace_root.resolve()
    parent = wr.parent.resolve()
    for r in (wr, parent):
        if r not in roots:
            roots.append(r)
    out: list[Path] = []
    for r in roots:
        cfg = r / "flume.config.json"
        if cfg.is_file():
            out.append(cfg)
    return out


def load_merged_bootstrap(workspace_root: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for p in discover_bootstrap_paths(workspace_root):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
        except (OSError, json.JSONDecodeError) as e:
            logging.warning(f"Failed to load bootstrap JSON at {p}: {e}")
            continue
    return merged


def _read_token_file(path_str: str) -> str:
    if not path_str.strip():
        return ""
    p = _expand_path(path_str)
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace").strip()


def resolve_openbao_credentials(
    workspace_root: Path,
    bootstrap: dict[str, Any],
) -> tuple[str, str] | None:
    """Return (addr, token) if OpenBao should be used; else None."""
    ob = bootstrap.get("openbao")
    if not isinstance(ob, dict):
        ob = {}

    addr = (
        os.environ.get("OPENBAO_ADDR", "").strip()
        or str(ob.get("addr", "")).strip()
    )
    if not addr:
        return None

    token = os.environ.get("OPENBAO_TOKEN", "").strip()
    if not token:
        tf = (
            os.environ.get("OPENBAO_TOKEN_FILE", "").strip()
            or str(ob.get("tokenFile", "")).strip()
        )
        token = _read_token_file(tf)
    if not token:
        token = str(ob.get("token", "")).strip()

    if not token:
        return None

    return addr, token


def openbao_mount_path(bootstrap: dict[str, Any]) -> tuple[str, str]:
    ob = bootstrap.get("openbao")
    if not isinstance(ob, dict):
        ob = {}
    mount = str(ob.get("mount", "secret") or "secret").strip().strip("/")
    path = str(ob.get("path", "flume") or "flume").strip().strip("/")
    return mount, path


def _bao_subprocess_env(addr: str, token: str) -> dict[str, str]:
    env = dict(os.environ)
    env["BAO_ADDR"] = addr
    env["BAO_TOKEN"] = token
    env["VAULT_ADDR"] = addr
    env["VAULT_TOKEN"] = token
    env["OPENBAO_ADDR"] = addr
    env["OPENBAO_TOKEN"] = token
    return env


def fetch_openbao_kv(addr: str, token: str, mount: str, path: str) -> dict[str, str]:
    if not shutil.which("openbao"):
        return {}
    secret_ref = f"{mount}/{path}"
    try:
        proc = subprocess.run(
            ["openbao", "kv", "get", "-format=json", secret_ref],
            capture_output=True,
            text=True,
            timeout=30,
            env=_bao_subprocess_env(addr, token),
        )
        if proc.returncode != 0:
            return {}
        payload = json.loads(proc.stdout or "{}")
        data = payload.get("data", {}).get("data", {})
        if not isinstance(data, dict):
            return {}
        return {str(k): "" if v is None else str(v) for k, v in data.items()}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, TypeError) as e:
        logging.warning(f"OpenBao KV fetch failed for {secret_ref}: {e}")
        return {}


def _apply_dotenv_line(raw_line: str) -> None:
    """Parse one KEY=VAL line from a .env file into os.environ (last write wins)."""
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return
    key, _, val = line.partition("=")
    key = key.strip()
    
    # ENFORCE NATIVE OPENBAO: Do not allow sensitive credentials to be loaded from plaintext .env files
    if key in {"ES_API_KEY", "LLM_API_KEY", "GH_TOKEN", "ADO_TOKEN", "OPENAI_OAUTH_SCOPES"}:
        logging.warning(f"SECURITY: Attempted to load sensitive key '{key}' from plaintext .env file. This is blocked. Please migrate this secret natively into your OpenBao vault.")
        return

    if not key or key.startswith("#"):
        return
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1]
    os.environ[key] = val


def load_legacy_dotenv_into_environ(workspace_root: Path) -> None:
    """
    Load ``<workspace>/.env`` then ``<repo-root>/.env`` into the process environment.

    The repo root is ``workspace_root.parent`` (e.g. ``flume/.env`` when workspace is
    ``flume/src``). **Later files override earlier keys**, so the repo root wins on
    duplicates — matching ``run.sh`` (which prefers repo-root ``.env``) and
    ``llm_settings.load_env_pairs``.

    This avoids a common bug: a stale ``src/.env`` must not shadow the real
    ``flume/.env`` that the installer maintains.
    """
    wr = workspace_root.resolve()
    for candidate in (wr / ".env", wr.parent / ".env"):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logging.warning(f"Failed to read legacy .env file at {candidate}: {e}")
            continue
        for raw in text.splitlines():
            _apply_dotenv_line(raw)


def apply_runtime_config(workspace_root: Path) -> bool:
    """
    Load legacy ``.env`` files, then ``flume.config.json`` (if any), then OpenBao KV.

    Returns True if OpenBao credentials were resolved and a KV read was attempted
    (even if the secret was empty or missing keys).
    """
    load_legacy_dotenv_into_environ(workspace_root)
    bootstrap = load_merged_bootstrap(workspace_root)
    creds = resolve_openbao_credentials(workspace_root, bootstrap)
    if not creds:
        return False

    addr, token = creds
    mount, path = openbao_mount_path(bootstrap)
    data = fetch_openbao_kv(addr, token, mount, path)

    # Merge KV into environment (KV wins over prior empty env for those keys)
    for key, val in data.items():
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        os.environ[key] = s

    # Ensure child processes and llm_settings see OpenBao connection
    for k, v in (("OPENBAO_ADDR", addr), ("OPENBAO_TOKEN", token)):
        os.environ[k] = v
    os.environ["BAO_ADDR"] = addr
    os.environ["BAO_TOKEN"] = token
    os.environ["VAULT_ADDR"] = addr
    os.environ["VAULT_TOKEN"] = token
    os.environ.setdefault("OPENBAO_MOUNT", mount)
    os.environ.setdefault("OPENBAO_PATH", path)
    return True


def resolve_oauth_state_path(workspace_root: Path, configured: str) -> Path:
    """
    Resolve ``OPENAI_OAUTH_STATE_FILE`` for git layout (workspace = ``src/``).

    Relative paths check **repo root** (``workspace_root.parent``) first, then
    ``workspace_root``, so ``.openai-oauth.json`` at the Flume root matches
    installer defaults and ``codex_oauth_login.py`` output.
    """
    raw = (configured or "").strip()
    rel = Path(raw) if raw else Path(".openai-oauth.json")
    if rel.is_absolute():
        return rel
    wr = workspace_root.resolve()
    for base in (wr.parent, wr):
        cand = (base / rel).resolve()
        if cand.is_file():
            return cand
    return (wr.parent / rel).resolve()


def has_openbao_bootstrap(workspace_root: Path) -> bool:
    """True if flume.config.json exists or OPENBAO_ADDR is set."""
    if os.environ.get("OPENBAO_ADDR", "").strip():
        return True
    return bool(discover_bootstrap_paths(workspace_root))


def has_legacy_dotenv(workspace_root: Path) -> bool:
    wr = workspace_root.resolve()
    for candidate in (wr / ".env", wr.parent / ".env"):
        if candidate.is_file():
            return True
    return False


def config_present(workspace_root: Path) -> bool:
    """Either OpenBao bootstrap or a legacy .env file exists."""
    return has_openbao_bootstrap(workspace_root) or has_legacy_dotenv(workspace_root)
