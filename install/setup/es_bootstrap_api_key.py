#!/usr/bin/env python3
"""
Create an Elasticsearch API key using the elastic superuser password (one-time),
then save ES_API_KEY (and ES_URL) to OpenBao KV and/or .env.

Unattended (no prompts), for provisioning / new installs where the user never sees passwords:
  • ELASTIC_PASSWORD in the environment, or
  • FLUME_ELASTIC_PASSWORD_FILE pointing at a file, or
  • install/.elastic-admin.env in the Flume repo (ELASTIC_PASSWORD=... or a single-line secret)

Interactive fallback: TTY + no FLUME_NON_INTERACTIVE when no password is available from above.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
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


def _merge_env_file(env_path: Path, updates: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        matched = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                matched = True
        if not matched:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def _es_ssl_context() -> ssl.SSLContext:
    if os.environ.get("ES_VERIFY_TLS", "false").strip().lower() in ("1", "true", "yes", "on"):
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def _post_api_key(es_url: str, basic: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{es_url.rstrip('/')}/_security/api_key",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=_es_ssl_context(), timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _print_api_key_http_error(exc: urllib.error.HTTPError) -> None:
    detail = ""
    try:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
    except Exception:
        pass
    if exc.code == 401:
        print(
            "  \033[1;33m[WARN]\033[0m  That username/password was not accepted by Elasticsearch.",
            file=sys.stderr,
            flush=True,
        )
    elif exc.code == 403:
        print(
            "  \033[1;33m[WARN]\033[0m  Forbidden — this user may not create API keys. "
            "Use the elastic superuser or an admin account.",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"  \033[1;33m[WARN]\033[0m  Elasticsearch returned HTTP {exc.code}: {detail}",
            file=sys.stderr,
            flush=True,
        )


def _create_api_key(es_url: str, username: str, password: str) -> str:
    es_url = es_url.rstrip("/")
    basic = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    full_body = {
        "name": "flume-installer",
        "expiration": "365d",
        "role_descriptors": {
            "flume_install": {
                "cluster": ["monitor", "manage_index_templates", "manage_ilm"],
                "indices": [{"names": ["*"], "privileges": ["all"]}],
            }
        },
    }
    minimal_body: dict = {"name": "flume-installer"}
    payload: dict = {}
    for idx, body in enumerate((full_body, minimal_body)):
        try:
            payload = _post_api_key(es_url, basic, body)
            break
        except urllib.error.HTTPError as e:
            if e.code == 400 and idx == 0:
                try:
                    e.read()
                except Exception:
                    pass
                continue
            _print_api_key_http_error(e)
            raise SystemExit(1) from e
        except urllib.error.URLError as e:
            print(f"  \033[1;33m[WARN]\033[0m  Could not reach Elasticsearch: {e}", file=sys.stderr, flush=True)
            raise SystemExit(1) from e
    else:
        raise SystemExit(1)

    enc = str(payload.get("encoded") or "").strip()
    if not enc:
        i, k = payload.get("id"), payload.get("api_key")
        if i and k:
            enc = base64.b64encode(f"{i}:{k}".encode()).decode("ascii")
    if not enc:
        print("  \033[1;33m[WARN]\033[0m  Unexpected response from Elasticsearch (no API key).", file=sys.stderr, flush=True)
        raise SystemExit(1)
    return enc


def _read_elastic_password(workspace_root: Path) -> tuple[str, str]:
    """Username + password from env, FLUME_ELASTIC_PASSWORD_FILE, or install/.elastic-admin.env."""
    user = os.environ.get("ELASTIC_USERNAME", "elastic").strip() or "elastic"
    pw = os.environ.get("ELASTIC_PASSWORD", "").strip()
    pfile = os.environ.get("FLUME_ELASTIC_PASSWORD_FILE", "").strip()
    if not pfile:
        default_admin = workspace_root / "install" / ".elastic-admin.env"
        if default_admin.is_file():
            pfile = str(default_admin)
    if not pw and pfile:
        p = Path(pfile).expanduser()
        if p.is_file():
            raw = p.read_text(encoding="utf-8", errors="replace")
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("ELASTIC_PASSWORD="):
                    pw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
                if line.startswith("ELASTIC_USERNAME="):
                    user = line.split("=", 1)[1].strip().strip('"').strip("'") or user
            if not pw:
                pw = raw.strip()
    return user, pw


def _save_key_and_url(workspace_root: Path, es_url: str, encoded: str) -> bool:
    updates = {"ES_API_KEY": encoded, "ES_URL": es_url}
    _load_flume_config_openbao(workspace_root)
    src = workspace_root / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))
    saved: list[str] = []
    try:
        from dashboard.llm_settings import _openbao_enabled, _openbao_put_many

        ob_ok, _ = _openbao_enabled(workspace_root)
        if ob_ok and _openbao_put_many(workspace_root, updates):
            saved.append("OpenBao KV (secret/flume)")
    except Exception as exc:
        print(f"  \033[1;33m[WARN]\033[0m  OpenBao update skipped: {exc}", file=sys.stderr, flush=True)

    env_path = workspace_root / ".env"
    try:
        _merge_env_file(env_path, updates)
        saved.append(str(env_path))
    except OSError as exc:
        print(f"  \033[1;33m[WARN]\033[0m  Could not write .env: {exc}", file=sys.stderr, flush=True)

    if not saved:
        print(
            "  \033[1;33m[WARN]\033[0m  API key was created but could not be saved to OpenBao or .env.",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(f"  \033[0;32m[OK]\033[0m    Saved new API key to: {', '.join(saved)}", flush=True)
    return True


def _run_create_flow(workspace_root: Path, es_url: str, user: str, pw: str, *, quiet: bool) -> int:
    if not quiet:
        print("  \033[0;36m[INFO]\033[0m  Creating API key…", flush=True)
    encoded = _create_api_key(es_url, user, pw)
    del pw
    if not _save_key_and_url(workspace_root, es_url, encoded):
        return 1
    print("  \033[0;36m[INFO]\033[0m  Retrying connection…", flush=True)
    return 0


def main() -> int:
    wr = Path(os.environ.get("FLUME_WORKSPACE_ROOT", "")).resolve()
    es_url = (os.environ.get("ES_URL") or "").strip().rstrip("/")
    if not wr.is_dir() or not es_url:
        print("  \033[1;33m[WARN]\033[0m  Missing FLUME_WORKSPACE_ROOT or ES_URL.", file=sys.stderr, flush=True)
        return 1

    non_interactive = os.environ.get("FLUME_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes")

    user, pw = _read_elastic_password(wr)
    if pw:
        print("")
        print("  \033[0;36m── Elasticsearch API key (automated) ──\033[0m")
        print("  \033[0;36m[INFO]\033[0m  Using elastic password from the install environment (not shown).", flush=True)
        return _run_create_flow(wr, es_url, user, pw, quiet=True)

    if non_interactive:
        return 1

    if not sys.stdin.isatty():
        return 1

    print("")
    print("  \033[0;36m── Elasticsearch API key ──\033[0m")
    print("  Flume will create a dedicated API key for this cluster.")
    print("  If your server was set up for you, ask for the install bundle or run again with")
    print("  ELASTIC_PASSWORD set (or install/.elastic-admin.env written by provisioning).")
    print("")
    try:
        user = input("  Username [elastic]: ").strip() or "elastic"
    except EOFError:
        return 1
    try:
        pw = getpass.getpass("  Password (hidden): ")
    except EOFError:
        return 1
    if not pw:
        print("  \033[1;33m[WARN]\033[0m  No password entered.", file=sys.stderr, flush=True)
        return 1

    return _run_create_flow(wr, es_url, user, pw, quiet=False)


if __name__ == "__main__":
    raise SystemExit(main())
