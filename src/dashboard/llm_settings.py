# Flume LLM settings — provider catalog, .env persistence, OAuth refresh.
# Used by dashboard server for /api/settings/llm endpoints.

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from flume_secrets import resolve_oauth_state_path
from openai_oauth_state import load_state_from_env_or_file, save_state_to_env_or_file
from typing import Any, Optional

import llm_credentials_store

import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_OPENAI_OAUTH_SCOPES = (
    'openid profile email offline_access model.request api.model.read api.responses.write'
)


def _openai_oauth_refresh_scopes() -> str | None:
    raw = os.environ.get('OPENAI_OAUTH_SCOPES')
    if raw is None:
        return _DEFAULT_OPENAI_OAUTH_SCOPES
    s = str(raw).strip()
    return s or None


def _decode_access_token_for_oauth_ui(access_token: str) -> dict[str, Any]:
    """Decode JWT claims without verifying signature (Settings UI / diagnostics only)."""
    t = (access_token or '').strip()
    out: dict[str, Any] = {
        'jwt_like': t.count('.') >= 2,
        'parsed': False,
        'scopes': [],
        'audience': '',
    }
    if not out['jwt_like']:
        return out
    try:
        payload_b64 = t.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        out['parsed'] = True
        aud = payload.get('aud')
        if isinstance(aud, str):
            out['audience'] = aud
        elif isinstance(aud, list) and aud:
            out['audience'] = str(aud[0])
        scopes: list[str] = []
        scp_raw = payload.get('scp')
        if isinstance(scp_raw, str):
            scopes.extend(x for x in scp_raw.split() if x)
        elif isinstance(scp_raw, list):
            scopes.extend(str(x) for x in scp_raw if x)
        roles = payload.get('roles')
        if isinstance(roles, list):
            scopes.extend(str(x) for x in roles if x)
        out['scopes'] = scopes
    except Exception:
        out['parsed'] = False
    return out


def _oauth_scope_status(dec: dict[str, Any], has_access: bool) -> str:
    """
    ok | missing_responses_write | jwt_no_scp | opaque_or_unknown | no_token
    """
    if not has_access:
        return 'no_token'
    if not dec.get('jwt_like'):
        return 'opaque_or_unknown'
    if not dec.get('parsed'):
        return 'opaque_or_unknown'
    scopes = dec.get('scopes') or []
    if not scopes:
        return 'jwt_no_scp'
    if 'api.responses.write' not in scopes:
        return 'missing_responses_write'
    return 'ok'

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
            {"id": "gpt-5.3", "name": "GPT-5.3 (Codex)"},
            {"id": "gpt-5.2", "name": "GPT-5.2 (Codex)"},
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
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash (recommended)"},
            {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash-Lite"},
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
            {"id": "gemini-flash-latest", "name": "Gemini Flash (latest alias)"},
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
SENSITIVE_KEYS = {"LLM_API_KEY", "GH_TOKEN", "ADO_TOKEN", "ES_API_KEY", "OPENAI_OAUTH_STATE_JSON"}


def _dedupe_models(models: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in models:
        mid = str((raw or {}).get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "name": str((raw or {}).get("name") or mid).strip() or mid})
    return out


def _normalize_ollama_base_url(base_url: str) -> str:
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
        path = (parsed.path or "").rstrip("/")
        if path in ("/v1", "/api"):
            parsed = parsed._replace(path="")
            return urllib.parse.urlunparse(parsed).rstrip("/")
    except Exception:
        pass
    if raw.endswith("/v1"):
        return raw[:-3].rstrip("/")
    if raw.endswith("/api"):
        return raw[:-4].rstrip("/")
    return raw


def _host_is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in {"", "127.0.0.1", "localhost", "::1"}


def resolve_effective_ollama_base_url(pairs: dict[str, str]) -> str:
    """
    Prefer explicit saved LLM_BASE_URL, but on clean installs fall back to LOCAL_OLLAMA_BASE_URL
    (which is usually populated by flume start / docker-compose as .../v1).
    If LLM_BASE_URL is still a loopback default while LOCAL_OLLAMA_BASE_URL points remote,
    prefer the remote LOCAL_OLLAMA_BASE_URL so Settings reflects the real reachable Ollama.
    """
    llm_base = _normalize_ollama_base_url(pairs.get("LLM_BASE_URL", ""))
    local_base = _normalize_ollama_base_url(pairs.get("LOCAL_OLLAMA_BASE_URL", ""))
    if not llm_base:
        return local_base
    if not local_base:
        return llm_base
    try:
        llm_host = urllib.parse.urlparse(llm_base).hostname or ""
        local_host = urllib.parse.urlparse(local_base).hostname or ""
        if _host_is_loopback(llm_host) and not _host_is_loopback(local_host):
            return local_base
    except Exception:
        pass
    return llm_base


def _fetch_ollama_models(base_url: str, timeout: float = 5.0) -> list[dict[str, str]]:
    base = _normalize_ollama_base_url(base_url)
    if not base:
        return []

    def _load(url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)

    model_rows: list[dict[str, str]] = []
    errs: list[str] = []
    for suffix, key in (("/api/tags", "models"), ("/v1/models", "data")):
        try:
            payload = _load(base + suffix)
            for item in payload.get(key) or []:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("name") or item.get("id") or "").strip()
                if mid:
                    model_rows.append({"id": mid, "name": mid})
            if model_rows:
                break
        except Exception as exc:
            errs.append(str(exc))
    if model_rows:
        return _dedupe_models(model_rows)
    return []


def provider_catalog_for_workspace(workspace_root: Path) -> list[dict[str, Any]]:
    pairs = load_effective_pairs(workspace_root)
    provider = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    model = pairs.get("LLM_MODEL", "llama3.2").strip() or "llama3.2"
    base_url = resolve_effective_ollama_base_url(pairs) if provider == "ollama" else pairs.get("LLM_BASE_URL", "").strip()

    catalog: list[dict[str, Any]] = []
    for entry in PROVIDER_CATALOG:
        copied = dict(entry)
        copied["models"] = [dict(m) for m in entry.get("models") or []]
        catalog.append(copied)

    if provider == "ollama":
        for entry in catalog:
            if entry.get("id") != "ollama":
                continue
            live_models = _fetch_ollama_models(base_url or str(entry.get("baseUrlDefault") or ""))
            merged = list(live_models) + [dict(m) for m in entry.get("models") or []]
            if model:
                merged.append({"id": model, "name": model})
            entry["models"] = _dedupe_models(merged)
            break

    return catalog

# ─── .env load/save ────────────────────────────────────────────────────────────


def _env_file_path(workspace_root: Path) -> Path:
    """
    File we mutate on Settings save.

    load_env_pairs merges workspace .env then repo-root .env with **parent winning** on duplicate
    keys. Writing only workspace/.env would leave repo-root LLM_* (e.g. llama3.2) overriding saves.
    """
    wr = workspace_root.resolve()
    parent_env = wr.parent / ".env"
    if parent_env.is_file():
        return parent_env
    return wr / ".env"


def _parse_env_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def load_env_pairs(workspace_root: Path) -> dict[str, str]:
    """Load workspace .env then repo-root .env (repo root wins for duplicate keys)."""
    out: dict[str, str] = {}
    wr = workspace_root.resolve()
    for base in (wr, wr.parent):
        path = base / ".env"
        if path.is_file():
            try:
                out.update(_parse_env_lines(path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return out


def _merge_openbao_connection_from_env(pairs: dict[str, str]) -> dict[str, str]:
    """Use process env + token file so Settings works after server hydrated from OpenBao."""
    for k in ("OPENBAO_ADDR", "OPENBAO_TOKEN", "OPENBAO_MOUNT", "OPENBAO_PATH", "OPENBAO_TOKEN_FILE"):
        v = os.environ.get(k, "").strip()
        if v:
            pairs[k] = v
    tf = pairs.get("OPENBAO_TOKEN_FILE", "").strip()
    if tf and not pairs.get("OPENBAO_TOKEN", "").strip():
        p = Path(tf).expanduser()
        if p.is_file():
            try:
                pairs["OPENBAO_TOKEN"] = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
    return pairs


def _openbao_enabled(workspace_root: Path) -> tuple[bool, dict[str, str]]:
    pairs = _merge_openbao_connection_from_env(load_env_pairs(workspace_root))
    addr = str(pairs.get("OPENBAO_ADDR", "") or "").strip()
    token = str(pairs.get("OPENBAO_TOKEN", "") or "").strip()
    if not addr or not token:
        return False, pairs
    
    # Natively resolve 'openbao' hostname to '127.0.0.1' if running outside Docker
    if "openbao" in addr:
        import urllib.request
        try:
            urllib.request.urlopen(addr.replace("openbao", "127.0.0.1") + "/v1/sys/health", timeout=1)
            addr = addr.replace("openbao", "127.0.0.1")
            pairs["OPENBAO_ADDR"] = addr
        except Exception:
            pass
            
    return True, pairs


def is_openbao_installed() -> bool:
    import os
    import urllib.request
    _DEFAULT_VAULT = 'http://localhost:8200' if os.environ.get('FLUME_NATIVE_MODE') == '1' else 'http://openbao:8200'
    addr = os.environ.get("OPENBAO_ADDR", _DEFAULT_VAULT).rstrip("/")
    try:
        urllib.request.urlopen(f"{addr}/v1/sys/health", timeout=1.5)
        return True
    except Exception:
        return False


def _openbao_secret_ref(pairs: dict[str, str]) -> str:
    mount = str(pairs.get("OPENBAO_MOUNT", "secret") or "secret").strip().strip("/")
    path = str(pairs.get("OPENBAO_PATH", "flume") or "flume").strip().strip("/")
    return f"{mount}/data/{path}"


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
        import urllib.request
        import json
        addr = pairs["OPENBAO_ADDR"].rstrip("/")
        token = pairs["OPENBAO_TOKEN"]
        secret_url = f"{addr}/v1/{_openbao_secret_ref(pairs)}"
        
        req = urllib.request.Request(secret_url, headers={"X-Vault-Token": token})
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read())
            data = payload.get("data", {}).get("data", {})
            return {str(k): str(v) for k, v in data.items() if v is not None}
    except Exception:
        return {}


def _openbao_put_many(workspace_root: Path, updates: dict[str, str]) -> bool:
    enabled, pairs = _openbao_enabled(workspace_root)
    if not enabled:
        return False
    try:
        import urllib.request
        import json
        existing = _openbao_get_all(workspace_root)
        merged = dict(existing)
        oauth_path_set = str(updates.get("OPENAI_OAUTH_STATE_FILE", "") or "").strip()
        for k, v in updates.items():
            if k == "LLM_API_KEY" and not str(v or "").strip() and oauth_path_set:
                continue
            merged[k] = v
            
        addr = pairs["OPENBAO_ADDR"].rstrip("/")
        token = pairs["OPENBAO_TOKEN"]
        secret_url = f"{addr}/v1/{_openbao_secret_ref(pairs)}"
        
        payload = json.dumps({"data": merged}).encode("utf-8")
        req = urllib.request.Request(
            secret_url, 
            data=payload, 
            headers={"X-Vault-Token": token, "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status in (200, 201, 204)
    except Exception:
        return False


# Do not let a stale interactive shell (or old exports) override .env / OpenBao for LLM.
# Worker processes are often started from a login shell that still has LLM_PROVIDER=ollama, etc.
_SKIP_PROCESS_OVERLAY_FOR_LLM = frozenset(
    {
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "OPENAI_OAUTH_STATE_FILE",
        "OPENAI_OAUTH_TOKEN_URL",
    }
)

# Repo tokens from .env / OpenBao must not be overridden by a stale GH_TOKEN in the shell
# or systemd environment left over from an old session.
_REPO_CREDS_FROM_FILE_FIRST = frozenset({"GH_TOKEN", "ADO_TOKEN", "ADO_ORG_URL"})
_LOCAL_PROVIDER_ROUTE_KEYS = frozenset({"LOCAL_OLLAMA_BASE_URL", "LOCAL_EXO_BASE_URL"})


def load_effective_pairs(workspace_root: Path) -> dict[str, str]:
    """
    Load settings: .env (optional), selected process env keys, then live OpenBao KV.
    Values from OpenBao override earlier sources.

    LLM-related keys are taken from .env + OpenBao only (not from inherited process env),
    so worker manager/handlers match Settings even when the parent shell has old exports.
    """
    pairs = _merge_openbao_connection_from_env(load_env_pairs(workspace_root))
    try:
        from flume_secrets import FLUME_ENV_KEYS

        for key in FLUME_ENV_KEYS:
            if key in _SKIP_PROCESS_OVERLAY_FOR_LLM:
                continue
            v = os.environ.get(key, "").strip()
            if not v:
                continue
            if key in _REPO_CREDS_FROM_FILE_FIRST:
                if str(pairs.get(key, "") or "").strip():
                    continue
            pairs[key] = v
    except ImportError:
        pass

    # Containerized clean installs often export provider-local routes (e.g. LOCAL_OLLAMA_BASE_URL)
    # while WORKSPACE_ROOT points at /workspace and the editable repo/.env lives elsewhere (/app/.env).
    # Overlay these explicit process env vars so Settings can discover remote local-model endpoints.
    for key in _LOCAL_PROVIDER_ROUTE_KEYS:
        v = os.environ.get(key, "").strip()
        if v:
            pairs[key] = v

    bao_vals = _openbao_get_all(workspace_root)
    for key, val in bao_vals.items():
        if val is not None and str(val).strip():
            pairs[str(key)] = str(val).strip()
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

    # When OpenBao is enabled, load_effective_pairs applies KV *after* .env and process env,
    # so KV wins for every key present. We must merge the full settings payload into KV
    # (not only SENSITIVE_KEYS); otherwise LLM_MODEL / LLM_PROVIDER / OAuth paths saved to
    # .env are ignored on the next request and stale values (e.g. llama3.2) reappear.
    ob_enabled, _ = _openbao_enabled(workspace_root)
    if ob_enabled and updates:
        if _openbao_put_many(workspace_root, updates):
            for k in sensitive_updates.keys():
                non_sensitive_updates[k] = ""

    elif sensitive_updates and _openbao_put_many(workspace_root, sensitive_updates):
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
        # Only overwrite LLM_API_KEY when the client sends a new value (omit key or send "***" to keep existing).
        raw_ak = payload.get("apiKey")
        new_key: str | None = None
        if raw_ak is not None:
            cand = str(raw_ak).strip()
            if cand and cand != "***":
                new_key = cand
                updates["LLM_API_KEY"] = cand
        # Clear OAuth state when using API key mode
        updates["OPENAI_OAUTH_STATE_FILE"] = ""
        if new_key:
            label = str(payload.get("credentialLabel") or "").strip() or f"{provider} · {model}"
            cid_in = str(payload.get("credentialId") or "").strip() or None
            if cid_in:
                row = llm_credentials_store.get_by_id(workspace_root, cid_in)
                if not row or str(row.get("provider") or "").strip().lower() != provider:
                    # Don't overwrite another vendor's row when switching provider in the UI.
                    cid_in = None
            try:
                nid = llm_credentials_store.upsert_credential(
                    workspace_root,
                    cid_in,
                    label,
                    provider,
                    new_key,
                    updates.get("LLM_BASE_URL", ""),
                )
            except ValueError as e:
                return False, str(e), {}
            llm_credentials_store.set_active_credential_id(workspace_root, nid)
    else:
        # OAuth mode
        updates["LLM_API_KEY"] = ""  # Will be filled by refresh
        state_file = str(payload.get("oauthStateFile") or "").strip()
        if not state_file:
            state_file = str(resolve_oauth_state_path(workspace_root, ""))
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
    state_path = resolve_oauth_state_path(workspace_root, state_file)

    if not state_path.exists():
        return False, "OAuth state file not found", None

    state, source = load_state_from_env_or_file(state_path)
    if not state:
        if source == 'file':
            return False, 'Invalid OAuth state file', None
        return False, 'OAuth state not found', None

    refresh_token = str(state.get("refresh") or "").strip()
    client_id = str(state.get("client_id") or "").strip()
    if not refresh_token or not client_id:
        return False, "OAuth state missing refresh_token or client_id", None

    token_url = pairs.get("OPENAI_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token").strip()
    if not token_url:
        token_url = "https://auth.openai.com/oauth/token"

    form = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
    }
    scp = _openai_oauth_refresh_scopes()
    if scp:
        form['scope'] = scp
    req = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(form).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
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
    saved_to, saved_path = save_state_to_env_or_file(state, state_path)

    save_env_key(workspace_root, "LLM_PROVIDER", "openai")
    ob_enabled, _ = _openbao_enabled(workspace_root)
    if ob_enabled:
        kv_updates = {"LLM_PROVIDER": "openai", "OPENAI_OAUTH_STATE_JSON": json.dumps(state)}
        if saved_to == 'file' and saved_path:
            kv_updates["OPENAI_OAUTH_STATE_FILE"] = saved_path
        _openbao_put_many(workspace_root, kv_updates)
    else:
        save_env_key(workspace_root, "LLM_API_KEY", new_access)

    return True, "Token refreshed", {
        "access": new_access[:20] + "...",
        "expires_in": expires_in,
    }


def get_oauth_status(workspace_root: Path) -> dict[str, Any]:
    pairs = load_effective_pairs(workspace_root)
    state_file = pairs.get("OPENAI_OAUTH_STATE_FILE", "").strip()
    state_path = resolve_oauth_state_path(workspace_root, state_file)

    if not state_path.exists():
        return {"configured": False, "message": "OAuth state file not found"}

    state, source = load_state_from_env_or_file(state_path)
    if not state:
        msg = 'Invalid OAuth state file' if source == 'file' else 'OAuth state not found'
        return {"configured": False, "message": msg}

    has_refresh = bool(str(state.get("refresh") or "").strip())
    has_access = bool(str(state.get("access") or "").strip())
    client_id = str(state.get("client_id") or "").strip()
    expires = int(state.get("expires") or 0)
    now_ms = int(time.time() * 1000)
    expires_in_sec = max(0, (expires - now_ms) // 1000) if expires else 0
    access = str(state.get("access") or "").strip()
    dec = _decode_access_token_for_oauth_ui(access)
    scopes = list(dec.get("scopes") or [])
    aud = str(dec.get("audience") or "")
    requested = str(state.get("oauth_scopes_requested") or "").strip()
    scope_status = _oauth_scope_status(dec, bool(access))

    return {
        "configured": has_refresh and client_id,
        "hasAccessToken": has_access,
        "clientId": client_id[:20] + "..." if len(client_id) > 20 else client_id,
        "expiresInSeconds": expires_in_sec,
        "accessTokenScopes": scopes,
        "accessTokenAudience": (aud[:120] + "...") if len(aud) > 120 else aud,
        "accessTokenJwtLike": bool(dec.get("jwt_like")),
        "accessTokenJwtParsed": bool(dec.get("parsed")),
        "hasApiResponsesWrite": "api.responses.write" in scopes,
        "hasModelRequestScope": "model.request" in scopes,
        "oauthScopesRequested": requested[:200] if requested else "",
        "oauthScopeStatus": scope_status,
        "stateSource": source,
    }


def _looks_like_openai_platform_api_key(value: str) -> bool:
    """Distinguish sk-… API keys from OAuth access tokens (often JWT) stored in LLM_API_KEY."""
    t = (value or "").strip()
    return t.startswith("sk-") or t.startswith("sk_")


def get_llm_settings_response(workspace_root: Path) -> dict[str, Any]:
    """Build full GET /api/settings/llm response: catalog, current settings, oauth status."""
    pairs = load_effective_pairs(workspace_root)
    provider = pairs.get("LLM_PROVIDER", "ollama").strip().lower()
    model = pairs.get("LLM_MODEL", "llama3.2").strip()
    base_url = resolve_effective_ollama_base_url(pairs) if provider == "ollama" else pairs.get("LLM_BASE_URL", "").strip()
    raw_api_key = (pairs.get("LLM_API_KEY") or "").strip()
    api_key_set = bool(raw_api_key)
    platform_api_key = _looks_like_openai_platform_api_key(raw_api_key)
    oauth_state = pairs.get("OPENAI_OAUTH_STATE_FILE", "").strip()
    oauth_token_url = pairs.get("OPENAI_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token").strip()

    # Codex OAuth persists the access token in LLM_API_KEY; that must not force "API Key" UI mode.
    if provider == "openai" and platform_api_key:
        auth_mode = "api_key"
    elif provider == "openai" and oauth_state:
        auth_mode = "oauth"
    else:
        auth_mode = "api_key"

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

    # Any saved API key (Gemini, Anthropic, OpenAI sk-, etc.) shows as masked with last-4 hint.
    api_key_masked = ""
    key_suffix_out = ""
    if api_key_set and auth_mode == "api_key":
        api_key_masked = "***"
        t = raw_api_key.strip()
        key_suffix_out = t[-4:] if len(t) > 4 else "••••"

    active_cred = llm_credentials_store.get_active_credential_id(workspace_root)
    cred_list = llm_credentials_store.list_public_credentials(workspace_root)
    active_meta = next((c for c in cred_list if c.get("id") == active_cred), None)
    active_label = str(active_meta.get("label") or "") if active_meta else ""
    am_prov = str(active_meta.get("provider") or "").strip().lower() if active_meta else ""

    # Only expose global masked key / active credential edit target when it matches this profile's provider.
    settings_credential_id = active_cred
    settings_credential_label = active_label
    if active_meta and am_prov != provider:
        settings_credential_id = ""
        settings_credential_label = ""
        api_key_masked = ""
        key_suffix_out = ""

    return {
        "catalog": provider_catalog_for_workspace(workspace_root),
        "settings": {
            "provider": provider,
            "model": model,
            "baseUrl": base_url,
            "authMode": auth_mode,
            "routeType": route_type,
            "host": host,
            "port": port,
            "basePath": base_path,
            "apiKey": api_key_masked,
            "keySuffix": key_suffix_out,
            "credentialId": settings_credential_id,
            "credentialLabel": settings_credential_label,
            "oauthStateFile": oauth_state or str(resolve_oauth_state_path(workspace_root, "")),
            "oauthTokenUrl": oauth_token_url,
        },
        "credentials": cred_list,
        "activeCredentialId": active_cred,
        "defaultCredentialId": active_cred,
        "oauthStatus": oauth_status,
        "restartRequired": False,
        "openbaoInstalled": is_openbao_installed(),
    }
