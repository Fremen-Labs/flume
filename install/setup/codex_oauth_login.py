#!/usr/bin/env python3
"""
Standalone OpenAI ChatGPT / Codex OAuth for Flume (no OpenClaw dependency).

Implements the same device-code flow as the official Codex CLI:
  https://github.com/openai/codex (codex-rs/login/src/device_code_auth.rs)

Also can import tokens from the official Codex CLI cache (~/.codex/auth.json).

Usage (from Flume repo root is recommended):
  python3 install/setup/codex_oauth_login.py login [--flume-root DIR]
  python3 install/setup/codex_oauth_login.py login-browser [--flume-root DIR]   # use if device login lacks API scopes
  python3 install/setup/codex_oauth_login.py login-paste [--port N] [--write-html FILE]   # headless: open URL elsewhere, paste redirect back
  python3 install/setup/codex_oauth_login.py import-codex [--codex-home DIR] [--flume-root DIR]

Environment:
  OPENAI_OAUTH_CLIENT_ID   Override OAuth client id (default: same as openai/codex CLI)
  OPENAI_OAUTH_SCOPES      Space-separated scopes for device login + token refresh.
                           Set to empty to omit scope from device/token requests (legacy).
  OPENAI_OAUTH_AUTHORIZE_SCOPES  Browser /oauth/authorize only (login-browser, login-paste).
                           Default matches codex-rs/login (connector scopes). Do not use model.request here.
  OPENAI_OAUTH_ORIGINATOR  Browser authorize URL (default: codex_cli_rs)
  OPENAI_OAUTH_RESOURCE    Optional: append resource=... to /oauth/authorize only (experimental).
                           Do NOT use on auth.openai.com token refresh — OpenAI returns unknown_parameter.
  SSL_CERT_FILE            Optional corporate CA bundle
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Public client id from openai/codex codex-rs/login/src/auth/manager.rs (same as `codex login`).
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_TOKEN_URL = f"{DEFAULT_ISSUER}/oauth/token"
USER_AGENT = "Flume/1.0 (codex-oauth-login; +https://github.com/Fremen-Labs/flume)"

# Device-code + refresh_token POST body: broader scopes (IdP may ignore or accept on token endpoint).
# Browser GET /oauth/authorize MUST use the allowlisted set from Codex — see DEFAULT_BROWSER_AUTHORIZE_SCOPES.
DEFAULT_OAUTH_SCOPES = (
    "openid profile email offline_access model.request api.model.read api.responses.write"
)

# openai/codex codex-rs/login/src/server.rs build_authorize_url — only these are valid on /oauth/authorize
# for client app_EMoamEEZ73f0CkXaXp7hrann. Requesting model.request (etc.) yields:
#   error=invalid_scope "not allowed to request scope 'model.request'"
DEFAULT_BROWSER_AUTHORIZE_SCOPES = (
    "openid profile email offline_access api.connectors.read api.connectors.invoke"
)


def _oauth_scopes_for_request() -> str | None:
    raw = os.environ.get("OPENAI_OAUTH_SCOPES")
    if raw is None:
        return DEFAULT_OAUTH_SCOPES
    s = str(raw).strip()
    return s or None


def _optional_authorize_resource() -> str | None:
    """
    RFC 8707 resource on /oauth/authorize only, and only if the user sets OPENAI_OAUTH_RESOURCE.
    auth.openai.com rejects `resource` on POST /oauth/token (refresh + code exchange).
    """
    s = os.environ.get("OPENAI_OAUTH_RESOURCE", "").strip()
    return s or None


def _browser_authorize_scopes() -> str:
    """
    Scopes for GET /oauth/authorize (login-browser, login-paste).

    This must match what OpenAI allows for the public Codex client — same string as
    codex-rs/login build_authorize_url. OPENAI_OAUTH_SCOPES is NOT used here (it includes
    model.request, which triggers invalid_scope on authorize).
    """
    raw = os.environ.get("OPENAI_OAUTH_AUTHORIZE_SCOPES")
    if raw is not None:
        s = str(raw).strip()
        return s if s else DEFAULT_BROWSER_AUTHORIZE_SCOPES
    return DEFAULT_BROWSER_AUTHORIZE_SCOPES


def _generate_pkce() -> tuple[str, str]:
    """Match openai/codex codex-rs/login/src/pkce.rs (verifier + S256 challenge)."""
    raw = os.urandom(64)
    code_verifier = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def _random_oauth_state() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def _build_browser_authorize_url(
    issuer: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str,
) -> str:
    originator = (os.environ.get("OPENAI_OAUTH_ORIGINATOR") or "codex_cli_rs").strip() or "codex_cli_rs"
    params = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("scope", scope),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
        ("id_token_add_organizations", "true"),
        ("codex_cli_simplified_flow", "true"),
        ("state", state),
        ("originator", originator),
    ]
    res = _optional_authorize_resource()
    if res:
        params.append(("resource", res))
    qs = urllib.parse.urlencode(params)
    return f"{issuer.rstrip('/')}/oauth/authorize?{qs}"


def _parse_pasted_oauth_redirect(raw: str) -> tuple[str, str]:
    """
    Extract (authorization_code, state) from a pasted browser URL or raw query string.

    After OAuth, the browser is redirected to e.g. http://localhost:PORT/auth/callback?code=...&state=...
    The page may fail to load on the machine with the browser; the user copies the full address bar URL.
    """
    s = raw.strip().strip('"').strip("'")
    if not s:
        raise ValueError("empty input")
    if "://" not in s:
        qstr = s.lstrip("?")
        q = urllib.parse.parse_qs(qstr, keep_blank_values=False)
    else:
        u = urllib.parse.urlparse(s)
        if u.query:
            q = urllib.parse.parse_qs(u.query, keep_blank_values=False)
        elif u.fragment and "code=" in u.fragment:
            q = urllib.parse.parse_qs(u.fragment, keep_blank_values=False)
        else:
            q = {}
    code = (q.get("code") or [""])[0].strip()
    st = (q.get("state") or [""])[0].strip()
    if not code:
        raise ValueError("could not find code= in pasted URL (copy the full address bar URL after login)")
    return code, st


def _decode_auth_openai_error_paste(raw: str) -> str | None:
    """
    If the user pastes https://auth.openai.com/error?payload=... (sign-in failed on OpenAI's site),
    decode the JWT-like payload and return a human-readable explanation. Otherwise return None.
    """
    s = raw.strip().strip('"').strip("'")
    if not s or "auth.openai.com" not in s or "/error" not in s:
        return None
    try:
        u = urllib.parse.urlparse(s)
        q = urllib.parse.parse_qs(u.query)
        payload_b64 = (q.get("payload") or [""])[0]
        if not payload_b64:
            return (
                "OpenAI returned an auth error page (no payload in URL).\n"
                "Check account status, subscription, and region; see help.openai.com."
            )
        payload_b64 = urllib.parse.unquote(payload_b64)
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        kind = str(obj.get("kind") or "")
        err_code = str(obj.get("errorCode") or "")
        req_id = str(obj.get("requestId") or "")
        lines = [
            "OpenAI sign-in failed (you pasted the auth.openai.com error URL, not the localhost redirect).",
            f"  Server says: kind={kind!r} errorCode={err_code!r} requestId={req_id!r}",
            "",
            "If errorCode is unknown_error, the authorize request is often rejected before redirect —",
            "for example redirect_uri must match OpenAI's allowlist for the Codex client.",
            "",
            "Flume uses port 1455 by default (same as the official Codex CLI:",
            "http://localhost:1455/auth/callback). Re-run login-paste without overriding the port,",
            "or set FLUME_OAUTH_PASTE_PORT=1455.",
            "",
            "After a successful login you should paste a URL starting with http://localhost:.../auth/callback?code=...",
        ]
        return "\n".join(lines)
    except Exception:
        return (
            "OpenAI returned an auth error page (payload could not be decoded).\n"
            "Try again; if it persists, contact help.openai.com with the request ID from the error page."
        )


def _oauth_callback_redirect_error(raw: str) -> str | None:
    """
    If the user pasted .../auth/callback?error=... (OAuth failed after redirect), return a message.
    """
    s = raw.strip().strip('"').strip("'")
    if "://" not in s:
        return None
    u = urllib.parse.urlparse(s)
    path = u.path or ""
    if "/auth/callback" not in path and not path.endswith("/callback"):
        return None
    q = urllib.parse.parse_qs(u.query, keep_blank_values=False)
    err = (q.get("error") or [""])[0].strip()
    if not err:
        return None
    desc = (q.get("error_description") or [""])[0].strip()
    desc_plain = urllib.parse.unquote_plus(desc) if desc else ""
    lines = [
        f"OAuth redirect returned error={err!r}",
        f"  {desc_plain}" if desc_plain else "",
        "",
    ]
    if err == "invalid_scope":
        lines.extend(
            [
                "The authorize URL requested a scope this client cannot use on /oauth/authorize.",
                "Flume defaults to the same scope string as the official Codex CLI (connector scopes).",
                "Upgrade to the latest Flume, or unset OPENAI_OAUTH_AUTHORIZE_SCOPES if you overrode it with model.request.",
            ]
        )
    return "\n".join(line for line in lines if line is not None)


def _exchange_localhost_authorization_code(
    issuer: str,
    client_id: str,
    authorization_code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict:
    """Browser/PKCE token exchange (redirect_uri is http://127.0.0.1:PORT/auth/callback)."""
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


def _resolve_oauth_callback_port(cli_port: int | None) -> int:
    """
    Port for http://localhost:PORT/auth/callback.

    Must match OpenAI's allowlist for client app_EMoamEEZ73f0CkXaXp7hrann — the official Codex CLI
    uses 1455 (codex-rs/login). Binding to port 0 / random ports often yields auth.openai.com unknown_error.
    """
    if cli_port is not None:
        if not (1024 <= cli_port <= 65535):
            raise SystemExit("--port must be between 1024 and 65535")
        return cli_port
    try:
        return int(os.environ.get("FLUME_OAUTH_PASTE_PORT", "1455"))
    except ValueError:
        return 1455


def _oauth_localhost_callback(expected_state: str, port: int) -> tuple[HTTPServer, str, dict[str, str]]:
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            del fmt, args

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            st = (q.get("state") or [""])[0]
            if st != expected_state:
                result.clear()
                result["error"] = "state_mismatch"
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"OAuth state mismatch")
                return
            if q.get("error"):
                err = (q.get("error") or [""])[0]
                desc = (q.get("error_description") or [""])[0]
                result.clear()
                result["error"] = err
                result["detail"] = desc
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<!doctype html><meta charset=utf-8><title>Flume</title>"
                    b"<p>Sign-in was not completed. You can close this window.</p>"
                )
                return
            code = (q.get("code") or [""])[0]
            if not code:
                result.clear()
                result["error"] = "missing_code"
                self.send_response(400)
                self.end_headers()
                return
            result.clear()
            result["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><meta charset=utf-8><title>Flume</title>"
                b"<p>Success. You can close this window and return to the terminal.</p>"
            )

    try:
        httpd = HTTPServer(("127.0.0.1", port), CallbackHandler)
    except OSError as e:
        raise SystemExit(
            f"Could not bind OAuth callback server on 127.0.0.1:{port}: {e}\n\n"
            "Port 1455 is the official Codex CLI default and must match OpenAI's redirect allowlist.\n"
            "Free the port (or stop the other process), then retry.\n"
            "For headless SSH: ssh -L 1455:127.0.0.1:1455 you@server then open the authorize URL on your laptop.\n"
            "Using a non-default port can cause auth.openai.com to show unknown_error on sign-in."
        ) from e
    # Match openai/codex redirect_uri shape (localhost, not 127.0.0.1 — IdP allowlists differ).
    redirect_uri = f"http://localhost:{port}/auth/callback"
    httpd.timeout = 2.0
    return httpd, redirect_uri, result


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
    body: dict = {"client_id": client_id}
    scp = _oauth_scopes_for_request()
    if scp:
        body["scope"] = scp
    code, body = _http_json("POST", url, json_body=body, timeout=60)
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
    scp = _oauth_scopes_for_request()
    if scp:
        form["scope"] = scp
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
    *,
    oauth_scopes_requested: str | None = None,
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
    if oauth_scopes_requested:
        state["oauth_scopes_requested"] = oauth_scopes_requested
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


def _jwt_access_token_scopes(access_token: str) -> tuple[bool, list[str]]:
    """Decode JWT `scp` / `roles` without verifying the signature (CLI hint only)."""
    t = (access_token or "").strip()
    if t.count(".") < 2:
        return False, []
    try:
        payload_b64 = t.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        scopes: list[str] = []
        scp = payload.get("scp")
        if isinstance(scp, str):
            scopes.extend(x for x in scp.split() if x)
        elif isinstance(scp, list):
            scopes.extend(str(x) for x in scp if x)
        roles = payload.get("roles")
        if isinstance(roles, list):
            scopes.extend(str(x) for x in roles if x)
        return True, scopes
    except Exception:
        return False, []


def _print_browser_oauth_route_hint(access: str) -> None:
    """After login-browser / login-paste, explain Flume routing and platform-key need for api.openai.com."""
    ok, scopes = _jwt_access_token_scopes(access)
    print()
    if ok and scopes and "api.responses.write" in scopes:
        print("Token includes api.responses.write — Flume can use OpenAI /v1/responses for chat-style calls.")
    elif ok and scopes:
        print(
            "Token has Codex connector scopes only (typical browser OAuth). "
            "Flume will try /v1/chat/completions, but OpenAI usually still requires **model.request** — "
            "which this OAuth flow cannot obtain. For Plan New Work / hosted GPT models, add an OpenAI **platform API key** "
            "(sk-…) in Settings → LLM → API Key (https://platform.openai.com/api-keys)."
        )
    elif ok and not scopes:
        print(
            "JWT has no scp list. If Flume returns 401 missing model.request or api.responses.write, use a platform sk- API key."
        )
    else:
        print(
            "Could not decode JWT scopes. If planner fails with missing scopes, use a platform sk- API key for api.openai.com calls."
        )


def _warn_device_login_responses_scope(access: str) -> None:
    """After device-code login, tell the user if /v1/responses will 401."""
    ok, scopes = _jwt_access_token_scopes(access)
    print()
    if not ok:
        print(
            "Note: Could not read API scopes from the access token (not a JWT or parse failed).\n"
            'If Flume returns "Missing scopes: api.responses.write", device login did not grant API access.\n'
            "Run:\n"
            "  ./flume codex-oauth login-browser\n"
            "  ./flume restart --all\n"
        )
        return
    if not scopes:
        print(
            "WARNING: Access token has no `scp` in the JWT. OpenAI may still reject /v1/responses.\n"
            "If Flume shows missing api.responses.write, run:\n"
            "  ./flume codex-oauth login-browser\n"
            "  ./flume restart --all\n"
        )
        return
    if "api.responses.write" not in scopes:
        shown = " ".join(scopes[:12])
        if len(scopes) > 12:
            shown += " …"
        print(
            "WARNING: Device-code login did NOT grant api.responses.write (required for Flume Plan New Work /v1/responses).\n"
            f"Decoded token scopes ({len(scopes)}): {shown}\n\n"
            "Use the browser flow instead (requests scopes on /oauth/authorize):\n"
            "  ./flume codex-oauth login-browser\n"
            "  ./flume restart --all\n"
        )


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
    print(
        "If you need the Flume planner (OpenAI /v1/responses), device login often lacks api.responses.write — "
        "use `login-browser` instead if you see 401 Missing scopes after this.\n"
    )
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
    _write_flume_state(
        state_path,
        access,
        refresh,
        client_id,
        expires_in,
        token_url,
        oauth_scopes_requested=_oauth_scopes_for_request() or "",
    )
    if args.sync_env:
        _merge_env(flume_root, state_path, token_url)
    _warn_device_login_responses_scope(access)
    print("Done. In Flume Settings choose OpenAI → Auth: OAuth, or restart the dashboard/workers.")


def cmd_login_browser(args: argparse.Namespace) -> None:
    """
    Full browser OAuth with localhost redirect (openai/codex login server pattern).
    Authorize URL uses the same scope string as codex-rs/login (connector scopes). Optional OPENAI_OAUTH_RESOURCE.
    """
    issuer = (args.issuer or DEFAULT_ISSUER).rstrip("/")
    client_id = (os.environ.get("OPENAI_OAUTH_CLIENT_ID") or "").strip() or DEFAULT_CLIENT_ID
    flume_root = _detect_flume_root(Path(args.flume_root) if args.flume_root else None)
    state_path = Path(args.state_file) if args.state_file else (flume_root / ".openai-oauth.json")
    if not state_path.is_absolute():
        state_path = flume_root / state_path

    port = _resolve_oauth_callback_port(args.port)
    oauth_state = _random_oauth_state()
    code_verifier, code_challenge = _generate_pkce()
    httpd, redirect_uri, callback_box = _oauth_localhost_callback(oauth_state, port)
    scope = _browser_authorize_scopes()
    auth_url = _build_browser_authorize_url(
        issuer, client_id, redirect_uri, code_challenge, oauth_state, scope
    )

    print()
    print("Flume — ChatGPT / Codex OAuth (browser login, localhost callback)")
    print()
    print(
        "Authorize URL uses the same scopes as the official Codex CLI (openid + connector scopes). "
        "OpenAI rejects model.request on /oauth/authorize — device-code login uses different scope rules."
    )
    print()
    print(
        f"Callback port {port} — same default as the official Codex CLI (1455). "
        "OpenAI rejects many other redirect ports (browser may show unknown_error)."
    )
    print("Remote server: the browser (or SSH tunnel) must reach THIS host’s loopback on that port,")
    print(f"  e.g.  ssh -L {port}:127.0.0.1:{port} you@this-host  then open the authorize URL on your laptop.")
    print()
    print(f"Listening on {redirect_uri}")
    _ores = _optional_authorize_resource()
    print(
        f"Authorize URL will include resource={_ores!r} (OPENAI_OAUTH_RESOURCE)."
        if _ores
        else "OPENAI_OAUTH_RESOURCE unset — authorize URL has no resource= (OpenAI token API rejects resource)."
    )
    print()
    print(f"Sign in here:\n\n  {auth_url}\n")
    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:
            pass
    print("Waiting for authorization (15 minutes max)...\n")

    deadline = time.monotonic() + 15 * 60
    try:
        while time.monotonic() < deadline:
            if callback_box.get("code") or callback_box.get("error"):
                break
            httpd.handle_request()
    finally:
        httpd.server_close()

    if callback_box.get("error"):
        err = str(callback_box.get("error") or "")
        detail = str(callback_box.get("detail") or "")
        detail_plain = urllib.parse.unquote_plus(detail.replace("+", " ")) if detail else ""
        msg = f"OAuth failed: {err}" + (f" — {detail_plain}" if detail_plain else "")
        if err == "invalid_scope":
            msg += (
                "\n\nTip: /oauth/authorize only allows Codex connector scopes (see Flume DEFAULT_BROWSER_AUTHORIZE_SCOPES). "
                "Unset OPENAI_OAUTH_AUTHORIZE_SCOPES if you pointed it at model.request / api.responses.write."
            )
        if err in ("unknown_error", "server_error") or "unknown_error" in detail_plain.lower():
            msg += (
                "\n\nTip: If you used a non-allowlisted redirect port, OpenAI often fails before redirect. "
                "Use default port 1455 (do not pass a random port). "
                "Free 1455 or set FLUME_OAUTH_PASTE_PORT=1455, then retry login-browser."
            )
        raise SystemExit(msg)

    auth_code = str(callback_box.get("code") or "").strip()
    if not auth_code:
        raise SystemExit("Timed out waiting for OAuth redirect (no authorization code).")

    tokens = _exchange_localhost_authorization_code(
        issuer, client_id, auth_code, code_verifier, redirect_uri
    )
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    if not access or not refresh:
        raise SystemExit(f"Token response missing access/refresh: {list(tokens.keys())}")

    expires_in = int(tokens.get("expires_in") or 3600)
    token_url = DEFAULT_TOKEN_URL
    _write_flume_state(
        state_path,
        access,
        refresh,
        client_id,
        expires_in,
        token_url,
        oauth_scopes_requested=scope,
    )
    if args.sync_env:
        _merge_env(flume_root, state_path, token_url)
    _print_browser_oauth_route_hint(access)
    print("\nDone. Run: ./flume restart --all")


def cmd_login_paste(args: argparse.Namespace) -> None:
    """
    Headless / remote server: print authorize URL (and optional HTML file), user opens it on any machine
    with a browser, then pastes the redirect URL from the address bar back into this terminal.

    Uses a fixed http://localhost:<port>/auth/callback redirect_uri (like OpenClaw-style manual capture).
    The browser will usually show "connection refused" after login — that is expected; copy the URL anyway.
    """
    issuer = (args.issuer or DEFAULT_ISSUER).rstrip("/")
    client_id = (os.environ.get("OPENAI_OAUTH_CLIENT_ID") or "").strip() or DEFAULT_CLIENT_ID
    flume_root = _detect_flume_root(Path(args.flume_root) if args.flume_root else None)
    state_path = Path(args.state_file) if args.state_file else (flume_root / ".openai-oauth.json")
    if not state_path.is_absolute():
        state_path = flume_root / state_path

    port = _resolve_oauth_callback_port(args.port)

    redirect_uri = f"http://localhost:{port}/auth/callback"
    oauth_state = _random_oauth_state()
    code_verifier, code_challenge = _generate_pkce()
    scope = _browser_authorize_scopes()
    auth_url = _build_browser_authorize_url(
        issuer, client_id, redirect_uri, code_challenge, oauth_state, scope
    )

    html_out = args.write_html
    if html_out:
        hp = Path(html_out)
        hp.parent.mkdir(parents=True, exist_ok=True)
        safe_href = html.escape(auth_url, quote=True)
        hp.write_text(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Flume — ChatGPT OAuth</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;line-height:1.5\">"
            "<h1>Flume OAuth (headless helper)</h1>"
            "<p>Click the link below and sign in. Your browser will try to open "
            f"<code>localhost:{port}</code> and may show an error — that is normal.</p>"
            "<p><strong>Copy the entire URL from the address bar</strong> (it contains <code>code=</code>) "
            "and paste it into the Flume server terminal where <code>login-paste</code> is waiting.</p>"
            f'<p><a href="{safe_href}">Sign in with ChatGPT / OpenAI</a></p>'
            "<hr><p style=\"font-size:0.85rem;color:#555\">If the link does not work, copy this URL:</p>"
            f"<pre style=\"white-space:pre-wrap;word-break:break-all;font-size:0.75rem\">{html.escape(auth_url)}</pre>"
            "</body></html>",
            encoding="utf-8",
        )
        print(f"Wrote HTML helper: {hp.resolve()}")

    print()
    print("Flume — ChatGPT / Codex OAuth (paste redirect — for headless servers)")
    print()
    print(
        "Authorize URL matches the official Codex CLI (openid + api.connectors.*). "
        "OpenAI does not allow model.request on /oauth/authorize — that produced invalid_scope on older Flume builds."
    )
    print()
    print(f"Redirect URI (must match what you paste later): {redirect_uri}")
    print(
        "(Port defaults to 1455 — same as the official Codex CLI; OpenAI often rejects other redirect ports.)"
    )
    print()
    _ores = _optional_authorize_resource()
    print(
        f"Authorize URL will include resource={_ores!r} (OPENAI_OAUTH_RESOURCE)."
        if _ores
        else "OPENAI_OAUTH_RESOURCE unset — authorize URL has no resource=."
    )
    print()
    print("1) On any computer with a browser, open this URL (copy/paste or use the HTML file):\n")
    print(auth_url)
    print()
    if html_out:
        print(f"   (HTML file with the same link: {Path(html_out).resolve()})")
    print()
    print(
        "2) After you sign in, the browser redirects to localhost on **that** computer. "
        "You may see \"Unable to connect\" — that is OK."
    )
    print("3) Copy the **full URL** from the address bar (starts with http://localhost:...) and paste it below.")
    print()
    try:
        raw = input("Paste redirect URL here, then Enter: ").strip()
    except EOFError:
        raise SystemExit("No input (stdin closed). Run interactively or pipe the redirect URL.")

    decoded_err = _decode_auth_openai_error_paste(raw)
    if decoded_err:
        raise SystemExit(decoded_err)

    cb_err = _oauth_callback_redirect_error(raw)
    if cb_err:
        raise SystemExit(cb_err)

    try:
        auth_code, pasted_state = _parse_pasted_oauth_redirect(raw)
    except ValueError as e:
        raise SystemExit(str(e))

    if not pasted_state:
        raise SystemExit(
            "Paste must include state= (copy the full redirect URL from the address bar, not only code=)."
        )

    if pasted_state != oauth_state:
        raise SystemExit(
            f"OAuth state mismatch (possible wrong paste or stale tab). Expected state ending …{oauth_state[-8:]}, "
            f"got {pasted_state!r}. Run login-paste again and use the fresh URL from this run only."
        )

    tokens = _exchange_localhost_authorization_code(
        issuer, client_id, auth_code, code_verifier, redirect_uri
    )
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    if not access or not refresh:
        raise SystemExit(f"Token response missing access/refresh: {list(tokens.keys())}")

    expires_in = int(tokens.get("expires_in") or 3600)
    token_url = DEFAULT_TOKEN_URL
    _write_flume_state(
        state_path,
        access,
        refresh,
        client_id,
        expires_in,
        token_url,
        oauth_scopes_requested=scope,
    )
    if args.sync_env:
        _merge_env(flume_root, state_path, token_url)
    print("\nOAuth state saved.")
    _print_browser_oauth_route_hint(access)
    print("Done. Run: ./flume restart --all")


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

    pb = sub.add_parser(
        "login-browser",
        help="Browser OAuth + localhost callback (Codex authorize scopes; use instead of device login for planner)",
    )
    pb.add_argument("--flume-root", type=str, default=None, help="Flume repository / package root")
    pb.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="Path for Flume .openai-oauth.json (default: <flume-root>/.openai-oauth.json)",
    )
    pb.add_argument("--issuer", type=str, default=None, help=f"Default: {DEFAULT_ISSUER}")
    pb.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not try to open a browser (print URL only)",
    )
    pb.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="Local callback http://localhost:N/auth/callback (default: FLUME_OAUTH_PASTE_PORT or 1455, Codex/OpenAI allowlist)",
    )
    pb.add_argument(
        "--no-sync-env",
        action="store_true",
        help="Do not merge LLM_* into .env (use OpenBao / Settings only)",
    )
    pb.set_defaults(func=cmd_login_browser, sync_env=True)

    pp = sub.add_parser(
        "login-paste",
        help="Headless: print authorize URL (optional HTML file); paste redirect URL from browser after login",
    )
    pp.add_argument("--flume-root", type=str, default=None, help="Flume repository / package root")
    pp.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="Path for Flume .openai-oauth.json (default: <flume-root>/.openai-oauth.json)",
    )
    pp.add_argument("--issuer", type=str, default=None, help=f"Default: {DEFAULT_ISSUER}")
    pp.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="Port in redirect_uri http://localhost:N/auth/callback (default: env FLUME_OAUTH_PASTE_PORT or 1455, Codex default)",
    )
    pp.add_argument(
        "--write-html",
        type=str,
        default=None,
        metavar="PATH",
        help="Write a small HTML page with a clickable sign-in link (scp to your laptop and open in a browser)",
    )
    pp.add_argument(
        "--no-sync-env",
        action="store_true",
        help="Do not merge LLM_* into .env (use OpenBao / Settings only)",
    )
    pp.set_defaults(func=cmd_login_paste, sync_env=True)

    pi = sub.add_parser("import-codex", help="Import tokens from official Codex CLI ~/.codex/auth.json")
    pi.add_argument("--codex-home", type=str, default="~/.codex", help="CODEX_HOME directory")
    pi.add_argument("--flume-root", type=str, default=None)
    pi.add_argument("--state-file", type=str, default=None)
    pi.add_argument("--no-sync-env", action="store_true")
    pi.set_defaults(func=cmd_import_codex, sync_env=True)

    args = p.parse_args()
    if getattr(args, "no_sync_env", False):
        args.sync_env = False
    args.func(args)


if __name__ == "__main__":
    main()
