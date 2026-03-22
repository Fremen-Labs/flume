#!/usr/bin/env python3
"""Print shell exports for ES_* after OpenBao hydration (used by create-es-indices.sh)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_openbao_cli() -> str | None:
    for name in ("openbao", "bao"):
        p = shutil.which(name)
        if p:
            return p
    for candidate in (Path("/usr/local/bin/openbao"), Path("/usr/local/bin/bao")):
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _load_flume_config_openbao(workspace_root: Path) -> None:
    cfg = workspace_root / "flume.config.json"
    if not cfg.is_file():
        return
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    ob = data.get("openbao") or {}
    addr = str(ob.get("addr", "")).strip()
    if addr:
        os.environ.setdefault("OPENBAO_ADDR", addr)
    mount = str(ob.get("mount", "secret") or "secret").strip().strip("/")
    path = str(ob.get("path", "flume") or "flume").strip().strip("/")
    os.environ.setdefault("OPENBAO_MOUNT", mount)
    os.environ.setdefault("OPENBAO_PATH", path)
    tf = str(ob.get("tokenFile", "") or "").strip()
    if tf:
        p = Path(tf).expanduser()
        os.environ.setdefault("OPENBAO_TOKEN_FILE", str(p))
        if p.is_file():
            tok = p.read_text(encoding="utf-8", errors="replace").strip()
            if tok:
                os.environ["OPENBAO_TOKEN"] = tok


def _merge_openbao_kv_into_environ() -> None:
    addr = os.environ.get("OPENBAO_ADDR", "").strip().rstrip("/")
    token = os.environ.get("OPENBAO_TOKEN", "").strip()
    if not addr or not token:
        return
    mount = os.environ.get("OPENBAO_MOUNT", "secret").strip().strip("/")
    path = os.environ.get("OPENBAO_PATH", "flume").strip().strip("/")
    env = dict(os.environ)
    env["VAULT_ADDR"] = addr
    env["VAULT_TOKEN"] = token
    env["BAO_ADDR"] = addr
    env["BAO_TOKEN"] = token
    cli = _resolve_openbao_cli()
    data: dict | None = None
    if cli:
        try:
            proc = subprocess.run(
                [cli, "kv", "get", "-format=json", f"{mount}/{path}"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            if proc.returncode == 0:
                payload = json.loads(proc.stdout or "{}")
                raw = payload.get("data", {}).get("data", {})
                if isinstance(raw, dict):
                    data = raw
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            data = None
    if data is None:
        try:
            import ssl
            import urllib.request

            url = f"{addr}/v1/{mount}/data/{path}"
            req = urllib.request.Request(url, headers={"X-Vault-Token": token})
            ctx = None
            if url.startswith("https://"):
                if os.environ.get("OPENBAO_SKIP_TLS_VERIFY", "").lower() in ("1", "true", "yes", "on"):
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                else:
                    from urllib.parse import urlparse

                    h = (urlparse(url).hostname or "").lower()
                    if h in ("127.0.0.1", "localhost", "::1"):
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
            raw = payload.get("data", {}).get("data", {})
            if not isinstance(raw, dict):
                return
            data = raw
        except Exception:
            return
    for k, v in data.items():
        if v is not None and str(v).strip():
            os.environ[str(k)] = str(v)


def main() -> None:
    workspace_root = Path(os.environ.get("FLUME_WORKSPACE_ROOT", "")).resolve()
    if not workspace_root.is_dir():
        print("echo 'hydrate-openbao-env: set FLUME_WORKSPACE_ROOT to Flume repo root' >&2", file=sys.stderr)
        sys.exit(1)
    src = workspace_root / "src"
    if not src.is_dir():
        src = workspace_root
    sys.path.insert(0, str(src))

    _load_flume_config_openbao(workspace_root)
    _merge_openbao_kv_into_environ()

    from flume_secrets import apply_runtime_config

    apply_runtime_config(src)

    keys = ("ES_API_KEY", "ES_URL", "ES_VERIFY_TLS")
    for k in keys:
        v = os.environ.get(k, "")
        if v:
            safe = v.replace("'", "'\"'\"'")
            print(f"export {k}='{safe}'")


if __name__ == "__main__":
    main()
