# Flume LLM settings — provider catalog, .env persistence, OAuth refresh.
# Used by dashboard server for /api/settings/llm endpoints.

import json
import os
import re
import time
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import urllib.request
import urllib.error

# ─── Provider/model catalog (all major public frontier) ────────────────────────

PROVIDER_CATALOG = [
    {
        "id": "ollama",
        "name": "Ollama (local)",
        "baseUrlDefault": "http://127.0.0.1:11434",
        "authMode": "none",
        "models": [
            {"id": "llama3.2", "name": "Llama 3.2"},
            {"id": "llama3.2:1b", "name": "Llama 3.2 1B"},
            {"id": "llama3.2:3b", "name": "Llama 3.2 3B"},
            {"id": "mistral", "name": "Mistral 7B"},
            {"id": "codellama", "name": "Code Llama"},
            {"id": "qwen2.5-coder:7b", "name": "Qwen 2.5 Coder 7B"},
            {"id": "qwen2.5-coder:14b", "name": "Qwen 2.5 Coder 14B"},
            {"id": "phi3", "name": "Phi-3"},
            {"id": "gemma2", "name": "Gemma 2"},
            {"id": "deepseek-coder", "name": "DeepSeek Coder"},
        ],
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "baseUrlDefault": "https://api.openai.com",
        "authMode": "api_key_or_oauth",
        "models": [
            {"id": "gpt-4o", "name": "GPT-4o"},
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
            {"id": "gpt-4-turbo", "name": "GPT-4 Turbo"},
            {"id": "gpt-4", "name": "GPT-4"},
            {"id": "gpt-3.5-turbo", "name": "GPT-3.5 Turbo"},
            {"id": "o1", "name": "o1"},
            {"id": "o1-mini", "name": "o1 Mini"},
            {"id": "gpt-5.4", "name": "GPT-5.4 (Codex)"},
        ],
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "baseUrlDefault": "https://api.anthropic.com",
        "authMode": "api_key",
        "models": [
            {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet"},
            {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku"},
            {"id": "claude-3-opus-20240229", "name": "Claude 3 Opus"},
        ],
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "baseUrlDefault": "https://generativelanguage.googleapis.com/v1beta/openai",
        "authMode": "api_key",
        "models": [
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
            {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro"},
            {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash"},
        ],
    },
    {
        "id": "xai",
        "name": "xAI",
        "baseUrlDefault": "https://api.x.ai/v1",
        "authMode": "api_key",
        "models": [
            {"id": "grok-2", "name": "Grok 2"},
            {"id": "grok-2-mini", "name": "Grok 2 Mini"},
        ],
    },
    {
        "id": "mistral",
        "name": "Mistral AI",
        "baseUrlDefault": "https://api.mistral.ai",
        "authMode": "api_key",
        "models": [
            {"id": "mistral-large-latest", "name": "Mistral Large"},
            {"id": "codestral-latest", "name": "Codestral"},
            {"id": "mixtral-8x22b-2404", "name": "Mixtral 8x22B"},
        ],
    },
    {
        "id": "cohere",
        "name": "Cohere",
        "baseUrlDefault": "https://api.cohere.ai/v1",
        "authMode": "api_key",
        "models": [
            {"id": "command-r-plus", "name": "Command R+"},
            {"id": "command-r", "name": "Command R"},
            {"id": "command", "name": "Command"},
        ],
    },
    {
        "id": "openai_compatible",
        "name": "OpenAI-compatible (custom)",
        "baseUrlDefault": "",
        "authMode": "api_key",
        "models": [],  # User enters model ID
    },
]

VALID_PROVIDERS = {p["id"] for p in PROVIDER_CATALOG}
OAUTH_PROVIDERS = {"openai"}  # Only OpenAI supports OAuth Codex flow
SENSITIVE_KEYS = {"LLM_API_KEY", "GH_TOKEN", "ADO_TOKEN"}

# ─── .env load/save ────────────────────────────────────────────────────────────


def _env_file_path(workspace_root: Path) -> Path:
    return workspace_root / ".env"


def load_env_pairs(workspace_root: Path) -> dict[str, str]:
    path = _env_file_path(workspace_root)
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def _openbao_enabled(workspace_root: Path) -> tuple[bool, dict[str, str]]:
    pairs = load_env_pairs(workspace_root)
    if not shutil.which("openbao"):
        return False, pairs
    addr = str(pairs.get("OPENBAO_ADDR", "") or "").strip()
    token = str(pairs.get("OPENBAO_TOKEN", "") or "").strip()
    if not addr or not token:
        return False, pairs
    return True, pairs


def is_openbao_installed() -> bool:
    return bool(shutil.which("openbao"))


def _openbao_secret_ref(pairs: dict[str, str]) -> str:
    mount = str(pairs.get("OPENBAO_MOUNT", "secret") or "secret").strip().strip("/")
    path = str(pairs.get("OPENBAO_PATH", "flume") or "flume").strip().strip("/")
    return f"{mount}/{path}"


def _openbao_env(pairs: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    addr = str(pairs.get("OPENBAO_ADDR", "") or "").strip()
    token = str(pairs.get("OPENBAO_TOKEN", "") or "").strip()
    # Keep compatibility with both OpenBao/Hashi-style env names.
    env["BAO_ADDR"] = addr
    env["BAO_TOKEN"] = token
    env["VAULT_ADDR"] = addr
    env["VAULT_TOKEN"] = token
    return env


def _openbao_get_all(workspace_root: Path) -> dict[str, str]:
    enabled, pairs = _openbao_enabled(workspace_root)
    if not enabled:
        return {}
    try:
        secret_ref = _openbao_secret_ref(pairs)
        proc = subprocess.run(
            ["openbao", "kv", "get", "-format=json", secret_ref],
            capture_output=True,
            text=True,
            timeout=15,
            env=_openbao_env(pairs),
        )
        if proc.returncode != 0:
            return {}
        payload = json.loads(proc.stdout or "{}")
        data = payload.get("data", {}).get("data", {})
        return {str(k): str(v) for k, v in data.items() if v is not None}
    except Exception:
        return {}


def _openbao_put_many(workspace_root: Path, updates: dict[str, str]) -> bool:
    enabled, pairs = _openbao_enabled(workspace_root)
    if not enabled:
        return False
    try:
        existing = _openbao_get_all(workspace_root)
        merged = dict(existing)
        merged.update(updates)
        secret_ref = _openbao_secret_ref(pairs)
        cmd = ["openbao", "kv", "put", secret_ref]
        for k, v in merged.items():
            cmd.append(f"{k}={v}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            env=_openbao_env(pairs),
        )
        return proc.returncode == 0
    except Exception:
        return False


def load_effective_pairs(workspace_root: Path) -> dict[str, str]:
    """
    Load settings with OpenBao values overlaying .env for sensitive keys.
    Non-sensitive settings always come from .env.
    """
    pairs = load_env_pairs(workspace_root)
    bao_vals = _openbao_get_all(workspace_root)
    if not bao_vals:
        return pairs
    for key in SENSITIVE_KEYS:
        if key in bao_vals and str(bao_vals.get(key, "")).strip():
            pairs[key] = str(bao_vals[key]).strip()
    return pairs


def save_env_key(workspace_root: Path, key: str, value: str, *, create: bool = True) -> None:
    path = _env_file_path(workspace_root)
    lines = path.read_text().splitlines() if path.exists() else []
    key_eq = key + "="
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(key_eq) or line.strip().split("=")[0].strip() == key:
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(new_lines) + "\n")


def _update_env_keys(workspace_root: Path, updates: dict[str, str]) -> None:
    sensitive_updates = {k: v for k, v in updates.items() if k in SENSITIVE_KEYS}
    non_sensitive_updates = {k: v for k, v in updates.items() if k not in SENSITIVE_KEYS}

    # Prefer OpenBao for sensitive keys. If persisted there, clear .env values.
    if sensitive_updates and _openbao_put_many(workspace_root, sensitive_updates):
        for k in sensitive_updates.keys():
            non_sensitive_updates[k] = ""

    path = _env_file_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve order and comments by reading, then rewriting
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=")[0].strip()
            if k in non_sensitive_updates:
                new_lines.append(f"{k}={non_sensitive_updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in non_sensitive_updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    path.write_text("\n".join(new_lines) + "\n")


# ─── Route config → LLM_BASE_URL ──────────────────────────────────────────────


def build_base_url(
    route_type: str,
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    base_path: str = "",
    *,
    use_https: bool = False,
) -> str:
    """Build LLM_BASE_URL from route config. route_type: 'local' | 'network'."""
    scheme = "https" if use_https else "http"
    host = (host or "127.0.0.1").strip()
    if route_type == "local" and not host:
        host = "127.0.0.1"
    path = (base_path or "").strip().strip("/")
    if port is not None and port > 0:
        url = f"{scheme}://{host}:{port}"
    else:
        url = f"{scheme}://{host}"
    if path:
        url = url.rstrip("/") + "/" + path
    return url.rstrip("/")


# ─── Validation ────────────────────────────────────────────────────────────────


def validate_llm_settings(payload: dict[str, Any], workspace_root: Path) -> tuple[bool, str, dict[str, str]]:
    """
    Validate settings payload and return (ok, error_message, env_updates).
    env_updates is the dict of key=value to write to .env.
    """
    provider = str(payload.get("provider") or "ollama").strip().lower()
    if provider not in VALID_PROVIDERS:
        return False, f"Invalid provider: {provider}", {}

    model = str(payload.get("model") or "").strip()
    if not model:
        return False, "model is required", {}

    auth_mode = str(payload.get("authMode") or "api_key").strip().lower()
    if auth_mode not in ("api_key", "oauth"):
        auth_mode = "api_key"

    if auth_mode == "oauth" and provider not in OAUTH_PROVIDERS:
        return False, f"OAuth is not supported for provider: {provider}", {}

    route_type = str(payload.get("routeType") or "local").strip().lower()
    if route_type not in ("local", "network"):
        route_type = "local"

    host = str(payload.get("host") or "127.0.0.1").strip()
    port_val = payload.get("port")
    port = None
    if port_val is not None:
        try:
            p = int(port_val)
            if 1 <= p <= 65535:
                port = p
        except (TypeError, ValueError):
            pass

    base_path = str(payload.get("basePath") or "").strip()

    # For cloud providers (openai, anthropic, gemini, etc), base URL comes from catalog
    # unless routeType is network (custom endpoint)
    provider_obj = next((p for p in PROVIDER_CATALOG if p["id"] == provider), None)
    default_base = (provider_obj or {}).get("baseUrlDefault", "")

    if route_type == "network" and (host or port is not None):
        base_url = build_base_url(route_type, host or "127.0.0.1", port, base_path)
    elif provider == "ollama":
        base_url = build_base_url(route_type, host or "127.0.0.1", port, base_path)
    elif provider == "openai_compatible":
        custom_base = str(payload.get("baseUrl") or "").strip()
        if not custom_base:
            return False, "baseUrl is required for openai_compatible", {}
        base_url = custom_base.rstrip("/")
    else:
        base_url = default_base or ""

    updates = {
        "LLM_PROVIDER": provider,
        "LLM_MODEL": model,
        "LLM_BASE_URL": base_url,
    }

    if auth_mode == "api_key":
        api_key = str(payload.get("apiKey") or "").strip()
        updates["LLM_API_KEY"] = api_key
        # Clear OAuth state when using API key
        updates["OPENAI_OAUTH_STATE_FILE"] = ""
    else:
        # OAuth mode
        updates["LLM_API_KEY"] = ""  # Will be filled by refresh
        state_file = str(payload.get("oauthStateFile") or "").strip()
        if not state_file:
            state_file = str(workspace_root / ".openai-oauth.json")
        updates["OPENAI_OAUTH_STATE_FILE"] = state_file
        token_url = str(payload.get("oauthTokenUrl") or "https://auth.openai.com/oauth/token").strip()
        updates["OPENAI_OAUTH_TOKEN_URL"] = token_url

    if provider in ("openai", "anthropic", "gemini", "xai", "mistral", "cohere") and not base_url:
        updates["LLM_BASE_URL"] = ""  # Use provider default (llm_client uses _PROVIDER_BASE_URLS)

    return True, "", updates


# ─── OAuth refresh ─────────────────────────────────────────────────────────────


def do_oauth_refresh(workspace_root: Path) -> tuple[bool, str, Optional[dict]]:
    """
    Refresh OAuth token and update .env with new access token.
    Returns (ok, message, optional_state).
    """
    pairs = load_effective_pairs(workspace_root)
    state_file = pairs.get("OPENAI_OAUTH_STATE_FILE", "").strip()
    if not state_file:
        state_path = workspace_root / ".openai-oauth.json"
    else:
        state_path = Path(state_file)
        if not state_path.is_absolute():
            state_path = workspace_root / state_path

    if not state_path.exists():
        return False, "OAuth state file not found", None

    try:
        state = json.loads(state_path.read_text())
    except Exception as e:
        return False, f"Invalid OAuth state file: {e}", None

    refresh_token = str(state.get("refresh") or "").strip()
    client_id = str(state.get("client_id") or "").strip()
    if not refresh_token or not client_id:
        return False, "OAuth state missing refresh_token or client_id", None

    token_url = pairs.get("OPENAI_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token").strip()
    if not token_url:
        token_url = "https://auth.openai.com/oauth/token"

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    req = urllib.request.Request(
        token_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        return False, f"OAuth refresh failed: {e.code} {body}", None
    except Exception as e:
        return False, f"OAuth refresh failed: {e}", None

    new_access = str(data.get("access_token") or "").strip()
    if not new_access:
        return False, "OAuth response missing access_token", None

    now_ms = int(time.time() * 1000)
    state["access"] = new_access
    if data.get("refresh_token"):
        state["refresh"] = data["refresh_token"]
    expires_in = int(data.get("expires_in") or 0)
    if expires_in > 0:
        state["expires"] = now_ms + (expires_in * 1000)
    state_path.write_text(json.dumps(state, indent=2))

    save_env_key(workspace_root, "LLM_API_KEY", new_access)
    save_env_key(workspace_root, "LLM_PROVIDER", "openai")

    return True, "Token refreshed", {
        "access": new_access[:20] + "...",
        "expires_in": expires_in,
    }


def get_oauth_status(workspace_root: Path) -> dict[str, Any]:
    pairs = load_effective_pairs(workspace_root)
    state_file = pairs.get("OPENAI_OAUTH_STATE_FILE", "").strip()
    if not state_file:
        state_path = workspace_root / ".openai-oauth.json"
    else:
        state_path = Path(state_file)
        if not state_path.is_absolute():
            state_path = workspace_root / state_path

    if not state_path.exists():
        return {"configured": False, "message": "OAuth state file not found"}

    try:
        state = json.loads(state_path.read_text())
    except Exception:
        return {"configured": False, "message": "Invalid OAuth state file"}

    has_refresh = bool(str(state.get("refresh") or "").strip())
    has_access = bool(str(state.get("access") or "").strip())
    client_id = str(state.get("client_id") or "").strip()
    expires = int(state.get("expires") or 0)
    now_ms = int(time.time() * 1000)
    expires_in_sec = max(0, (expires - now_ms) // 1000) if expires else 0

    return {
        "configured": has_refresh and client_id,
        "hasAccessToken": has_access,
        "clientId": client_id[:20] + "..." if len(client_id) > 20 else client_id,
        "expiresInSeconds": expires_in_sec,
    }


def get_llm_settings_response(workspace_root: Path) -> dict[str, Any]:
    """Build full GET /api/settings/llm response: catalog, current settings, oauth status."""
    pairs = load_effective_pairs(workspace_root)
    provider = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    model = pairs.get("LLM_MODEL", "llama3.2").strip()
    base_url = pairs.get("LLM_BASE_URL", "").strip()
    api_key_set = bool(pairs.get("LLM_API_KEY", "").strip())
    oauth_state = pairs.get("OPENAI_OAUTH_STATE_FILE", "").strip()
    oauth_token_url = pairs.get("OPENAI_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token").strip()

    auth_mode = "oauth" if (oauth_state and provider == "openai" and not api_key_set) else "api_key"

    route_type = "local"
    host = "127.0.0.1"
    port = None
    base_path = ""
    if base_url and provider in ("ollama", "openai_compatible"):
        try:
            from urllib.parse import urlparse
            p = urlparse(base_url)
            host = p.hostname or "127.0.0.1"
            port = p.port
            base_path = (p.path or "").strip("/")
            route_type = "network" if host not in ("127.0.0.1", "localhost") else "local"
        except Exception:
            pass

    oauth_status = get_oauth_status(workspace_root) if provider == "openai" else {}

    return {
        "catalog": PROVIDER_CATALOG,
        "settings": {
            "provider": provider,
            "model": model,
            "baseUrl": base_url,
            "authMode": auth_mode,
            "routeType": route_type,
            "host": host,
            "port": port,
            "basePath": base_path,
            "apiKey": "***" if api_key_set and auth_mode == "api_key" else "",
            "oauthStateFile": oauth_state or str(workspace_root / ".openai-oauth.json"),
            "oauthTokenUrl": oauth_token_url,
        },
        "oauthStatus": oauth_status,
        "restartRequired": True,
        "openbaoInstalled": is_openbao_installed(),
    }
