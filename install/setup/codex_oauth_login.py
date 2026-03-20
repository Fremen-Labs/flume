#!/usr/bin/env python3
"""
Standalone OpenAI ChatGPT / Codex OAuth for Flume (no OpenClaw dependency).

Implements the same device-code flow as the official Codex CLI:
  https://github.com/openai/codex (codex-rs/login/src/device_code_auth.rs)

Also can import tokens from the official Codex CLI cache (~/.codex/auth.json).

Usage (from Flume repo root is recommended):
  python3 install/setup/codex_oauth_login.py login [--flume-root DIR]
  python3 install/setup/codex_oauth_login.py import-codex [--codex-home DIR] [--flume-root DIR]

Environment:
  OPENAI_OAUTH_CLIENT_ID   Override OAuth client id (default: same as openai/codex CLI)
  SSL_CERT_FILE            Optional corporate CA bundle
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Public client id from openai/codex codex-rs/login/src/auth/manager.rs (same as `codex login`).
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_TOKEN_URL = f"{DEFAULT_ISSUER}/oauth/token"
USER_AGENT = "Flume/1.0 (codex-oauth-login; +https://github.com/Fremen-Labs/flume)"


def _client_id_from_jwt(jwt_token: str) -> str:
    if jwt_token.count(".") < 2:
        return ""
    try:
        payload = jwt_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return str(obj.get("client_id") or "").strip()
    except Exception:
        return ""


def _detect_flume_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    here = Path(__file__).resolve().parent
    # install/setup/this.py -> repo root is parent.parent
    cand = here.parent.parent
    if (cand / "src" / "dashboard").is_dir() or (cand / "src").is_dir():
        return cand
    # package: setup/this.py -> root is parent
    cand2 = here.parent
    if (cand2 / "dashboard").is_dir():
        return cand2
    return Path.cwd().resolve()


def _http_json(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    form_body: dict | None = None,
    timeout: float = 120,
) -> tuple[int, dict | str]:
    ctx = ssl.create_default_context()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode()
            code = resp.getcode()
            try:
                return code, json.loads(raw)
            except json.JSONDecodeError:
                return code, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def _request_user_code(issuer: str, client_id: str) -> dict:
    api = f"{issuer.rstrip('/')}/api/accounts"
    url = f"{api}/deviceauth/usercode"
    code, body = _http_json("POST", url, json_body={"client_id": client_id}, timeout=60)
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"device usercode failed ({code}): {body}")
    return body


def _poll_device_token(
    issuer: str,
    device_auth_id: str,
    user_code: str,
    interval_sec: int,
) -> dict:
    api = f"{issuer.rstrip('/')}/api/accounts"
    url = f"{api}/deviceauth/token"
    deadline = time.monotonic() + 15 * 60
    interval = max(1, int(interval_sec) or 5)
    while time.monotonic() < deadline:
        code, body = _http_json(
            "POST",
            url,
            json_body={"device_auth_id": device_auth_id, "user_code": user_code},
            timeout=60,
        )
        if code == 200 and isinstance(body, dict):
            return body
        if code in (403, 404):
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            continue
        raise SystemExit(f"device token poll failed ({code}): {body}")
    raise SystemExit("Device authorization timed out (15 minutes).")


def _exchange_authorization_code(
    issuer: str,
    client_id: str,
    authorization_code: str,
    code_verifier: str,
) -> dict:
    redirect_uri = f"{issuer.rstrip('/')}/deviceauth/callback"
    form = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    url = f"{issuer.rstrip('/')}/oauth/token"
    code, body = _http_json("POST", url, form_body=form, timeout=60)
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"token exchange failed ({code}): {body}")
    return body


def _write_flume_state(
    state_path: Path,
    access: str,
    refresh: str,
    client_id: str,
    expires_in: int,
    token_url: str,
) -> None:
    now_ms = int(time.time() * 1000)
    exp = now_ms + int(expires_in) * 1000 if expires_in > 0 else 0
    state = {
        "provider": "openai-codex-oauth",
        "access": access,
        "refresh": refresh,
        "expires": exp,
        "client_id": client_id,
        "token_url": token_url,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        state_path.chmod(0o600)
    except OSError:
        pass
    print(f"Wrote Flume OAuth state: {state_path}")


def _merge_env(flume_root: Path, state_path: Path, token_url: str) -> None:
    env_path = flume_root / ".env"
    if not env_path.is_file():
        print(f"No {env_path} — set OPENAI_OAUTH_STATE_FILE in Settings or create .env.")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    access = str(state.get("access") or "").strip()
    if not access:
        return
    lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    updates = {
        "LLM_PROVIDER": "openai",
        "LLM_API_KEY": access,
        "OPENAI_OAUTH_STATE_FILE": str(state_path),
        "OPENAI_OAUTH_TOKEN_URL": str(state.get("token_url") or token_url),
    }
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if "=" not in line or line.strip().startswith("#"):
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Updated {env_path}")


def cmd_login(args: argparse.Namespace) -> None:
    issuer = (args.issuer or DEFAULT_ISSUER).rstrip("/")
    client_id = (os.environ.get("OPENAI_OAUTH_CLIENT_ID") or "").strip() or DEFAULT_CLIENT_ID
    flume_root = _detect_flume_root(Path(args.flume_root) if args.flume_root else None)
    state_path = Path(args.state_file) if args.state_file else (flume_root / ".openai-oauth.json")
    if not state_path.is_absolute():
        state_path = flume_root / state_path

    uc = _request_user_code(issuer, client_id)
    device_auth_id = str(uc.get("device_auth_id") or "").strip()
    user_code = str(uc.get("user_code") or uc.get("usercode") or "").strip()
    interval_raw = str(uc.get("interval") or "5").strip()
    try:
        interval = int(interval_raw)
    except ValueError:
        interval = 5
    if not device_auth_id or not user_code:
        raise SystemExit(f"Unexpected usercode response: {uc}")

    verify_url = f"{issuer}/codex/device"
    print()
    print("Flume — ChatGPT / Codex OAuth (standalone, same flow as `codex login --device-auth`)")
    print()
    print(f"1. Open in your browser and sign in:\n   {verify_url}\n")
    print(f"2. Enter this one-time code:\n   {user_code}\n")
    print("(Never share this code — it grants account access.)\n")
    print("Waiting for authorization...\n")

    poll = _poll_device_token(issuer, device_auth_id, user_code, interval)
    auth_code = str(poll.get("authorization_code") or "").strip()
    code_verifier = str(poll.get("code_verifier") or "").strip()
    if not auth_code or not code_verifier:
        raise SystemExit(f"Unexpected poll response: {poll}")

    tokens = _exchange_authorization_code(issuer, client_id, auth_code, code_verifier)
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    if not access or not refresh:
        raise SystemExit(f"Token response missing access/refresh: {list(tokens.keys())}")

    expires_in = int(tokens.get("expires_in") or 3600)
    token_url = DEFAULT_TOKEN_URL
    _write_flume_state(state_path, access, refresh, client_id, expires_in, token_url)
    if args.sync_env:
        _merge_env(flume_root, state_path, token_url)
    print("\nDone. In Flume Settings choose OpenAI → Auth: OAuth, or restart the dashboard/workers.")


def cmd_import_codex(args: argparse.Namespace) -> None:
    codex_home = Path(os.path.expanduser(args.codex_home)).resolve()
    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        raise SystemExit(f"Codex auth file not found: {auth_path}\nRun: codex login")

    data = json.loads(auth_path.read_text(encoding="utf-8"))
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    if not refresh:
        raise SystemExit("No refresh_token in Codex auth.json — use ChatGPT login in Codex (not API-key-only).")

    client_id = _client_id_from_jwt(access) or _client_id_from_jwt(refresh)
    if not client_id:
        client_id = (os.environ.get("OPENAI_OAUTH_CLIENT_ID") or "").strip() or DEFAULT_CLIENT_ID

    flume_root = _detect_flume_root(Path(args.flume_root) if args.flume_root else None)
    state_path = Path(args.state_file) if args.state_file else (flume_root / ".openai-oauth.json")
    if not state_path.is_absolute():
        state_path = flume_root / state_path

    token_url = DEFAULT_TOKEN_URL
    expires_in = 3600
    if access:
        try:
            payload = access.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
            exp = int(claims.get("exp") or 0)
            if exp:
                expires_in = max(0, exp - int(time.time()))
        except Exception:
            pass

    _write_flume_state(state_path, access, refresh, client_id, expires_in, token_url)
    if args.sync_env:
        _merge_env(flume_root, state_path, token_url)
    print("Imported Codex CLI tokens into Flume OAuth state.")


def main() -> None:
    p = argparse.ArgumentParser(description="Flume standalone Codex / ChatGPT OAuth")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="Device-code login (no OpenClaw, no Codex CLI required)")
    pl.add_argument("--flume-root", type=str, default=None, help="Flume repository / package root")
    pl.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="Path for Flume .openai-oauth.json (default: <flume-root>/.openai-oauth.json)",
    )
    pl.add_argument("--issuer", type=str, default=None, help=f"Default: {DEFAULT_ISSUER}")
    pl.add_argument(
        "--no-sync-env",
        action="store_true",
        help="Do not merge LLM_* into .env (use OpenBao / Settings only)",
    )
    pl.set_defaults(func=cmd_login, sync_env=True)

    pi = sub.add_parser("import-codex", help="Import tokens from official Codex CLI ~/.codex/auth.json")
    pi.add_argument("--codex-home", type=str, default="~/.codex", help="CODEX_HOME directory")
    pi.add_argument("--flume-root", type=str, default=None)
    pi.add_argument("--state-file", type=str, default=None)
    pi.add_argument("--no-sync-env", action="store_true")
    pi.set_defaults(func=cmd_import_codex, sync_env=True)

    args = p.parse_args()
    if hasattr(args, "no_sync_env") and args.no_sync_env:
        args.sync_env = False
    args.func(args)


if __name__ == "__main__":
    main()
