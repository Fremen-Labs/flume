#!/usr/bin/env python3
"""
After create-es-indices.sh remaps ES_URL from Docker hostname to localhost,
persist the new URL to OpenBao KV and/or .env so non-technical users need not edit secrets.

Invoked only when FLUME_PERSIST_ES_URL and FLUME_PERSIST_ES_URL_WAS are set by the shell script.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


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


def _patch_env_es_url(env_path: Path, was: str, new: str) -> bool:
    if not env_path.is_file():
        return False
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = text.splitlines()
    out: list[str] = []
    changed = False
    was_norm = was.strip().rstrip("/")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ES_URL=") and not stripped.startswith("#"):
            _, _, val = stripped.partition("=")
            val_stripped = val.strip().strip('"').strip("'").rstrip("/")
            if (
                "elasticsearch" in val.lower()
                or val_stripped == was_norm
                or val_stripped == was.strip().rstrip("/")
            ):
                out.append(f"ES_URL={new}")
                changed = True
            else:
                out.append(line)
        else:
            out.append(line)
    if not changed:
        return False
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def main() -> int:
    new_url = (os.environ.get("FLUME_PERSIST_ES_URL") or "").strip()
    was_url = (os.environ.get("FLUME_PERSIST_ES_URL_WAS") or "").strip()
    wr = Path(os.environ.get("FLUME_WORKSPACE_ROOT", "")).resolve()
    if not new_url or not was_url or "elasticsearch" not in was_url.lower():
        return 0
    if new_url == was_url:
        return 0
    if not wr.is_dir():
        return 0

    _load_flume_config_openbao(wr)
    src = wr / "src"
    if not src.is_dir():
        return 0
    sys.path.insert(0, str(src))

    from dashboard.llm_settings import _openbao_enabled, _openbao_get_all, _openbao_put_many

    notes: list[str] = []
    ob_ok, _pairs = _openbao_enabled(wr)
    if ob_ok:
        existing = _openbao_get_all(wr)
        kv_es = (existing.get("ES_URL") or "").strip()
        should_write_kv = (
            not kv_es
            or "elasticsearch" in kv_es.lower()
            or kv_es.rstrip("/") == was_url.rstrip("/")
        )
        if should_write_kv:
            if _openbao_put_many(wr, {"ES_URL": new_url}):
                notes.append("OpenBao KV (secret/flume)")
            else:
                print(
                    "  \033[1;33m[WARN]\033[0m  Could not update ES_URL in OpenBao (check token and path).",
                    flush=True,
                )

    env_path = wr / ".env"
    if _patch_env_es_url(env_path, was_url, new_url):
        notes.append(str(env_path))

    if notes:
        print(
            "  \033[0;36m[INFO]\033[0m  Saved host Elasticsearch URL for you: "
            + ", ".join(notes),
            flush=True,
        )
        print(
            f"  \033[0;36m[INFO]\033[0m  ES_URL is now {new_url!r} (was Docker-only {was_url!r}).",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
