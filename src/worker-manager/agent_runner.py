#!/usr/bin/env python3
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger
logger = get_logger(__name__)

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
# Ensure worker-manager-local modules win over dashboard siblings with the same names.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(BASE) not in sys.path:
    sys.path.insert(1, str(BASE))
import llm_credentials_store as lcs  # noqa: E402
import codex_app_server_bridge as codex_bridge  # noqa: E402

AGENTS_ROOT = BASE / 'agents'


def _current_llm_model() -> str:
    """AP-10: Read from ES (flume-llm-config) first, fall back to process env.

    This allows the model to be changed in the Settings UI and take effect
    on the next agent iteration without requiring a container restart.
    """
    try:
        import sys
        _src = str(__import__('pathlib').Path(__file__).resolve().parent.parent)
        if _src not in sys.path:
            sys.path.insert(0, _src)
        from workspace_llm_env import get_active_llm_model
        return get_active_llm_model()
    except Exception:
        return (os.environ.get('LLM_MODEL') or 'llama3.2').strip() or 'llama3.2'


@dataclass
class AgentResult:
    action: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    verdict: Optional[str] = None
    bugs: list[dict[str, Any]] = field(default_factory=list)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _emit_usage(task: Optional[dict[str, Any]], usage: dict):
    if not task or not usage:
        return
    try:
        import urllib.request
        import json
        import ssl
        from datetime import datetime, timezone
        import os
        es_url = os.environ.get('ES_URL', 'http://elasticsearch:9200').rstrip('/')
        es_key = os.environ.get('ES_API_KEY', '')

        # S2: Only disable TLS verification for non-TLS endpoints
        ssl_ctx = None
        if es_url.startswith('https'):
            ssl_ctx = ssl.create_default_context()
            if os.environ.get('ES_VERIFY_TLS', 'true').lower() == 'false':
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        # P4: Ollama returns prompt_eval_count/eval_count, OpenAI uses
        # prompt_tokens/completion_tokens. Accept both key conventions.
        input_tokens = (
            usage.get('prompt_tokens')
            or usage.get('prompt_eval_count')
            or 0
        )
        output_tokens = (
            usage.get('completion_tokens')
            or usage.get('eval_count')
            or 0
        )
        doc = {
            'worker_name': task.get('active_worker') or task.get('assigned_agent') or 'unknown-worker',
            'worker_role': task.get('assigned_agent_role') or task.get('owner') or 'generic',
            'provider': task.get('preferred_llm_provider') or task.get('llm_provider') or 'ollama',
            'model': task.get('preferred_model') or task.get('llm_model') or 'unknown',
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'savings': 0,
            'created_at': datetime.now(timezone.utc).isoformat()
        }
        hdrs = {'Content-Type': 'application/json'}
        # S1: Only send Authorization header when an API key is present
        if es_key:
            hdrs['Authorization'] = f'ApiKey {es_key}'
        req = urllib.request.Request(f"{es_url}/agent-token-telemetry/_doc", data=json.dumps(doc).encode(), headers=hdrs, method='POST')
        with urllib.request.urlopen(req, timeout=2, context=ssl_ctx):
            pass
    except Exception as e:
        logger.warning(f"[metrics] Telemetry delivery aborted: {e}")

def _load_system_prompt(role: str) -> str:
    prompt_path = AGENTS_ROOT / role / 'SYSTEM_PROMPT.md'
    if prompt_path.exists():
        return prompt_path.read_text()
    return f"You are the {role} agent. Produce concise, actionable outputs."


def _task_llm_kw(task: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Route LLM calls per task: saved credential (API key + provider) or preferred_llm_provider."""
    if not task:
        return {}
    cred_id = str(task.get('preferred_llm_credential_id') or '').strip()
    if cred_id and cred_id != lcs.SETTINGS_DEFAULT_CREDENTIAL_ID:
        resolved = lcs.get_resolved_for_worker(BASE, cred_id)
        if resolved:
            bu = (resolved.get('base_url') or '').strip() or None
            return {
                'provider_override': resolved['provider'],
                'base_url_override': bu,
                'api_key_override': resolved.get('api_key', ''),
            }
    pov = (task.get('preferred_llm_provider') or '').strip().lower()
    if not pov:
        return {}
    return {'provider_override': pov, 'base_url_override': None}


def _task_uses_codex_app_server(task: Optional[dict[str, Any]]) -> bool:
    if not task:
        return False
    cred_id = str(task.get('preferred_llm_credential_id') or '').strip()
    if cred_id == lcs.OPENAI_OAUTH_CREDENTIAL_ID:
        return codex_bridge.codex_auth_present() and codex_bridge.codex_available()
    if cred_id and cred_id not in ('', lcs.SETTINGS_DEFAULT_CREDENTIAL_ID):
        return False
    provider = (task.get('preferred_llm_provider') or '').strip().lower()
    if provider and provider != 'openai':
        return False
    api_key = (os.environ.get('LLM_API_KEY') or '').strip()
    has_oauth = bool((os.environ.get('OPENAI_OAUTH_STATE_FILE') or '').strip() or (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip())
    if provider == 'openai' and has_oauth and not (api_key.startswith('sk-') or api_key.startswith('sk_')):
        return codex_bridge.codex_auth_present() and codex_bridge.codex_available()
    return False


def _call_ollama(
    system_prompt: str,
    user_payload: dict[str, Any],
    model: Optional[str] = None,
    task: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    from utils import llm_client
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': json.dumps(user_payload, indent=2)},
    ]
    try:
        kw = _task_llm_kw(task)
        content, usage = llm_client.chat(
            messages,
            model=model or _current_llm_model(),
            temperature=0.2,
            max_tokens=8192,
            return_usage=True,
            timeout_seconds=300,
            ollama_think=False,
            **kw,
        )
        _emit_usage(task, usage)
        content = content.strip()
        try:
            val = content
            # P3: Robust JSON fence extraction — handle trailing prose after
            # closing fence, case-insensitive language tags, and nested fences.
            if val.startswith('```'):
                # Find the opening fence line and skip it
                first_nl = val.index('\n') if '\n' in val else len(val)
                inner = val[first_nl + 1:]
                # Find the LAST closing fence
                last_fence = inner.rfind('```')
                if last_fence != -1:
                    inner = inner[:last_fence]
                val = inner.strip()
            return json.loads(val)
        except json.JSONDecodeError as de:
            logger.warning(f'[agent_runner] LLM JSON parse failed — raw response (first 2000 chars): {content[:2000]}')
            raise de
    except Exception as e:
        logger.error(f"LLM Execution Trap: {e}", exc_info=True)
        raise e




_ELASTRO_QUERY_TOOL = {
    'type': 'function',
    'function': {
        'name': 'elastro_query_ast',
        'description': 'Query the Elastro AST index for precise code mappings and snippets matching your work item. MUST be used before modifying code to dynamically save tokens contextually.',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'The search query, e.g., a function name or class.'},
                'target_path': {'type': 'string', 'description': 'The absolute path to the target repository.'},
            },
            'required': ['query', 'target_path'],
        },
    },
}

_IMPLEMENTER_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': 'Read the full contents of a file.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute or repo-relative file path'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': 'Write (overwrite) a file with the given content. Creates parent directories as needed.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute or repo-relative file path'},
                    'content': {'type': 'string', 'description': 'Full file content to write'},
                },
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memory_read',
            'description': 'Retrieve cached context natively from the semantic memory bounds.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'namespace': {'type': 'string', 'enum': ['agent_semantic_memory', 'agent_knowledge']},
                    'key': {'type': 'string'},
                },
                'required': ['namespace', 'key'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memory_write',
            'description': 'Persist operational logic natively into semantic memory bounds.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'namespace': {'type': 'string', 'enum': ['agent_semantic_memory', 'agent_knowledge']},
                    'key': {'type': 'string'},
                    'value': {'type': 'string'},
                    'ttl': {'type': 'integer', 'description': 'Time to live in seconds'}
                },
                'required': ['namespace', 'key', 'value'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'multi_replace_file_content',
            'description': 'Replace multiple non-contiguous chunks of text in a file. Use this for deterministic surgical code edits instead of raw bash loops.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute or repo-relative file path'},
                    'replacements': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'target_content': {'type': 'string', 'description': 'Exact string to find'},
                                'replacement_content': {'type': 'string', 'description': 'Exact string to replace it with'}
                            },
                            'required': ['target_content', 'replacement_content']
                        }
                    }
                },
                'required': ['path', 'replacements'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_directory',
            'description': 'List files and subdirectories at a path.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Directory path (defaults to repo root)'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'run_shell',
            'description': 'Strict validation boundary. Run linting or test commands (e.g. npm test, pytest, golangci-lint, ruff). Strictly prohibited from file modification or guessing state via bash macros.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': 'Shell command to execute'},
                    'working_dir': {'type': 'string', 'description': 'Optional working directory override'},
                },
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'implementation_complete',
            'description': 'Signal that all code changes are done and ready for testing.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'summary': {'type': 'string', 'description': 'What was implemented'},
                    'commit_message': {'type': 'string', 'description': 'Git commit message'},
                    'artifacts': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of files changed',
                    },
                },
                'required': ['summary', 'commit_message'],
            },
        },
    },
]


def _resolve_path(path: str, repo_path: Optional[str]) -> Path:
    p = Path(path)
    try:
        base = Path(repo_path).resolve() if repo_path else Path('.').resolve()
        final_path = (base / p).resolve() if not p.is_absolute() else p.resolve()
        if not str(final_path).startswith(str(base)):
            raise PermissionError('Path Traversal Attempt Halted.')
        return final_path
    except Exception:
        raise PermissionError('Path Traversal Attempt Halted.')



def _exec_read_file(args: dict, repo_path: Optional[str]) -> str:
    try:
        p = _resolve_path(args.get('path', ''), repo_path)
        content = p.read_text(errors='replace')
        if len(content) > 12000:
            return content[:12000] + f'\n... (truncated, {len(content)} total chars)'
        return content
    except Exception as e:
        return f'ERROR reading file: {e}'


def _exec_write_file(args: dict, repo_path: Optional[str]) -> str:
    # B1: Fixed duplicate except block that silently swallowed write errors.
    try:
        p = _resolve_path(args.get('path', ''), repo_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = args.get('content', '')
        if p.name.endswith('.py'):
            try:
                compile(content, p.name, 'exec')
            except SyntaxError as e:
                return f'ERROR writing file: Meta-Critic Python Syntax Check Failed at line {e.lineno}: {e.msg}'
        p.write_text(content)
        return f'OK: wrote {len(content)} chars to {p}'
    except Exception as e:
        return f'ERROR writing file: {e}'


def _exec_elastro_query_ast(args: dict, repo_path: Optional[str]) -> str:
    query = args.get('query', '')
    target_path = _resolve_path(args.get('target_path', repo_path or '.'), repo_path)
    try:
        es_url = os.environ.get('ES_URL', 'http://elasticsearch:9200').rstrip('/')
        es_api_key = os.environ.get('ES_API_KEY', '')
        headers = {'Content-Type': 'application/json'}
        if es_api_key:
            headers['Authorization'] = f'ApiKey {es_api_key}'

        # Schema: file_path, content, functions_defined, functions_called, chunk_name, chunk_type, extension, repo_name
        query_payload = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": [
                        "content",
                        "functions_defined^3",
                        "functions_called^2",
                        "file_path^2",
                        "chunk_name^3",
                        "repo_name",
                    ]
                }
            },
            "size": 12,
            "_source": ["file_path", "content", "functions_defined", "functions_called", "chunk_type", "chunk_name", "extension", "repo_name"],
        }

        elastro_index = os.environ.get("FLUME_ELASTRO_INDEX", "flume-elastro-graph")

        req = urllib.request.Request(
            f"{es_url}/{elastro_index}/_search",
            data=json.dumps(query_payload).encode(),
            headers=headers,
            method='POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                hits = data.get('hits', {}).get('hits', [])
                if not hits:
                    output = f"AST Search: No matching nodes found for '{query}' in index '{elastro_index}'. Try a broader search term or fall back to list_directory + grep."
                    savings = 0
                else:
                    output_chunks = []
                    for h in hits:
                        src = h.get('_source', {})
                        fp = src.get('file_path', 'unknown')
                        chunk_type = src.get('chunk_type', 'file')
                        chunk_name = src.get('chunk_name', 'module')
                        ext = src.get('extension', '')
                        fns_defined = src.get('functions_defined', [])
                        fns_called = src.get('functions_called', [])
                        content = src.get('content', '')[:800]

                        entry = f"── {fp} ({chunk_type}: {chunk_name}) [{ext}]"
                        if fns_defined:
                            entry += f"\n  Defines: {', '.join(fns_defined[:10])}"
                        if fns_called:
                            entry += f"\n  Calls: {', '.join(fns_called[:10])}"
                        entry += f"\n  Content:\n{content}"
                        output_chunks.append(entry)
                    output = f"AST Search Results ({len(hits)} hits):\n\n" + "\n\n".join(output_chunks)
                    # Estimate tokens saved: AST cache returns targeted results vs reading entire files.
                    # Token estimate: output bytes / 4 (standard ~4 bytes/token approximation).
                    savings = len(output.encode('utf-8')) // 4
        except urllib.error.HTTPError as he:
            if he.code == 404:
                return f"AST Search Failed: Index '{elastro_index}' not found. The codebase AST has not been ingested yet. Please fall back to manual recursive file search via list_directory and grep."
            return f"AST Search HTTP Error: {he.code} {he.reason}"

        # Submit agent telemetry metric
        if es_url:
            doc = {
                'worker_name': 'implementer',
                'worker_role': 'system',
                'provider': 'elastro-cache',
                'model': 'ast-sync',
                'input_tokens': 0,
                'output_tokens': 0,
                'savings': savings,
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            req = urllib.request.Request(
                f"{es_url}/agent-token-telemetry/_doc",
                data=json.dumps(doc).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            try:
                with urllib.request.urlopen(req, timeout=3):
                    pass
            except Exception:
                pass
                
        return output
    except Exception as e:
        return f'ERROR querying AST natively: {e}'


def _exec_memory_read(args: dict) -> str:
    ns = args.get('namespace')
    key = args.get('key')
    
    if not ns or not key:
        logger.error({
            "event": "memory_read",
            "status": "failure",
            "error": "namespace and key are required",
        })
        return json.dumps({"status": "error", "message": "namespace and key are required"})
        
    try:
        es_url = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
        api_key = os.environ.get('ES_API_KEY', '')
        headers = {'Authorization': f'ApiKey {api_key}', 'Content-Type': 'application/json'}
        query = json.dumps({'query': {'term': {'_id': key}}}).encode()
        
        req = urllib.request.Request(f"{es_url}/{ns}/_search", data=query, headers=headers, method='POST')
        import ssl
        # ensure no local shadow
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            raw = resp.read().decode()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as je:
                raise ValueError(f"Invalid JSON returned from ES: {raw[:100]}") from je
                
            hits = data.get('hits', {}).get('hits', [])
            if not hits:
                logger.info({"event": "memory_read", "status": "not_found", "namespace": ns, "key": key})
                return json.dumps({"status": "not_found", "message": "No memory stored at this key."})
            
            src = hits[0].get('_source', {})
            expires = src.get('expires_at')
            if expires:
                import time
                if time.time() > expires:
                    logger.info({"event": "memory_read", "status": "expired", "namespace": ns, "key": key})
                    return json.dumps({"status": "not_found", "message": "Memory has expired due to TTL decay."})
            
            value = src.get('value', '')
            logger.info({"event": "memory_read", "status": "success", "namespace": ns, "key": key})
            return json.dumps({"status": "success", "value": value})
            
    except urllib.error.URLError as e:
        logger.error({
            "event": "memory_read",
            "status": "failure",
            "namespace": ns,
            "key": key,
            "error": str(e),
            "error_type": "URLError"
        }, exc_info=True)
        return json.dumps({"status": "error", "message": f"Network error contacting Elasticsearch: {e}", "error_type": "URLError"})
        
    except Exception as e:
        logger.error({
            "event": "memory_read",
            "status": "failure",
            "namespace": ns,
            "key": key,
            "error": str(e),
            "error_type": type(e).__name__
        }, exc_info=True)
        return json.dumps({"status": "error", "message": f"Internal error during memory read: {e}", "error_type": type(e).__name__})

def _exec_memory_write(args: dict) -> str:
    ns = args.get('namespace')
    key = args.get('key')
    val = args.get('value')
    ttl = args.get('ttl')
    
    if not ns or not key or not val:
        logger.error({
            "event": "memory_write",
            "status": "failure",
            "error": "namespace, key, and value are required"
        })
        return json.dumps({"status": "error", "message": "namespace, key, and value are required"})
        
    try:
        es_url = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
        api_key = os.environ.get('ES_API_KEY', '')
        # S1: Only send Authorization header when an API key is present
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'ApiKey {api_key}'
        
        import time
        doc = {'key': key, 'value': val, 'updated_at': time.time()}
        if ttl:
            doc['expires_at'] = time.time() + int(ttl)
            
        payload = json.dumps(doc).encode()
        # ensure no local shadow
        safe_key = urllib.parse.quote(key, safe='')
        # Q3: Use PUT for idempotent upsert with explicit _id
        req = urllib.request.Request(f"{es_url}/{ns}/_doc/{safe_key}", data=payload, headers=headers, method='PUT')
        
        # S2: Only disable TLS verification when explicitly configured
        import ssl
        ssl_ctx = None
        if es_url.startswith('https'):
            ssl_ctx = ssl.create_default_context()
            if os.environ.get('ES_VERIFY_TLS', 'true').lower() == 'false':
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=5):
            logger.info({
                "event": "memory_write",
                "status": "success",
                "namespace": ns,
                "key": key
            })
            return json.dumps({"status": "success", "message": f"Wrote memory key {key} to {ns}"})
            
    except urllib.error.URLError as e:
        logger.error({
            "event": "memory_write",
            "status": "failure",
            "namespace": ns,
            "key": key,
            "error": str(e),
            "error_type": "URLError"
        }, exc_info=True)
        return json.dumps({"status": "error", "message": f"Network error contacting Elasticsearch: {e}", "error_type": "URLError"})
        
    except Exception as e:
        logger.error({
            "event": "memory_write",
            "status": "failure",
            "namespace": ns,
            "key": key,
            "error": str(e),
            "error_type": type(e).__name__
        }, exc_info=True)
        return json.dumps({"status": "error", "message": f"Internal error during memory write: {e}", "error_type": type(e).__name__})

def _exec_multi_replace_file_content(args: dict, repo_path: Optional[str]) -> str:
    try:
        p = _resolve_path(args.get('path', ''), repo_path)
        if not p.exists():
            return json.dumps({"status": "error", "message": f"File {p} does not exist", "path": str(p)})
        
        content = p.read_text(errors='replace')
        replacements = args.get('replacements', [])
        
        if not replacements:
            return json.dumps({"status": "error", "message": "No replacements provided."})
            
        for idx, repl in enumerate(replacements):
            target = repl.get('target_content', '')
            new_text = repl.get('replacement_content', '')
            
            if target not in content:
                return json.dumps({"status": "error", "message": "target_content not found in file.", "block_index": idx})
            if content.count(target) > 1:
                return json.dumps({"status": "error", "message": "target_content matches multiple locations. Make it more specific.", "block_index": idx})
                
            content = content.replace(target, new_text)
            
        p.write_text(content)
        
        return json.dumps({"status": "success", "message": f"Applied {len(replacements)} deterministic replacements to {p}"})
    except Exception as e:
        logger.exception("Unexpected error in multi_replace_file_content")
        return json.dumps({"status": "error", "message": str(e), "error_type": type(e).__name__})


def _exec_list_directory(args: dict, repo_path: Optional[str]) -> str:
    try:
        raw = args.get('path') or repo_path or '.'
        p = _resolve_path(raw, repo_path)
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = [f'{"[d]" if e.is_dir() else "[f]"} {e.name}' for e in entries]
        return '\n'.join(lines) if lines else '(empty directory)'
    except Exception as e:
        return f'ERROR listing directory: {e}'


class ShellPermissionError(PermissionError):
    """Raised when an agent attempts to execute an unauthorized shell command."""
    pass

def _exec_run_shell(args: dict, repo_path: Optional[str]) -> str:
    command = args.get('command', '')
    cwd = args.get('working_dir') or repo_path or '.'
    
    try:
        import shlex
        cmd_list = shlex.split(command)
        if not cmd_list:
            return json.dumps({"status": "error", "message": "Empty command provided."})
            
        if len(cmd_list) >= 3 and cmd_list[0] == 'cd' and cmd_list[2] == '&&':
            resolved_cwd = _resolve_path(cmd_list[1], repo_path)
            cwd = str(resolved_cwd)
            cmd_list = cmd_list[3:]
            if not cmd_list:
                return json.dumps({"status": "error", "message": "Empty command provided after cd."})
                
        executable = cmd_list[0]
        # Q2: Added 'git' for validation commands (git diff, git status, git log)
        allow_list = {'npm', 'npx', 'pytest', 'golangci-lint', 'ruff', 'go', 'python', 'python3', 'uv', 'node', 'grep', 'find', 'git'}
        
        if executable not in allow_list:
            logger.warning({
                "event": "security_boundary_violation",
                "service": "worker-manager",
                "function": "_exec_run_shell",
                "attempted_command": command,
                "executable": executable,
                "reason": "Executable not in allow-list"
            })
            raise ShellPermissionError(f"run_shell is strictly bounded to validation commands ({', '.join(sorted(allow_list))}). System/file manipulation commands are explicitly denied.")
            
        result = subprocess.run(
            cmd_list, shell=False, capture_output=True, text=True, timeout=30, cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 6000:
            output = output[:6000] + '\n... (truncated)'
        return json.dumps({
            "status": "success" if result.returncode == 0 else "error",
            "exit_code": result.returncode,
            "output": output or "(no output)"
        })
    except ShellPermissionError:
        raise
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "error", "message": "Command timed out after 30s"})
    except Exception as e:
        logger.error({
            "event": "run_shell_error",
            "command": command,
            "error_type": type(e).__name__,
            "error_message": str(e)
        }, exc_info=True)
        return json.dumps({"status": "error", "message": f"Execution failed: {e}", "error_type": type(e).__name__})


_LLM_CLIENT = None


def _load_llm_client():
    global _LLM_CLIENT
    if _LLM_CLIENT and getattr(_LLM_CLIENT, '__file__', '') == str(HERE.parent / 'utils' / 'llm_client.py'):
        return _LLM_CLIENT
    path = HERE.parent / 'utils' / 'llm_client.py'
    spec = importlib.util.spec_from_file_location('worker_manager_llm_client', path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    _LLM_CLIENT = mod
    return mod


def _call_ollama_tools(
    messages: list,
    tools: list,
    model: str,
    task: Optional[dict[str, Any]] = None,
) -> Optional[dict]:
    try:
        llm_client = _load_llm_client()
        _task_llm_kw(task)  # resolve creds into env side-effects
        res = llm_client.chat_with_tools(
            messages,
            tools,
            model=model,
            temperature=0.2,
            max_tokens=4096,
            ollama_think=True,
        )
        _emit_usage(task, {'total_tokens': 0})
        return res
    except Exception as e:

        logger.error(f"[agent_runner] _call_ollama_tools error: {type(e).__name__}: {e}", exc_info=True)
        return None

def _codex_json_schema_implementer() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'summary': {'type': 'string'},
            'commit_message': {'type': 'string'},
            'artifacts': {'type': 'array', 'items': {'type': 'string'}},
        },
        'required': ['summary', 'commit_message', 'artifacts'],
        'additionalProperties': False,
    }


def _codex_json_schema_tester() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'action': {'type': 'string'},
            'summary': {'type': 'string'},
            'bugs': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'objective': {'type': 'string'},
                        'severity': {'type': 'string'},
                    },
                    'required': ['title', 'objective', 'severity'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['action', 'summary', 'bugs'],
        'additionalProperties': False,
    }


def _codex_json_schema_reviewer() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'verdict': {'type': 'string'},
            'summary': {'type': 'string'},
        },
        'required': ['verdict', 'summary'],
        'additionalProperties': False,
    }


def _codex_json_schema_pm() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['decompose', 'compute_ready']},
            'summary': {'type': 'string'},
            'subtasks': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'objective': {'type': 'string'},
                        'item_type': {'type': 'string', 'enum': ['feature', 'story', 'task']},
                    },
                    'required': ['title', 'objective', 'item_type'],
                    'additionalProperties': False,
                }
            }
        },
        'required': ['action', 'summary'],
        'additionalProperties': False,
    }


def _run_codex_json_task(prompt: str, schema: dict[str, Any], *, model: str, cwd: str) -> dict[str, Any] | None:
    try:
        return codex_bridge.run_turn_json(prompt, model=model, cwd=cwd, output_schema=schema, timeout=300)
    except Exception as e:
        logger.info(f'[agent_runner] Codex app-server error: {type(e).__name__}: {e}')
        raise e


# ── Pre-flight phantom task detection ────────────────────────────────────
# Patterns that suggest a task assumes specific artifacts exist.
# Each entry: (keywords_in_title, file_extensions_to_check)
_PHANTOM_ARTIFACT_PATTERNS = [
    (['replace', 'svg'], ['.svg']),
    (['replace', 'icon', 'asset'], ['.svg', '.png', '.ico']),
    (['replace', 'image', 'asset'], ['.png', '.jpg', '.jpeg', '.webp']),
    (['replace', 'png'], ['.png']),
    (['swap', 'icon'], ['.svg', '.png', '.ico']),
    (['swap', 'image'], ['.png', '.jpg', '.jpeg', '.webp']),
    (['update', 'icon', 'asset'], ['.svg', '.png', '.ico']),
]


def _extract_validation_symbols(text: str) -> list[str]:
    """Extract likely file names and components/functions for AST validation."""
    # Common file names with relevant extensions
    files = re.findall(r'\b[\w\.\-]+\.(?:tsx|ts|js|jsx|py|go|html|css|md|json|yml|yaml)\b', text, re.IGNORECASE)
    # PascalCase/CamelCase words (likely components, classes, or function names)
    symbols = re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b', text)
    
    found = set([s.strip('.') for s in files] + symbols)
    # Filter out common UI/design terms that might be capitalized but aren't specific AST symbols
    ignore = {'API', 'URL', 'UI', 'UX', 'JSON', 'HTML', 'HTTP', 'REST', 'GraphQL', 'PDF', 'XML'}
    return [s for s in found if s not in ignore]


def _preflight_validate_task(
    task: dict,
    repo_path: Optional[str],
    progress_fn=None,
) -> Optional['AgentResult']:
    """Detect phantom tasks that reference non-existent artifacts.

    Returns an AgentResult to skip the task if validation fails,
    or None if the task looks valid and should proceed normally.
    """
    if not repo_path:
        return None
    title = (task.get('title') or '').lower()
    desc = (task.get('description') or task.get('objective') or '').lower()
    combined = f"{title} {desc}"

    for keywords, extensions in _PHANTOM_ARTIFACT_PATTERNS:
        if all(kw in combined for kw in keywords):
            # This task assumes specific artifacts exist — verify they do
            repo = Path(repo_path)
            found = False
            for ext in extensions:
                # Quick check: does at least one file with this extension exist?
                try:
                    result = subprocess.run(
                        ['find', str(repo), '-name', f'*{ext}', '-type', 'f', '-maxdepth', '5'],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.stdout.strip():
                        found = True
                        break
                except Exception:
                    found = True  # fail open
                    break
            if not found:
                reason = (
                    f"Pre-flight validation: task assumes {'/'.join(extensions)} artifacts exist "
                    f"but none were found in {repo_path}. Skipping phantom task."
                )
                if progress_fn:
                    progress_fn(f'Skipping: {reason}')
                logger.warning(reason)
                return AgentResult(
                    action='implementation_complete',
                    summary=reason,
                    artifacts=[],
                    metadata={'source': 'preflight_skip', 'commit_sha': '', 'commit_message': ''},
                )

    # ── AST-Aware Task Validation ─────────────────────────────────────────
    # For modification tasks, extract targeted symbols and query the AST.
    # If a modification task specifies exact targets but NONE exist in the AST,
    # skip it to prevent extreme hallucination spirals.
    mod_verbs = ['update', 'replace', 'modify', 'edit', 'fix', 'change']
    is_mod_task = any(verb in combined for verb in mod_verbs)
    create_verbs = ['create', 'add ', 'new ', 'implement']
    is_create_task = any(verb in combined for verb in create_verbs)
    
    if is_mod_task and not is_create_task:
        symbols = _extract_validation_symbols(combined)
        if symbols:
            all_missed = True
            for sym in symbols:
                # Reuse the AST query function
                ast_result = _exec_elastro_query_ast({'query': sym}, repo_path)
                if "No matching nodes found" not in ast_result:
                    all_missed = False
                    break # At least one extracted symbol exists
            
            if all_missed:
                reason = (
                    f"AST-Aware Validation: task aims to modify specific symbols ({', '.join(symbols)}) "
                    f"but none were found in the Elastro index for {repo_path}. Skipping phantom task."
                )
                if progress_fn:
                    progress_fn(f'Skipping: {reason}')
                logger.warning(reason)
                return AgentResult(
                    action='implementation_complete',
                    summary=reason,
                    artifacts=[],
                    metadata={'source': 'preflight_skip', 'commit_sha': '', 'commit_message': ''},
                )
                
    return None


def run_implementer(
    task: dict[str, Any],
    repo_path: Optional[str] = None,
    on_progress: Optional[Any] = None,
    on_thought: Optional[Any] = None,
) -> AgentResult:
    system_prompt = _load_system_prompt('implementer')
    model = task.get('preferred_model') or _current_llm_model()

    def _progress(note: str) -> None:
        if on_progress:
            try:
                on_progress(note)
            except Exception:
                pass

    def _thought(note: str) -> None:
        if on_thought:
            try:
                on_thought(note)
            except Exception:
                pass

    if _task_uses_codex_app_server(task) and repo_path:
        _progress('Using Codex app-server backend…')
        prompt = (
            f"SYSTEM PROMPT:\n{system_prompt}\n\n"
            "You are operating inside the repository at the provided cwd. Make any needed file edits directly. "
            "When finished, return ONLY JSON matching the required schema.\n\n"
            f"TASK JSON:\n{json.dumps(task, indent=2)}\n\n"
            f"REPO PATH: {repo_path}\n\n"
            "If task.requires_code is true, you must make real code edits before finishing. "
            "Set artifacts to the list of changed file paths relative to the repo root when possible."
        )
        response = _run_codex_json_task(
            prompt,
            _codex_json_schema_implementer(),
            model=model,
            cwd=repo_path,
        )
        if response and isinstance(response, dict):
            return AgentResult(
                action='handoff_to_tester',
                summary=str(response.get('summary') or 'Implementation completed.').strip() or 'Implementation completed.',
                artifacts=[str(x) for x in (response.get('artifacts') or []) if str(x).strip()],
                metadata={
                    'source': 'codex_app_server',
                    'commit_sha': '',
                    'commit_message': str(response.get('commit_message') or '').strip(),
                },
            )
        _progress('Codex app-server returned no usable response — stopping.')
        return AgentResult(
            action='implementer_failed',
            summary='Codex app-server returned no usable response — stopping.',
            artifacts=[],
            metadata={'source': 'llm_no_response', 'commit_sha': '', 'commit_message': ''},
        )

    # ── Pre-flight: detect phantom tasks before burning LLM tokens ─────────
    # If the task title/description references specific file types (SVG, PNG, icon
    # asset, etc.) that don't exist in the workspace, skip immediately rather than
    # running 50+ shell commands searching for them.
    _phantom_result = _preflight_validate_task(task, repo_path, _progress)
    if _phantom_result is not None:
        return _phantom_result

    # AST is always available — projects are cloned and AST-indexed before work begins.
    system_prompt += "\n\nCRITICAL CONTEXT: An Elastro AST index is available. You MUST use the `elastro_query_ast` tool to search for function names, component paths, or code structures BEFORE listing directories or reading files. This is dramatically faster than directory traversal. You may also use `grep` or `find` via `run_shell` for targeted file searches."

    messages: list[dict] = [
        {'role': 'system', 'content': system_prompt},
        {
            'role': 'user',
            'content': json.dumps(
                {
                    'task': task,
                    'repo_path': repo_path,
                    'instruction': (
                        'Complete the task using the provided tools. '
                        'Start by reading the task title and objective carefully. '
                        + (
                            'This task MUST be treated as a code-edit task (task.requires_code=true): '
                            'you MUST write files to the repo and call implementation_complete with a non-empty commit_message. '
                        'Do NOT treat it as analysis/context.'
                        if task.get('requires_code')
                        else 'Decide whether this is a code task, analysis task, or context task. '
                        )
                        + ' You MUST use elastro_query_ast to search the AST index FIRST. Do NOT use list_directory unless the AST search fails or is insufficient. Act accordingly, then call implementation_complete with a clear summary.'
                    ),
                },
                indent=2,
            ),
        },
    ]

    final_summary = ''
    final_commit_message = ''
    final_artifacts: list[str] = []

    _progress('Agent started — analysing task…')

    for _iteration in range(15):
        _progress(f'Thinking… (step {_iteration + 1})')
        dynamic_tools = _IMPLEMENTER_TOOLS.copy()
        dynamic_tools.append(_ELASTRO_QUERY_TOOL)
            
        raw = _call_ollama_tools(messages, dynamic_tools, model, task=task)
        if not raw:
            # Backoff before retrying to avoid tight error loops (e.g., rate limits)
            time.sleep(min(2 + _iteration, 8))
            _progress('LLM returned no response — stopping.')
            return AgentResult(
                action='implementer_failed',
                summary='LLM returned no response — stopping.',
                artifacts=[],
                metadata={
                    'source': 'llm_no_response',
                    'commit_sha': '',
                    'commit_message': '',
                },
            )

        message = raw.get('message', {})
        tool_calls = message.get('tool_calls') or []
        thoughts = message.get('thoughts') or ''
        content = message.get('content') or ''

        if thoughts:
            _thought(thoughts)
        elif content and tool_calls:
            _thought(content)

        # Normalize tool call shape (OpenAI requires type + id on follow-up turns)
        norm_calls = []
        for idx, call in enumerate(tool_calls):
            call = dict(call)
            call.setdefault('id', f'call_{idx}')
            call.setdefault('type', 'function')
            norm_calls.append(call)

        # Append assistant turn
        assistant_msg: dict[str, Any] = {'role': 'assistant', 'content': message.get('content') or ''}
        if norm_calls:
            assistant_msg['tool_calls'] = norm_calls
        messages.append(assistant_msg)

        if not tool_calls:
            final_summary = (message.get('content') or '').strip() or 'Implementation completed.'
            _progress(f'Done: {final_summary[:120]}')
            break

        done = False
        for call in norm_calls:
            fn_name = call.get('function', {}).get('name', '')
            fn_args = call.get('function', {}).get('arguments', {})
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            call_id = call.get('id', '')

            # Emit a human-readable progress note for each tool use
            if fn_name == 'read_file':
                _progress(f'Reading file: {fn_args.get("path", "")}')
                tool_result = _exec_read_file(fn_args, repo_path)
            elif fn_name == 'write_file':
                _progress(f'Writing file: {fn_args.get("path", "")}')
                tool_result = _exec_write_file(fn_args, repo_path)
            elif fn_name == 'multi_replace_file_content':
                _progress(f'Replacing content in: {fn_args.get("path", "")}')
                tool_result = _exec_multi_replace_file_content(fn_args, repo_path)
            elif fn_name == 'list_directory':
                _progress(f'Listing directory: {fn_args.get("path", "") or "(repo root)"}')
                tool_result = _exec_list_directory(fn_args, repo_path)
            elif fn_name == 'run_shell':
                cmd = (fn_args.get('command', ''))[:80]
                _progress(f'Running: {cmd}')
                try:
                    tool_result = _exec_run_shell(fn_args, repo_path)
                except ShellPermissionError as e:
                    tool_result = json.dumps({"status": "error", "error_type": "ShellPermissionError", "message": str(e)})
            elif fn_name == 'memory_read':
                _progress(f'Reading memory: {fn_args.get("namespace", "")}/{fn_args.get("key", "")}')
                tool_result = _exec_memory_read(fn_args)
            elif fn_name == 'memory_write':
                _progress(f'Writing memory: {fn_args.get("namespace", "")}/{fn_args.get("key", "")}')
                tool_result = _exec_memory_write(fn_args)
            elif fn_name == 'elastro_query_ast':
                _progress(f'Querying AST for nodes mapping: {fn_args.get("query", "")}')
                tool_result = _exec_elastro_query_ast(fn_args, repo_path)
            elif fn_name == 'implementation_complete':
                final_summary = fn_args.get('summary', 'Implementation completed.')
                final_commit_message = fn_args.get('commit_message', '')
                if not final_commit_message:
                    final_commit_message = 'Verified task complete, no code changes required.'
                final_artifacts = fn_args.get('artifacts') or []
                _progress(f'Completing: {final_summary[:120]}')
                tool_result = 'Implementation marked complete.'
                done = True
            else:
                tool_result = f'Unknown tool: {fn_name}'
                _progress(f'Unknown tool called: {fn_name}')

            messages.append({'role': 'tool', 'content': tool_result, 'tool_call_id': call_id})

        if done:
            break

    if final_summary:
        return AgentResult(
            action='handoff_to_tester',
            summary=final_summary,
            artifacts=final_artifacts,
            metadata={
                'source': 'llm_agentic',
                'commit_sha': '',
                'commit_message': final_commit_message,
            },
        )

    return AgentResult(
        action='handoff_to_tester',
        summary='Implementation step completed (fallback). Ready for test validation.',
        artifacts=[],
        metadata={'source': 'fallback', 'commit_sha': '', 'commit_message': ''},
    )


def run_tester(task: dict[str, Any]) -> AgentResult:
    system_prompt = _load_system_prompt('tester')
    response = _call_ollama(
        system_prompt,
        {
            'instruction': (
                'Return JSON: {"action":"pass|fail","summary":"...","bugs":[{"title":"...","objective":"...","severity":"high|normal"}]}'
            ),
            'task': task,
        },
        model=task.get('preferred_model') or _current_llm_model(),
        task=task,
    )
    if response and isinstance(response, dict):
        action = response.get('action', 'pass')
        if action == 'fail':
            bugs = response.get('bugs') or [{
                'title': f"Bug found in {task.get('title', task.get('id', 'task'))}",
                'objective': response.get('summary', 'Fix failing behavior found during validation.'),
                'severity': 'high',
            }]
            return AgentResult(action='fail', summary=response.get('summary', 'Testing failed.'), bugs=bugs, metadata={'source': 'llm'})
        return AgentResult(action='pass', summary=response.get('summary', 'Testing passed.'), metadata={'source': 'llm'})
    return AgentResult(action='pass', summary='Testing passed by fallback policy.', metadata={'source': 'fallback'})


def run_reviewer(task: dict[str, Any]) -> AgentResult:
    system_prompt = _load_system_prompt('reviewer')
    response = _call_ollama(
        system_prompt,
        {
            'instruction': 'Return JSON: {"verdict":"approved|changes_requested","summary":"..."}',
            'task': task,
        },
        model=task.get('preferred_model') or _current_llm_model(),
        task=task,
    )
    if response and isinstance(response, dict):
        raw_verdict = response.get('verdict', 'approved')
        # Normalise: only 'approved' and 'changes_requested' are valid.
        # Any other value (e.g. 'blocked' hallucinated by the LLM) is treated
        # as 'changes_requested' so the task re-queues rather than blocking.
        if raw_verdict not in ('approved', 'changes_requested'):
            raw_verdict = 'changes_requested'
        verdict = raw_verdict
        return AgentResult(
            action='review_complete',
            verdict=verdict,
            summary=response.get('summary', f'Review verdict: {verdict}.'),
            metadata={'source': 'llm'},
        )
    return AgentResult(
        action='review_complete',
        verdict='approved',
        summary='Review approved by fallback policy.',
        metadata={'source': 'fallback'},
    )


def _get_cluster_topology() -> dict[str, Any]:
    from worker_handlers import es_request
    try:
        res = es_request('/agent-system-workers/_search', {'size': 100}, method='GET')
        implementers = []
        for hit in res.get('hits', {}).get('hits', []):
            state = hit.get('_source', {})
            for w in state.get('workers', []):
                if w.get('role') == 'implementer':
                    implementers.append(w)
        models = list(set(w.get('model', 'unknown') for w in implementers))
        return {'available_implementers': len(implementers) or 1, 'target_models': models or ['unknown']}
    except Exception as e:
        logger.warning(f"Error fetching cluster topology from ES: {e}")
        return {'available_implementers': 1, 'target_models': ['unknown']}

def run_pm_dispatcher(task: Optional[dict[str, Any]] = None) -> AgentResult:
    logger.info("run_pm_dispatcher: started. getting system prompt.")
    system_prompt = _load_system_prompt('pm-dispatcher')
    logger.info("run_pm_dispatcher: getting cluster topology.")
    topology = _get_cluster_topology()
    logger.info(f"run_pm_dispatcher: got cluster topology {topology}. Checking codex app server.")
    
    # ── Complexity-aware instruction ──────────────────────────────────────
    # Prevent the PM from re-decomposing tasks that the planner already scoped.
    item_type = (task or {}).get('item_type', 'task')
    task_title = (task or {}).get('title', '')

    instruction = (
        f"You are the Program Manager. Analyze the task and the following cluster execution topology:\n"
        f"- Available Implementer Nodes: {topology['available_implementers']}\n"
        f"- Target Implementer Models: {', '.join(topology['target_models'])}\n\n"
    )

    # CRITICAL: Only decompose if the item is a parent container (epic/feature/story).
    # If the task is already item_type='task', it has been scoped by the planner — do NOT
    # create additional subtasks. This prevents the 2-task → 13-item explosion.
    if item_type == 'task':
        instruction += (
            "This item is already a TASK (leaf-level work item). It has been scoped by the planner "
            "and should NOT be decomposed further.\n"
            "Return action='compute_ready' to push it to the execution queue.\n"
            "Do NOT return action='decompose'. Do NOT create subtasks.\n"
        )
    else:
        instruction += (
            "COMPLEXITY-PROPORTIONAL DECOMPOSITION RULES:\n"
            "- If the task title describes a trivial change (URL update, typo fix, config change, "
            "single-file edit), return action='compute_ready' — do NOT decompose.\n"
            "- Only return action='decompose' if the work genuinely requires multiple independent "
            "implementation steps across different files or components.\n"
            "- When decomposing, create the MINIMUM number of subtasks needed. Each subtask must "
            "modify different files or components. Never create separate tasks for 'locate file' "
            "and 'make change'.\n"
            "- Never create subtasks that assume artifacts exist without evidence (e.g., "
            "'replace icon asset' when no SVG files were mentioned).\n\n"
        )
        if 'gpt-4' not in (str((task or {}).get('preferred_model') or '')).lower():
            instruction += (
                "The Implementer models are local/quantized — if decomposition is needed, "
                "keep subtasks tightly scoped (1 function/file per task) to reduce hallucination.\n"
            )
        else:
            instruction += (
                "The Implementer models are Frontier API models — you may chunk work "
                "into broader architecture scopes if decomposition is needed.\n"
            )
    
    if _task_uses_codex_app_server(task):
        logger.info("run_pm_dispatcher: Using codex app server. calling _run_codex_json_task.")
        prompt = (
            f"SYSTEM PROMPT:\n{system_prompt}\n\n{instruction}\n\n"
            'Return explicitly mapped JSON per the schema.\n'
            f"TASK JSON:\n{json.dumps(task or {}, indent=2)}"
        )
        response = _run_codex_json_task(
            prompt,
            _codex_json_schema_pm(),
            model=(task or {}).get('preferred_model') or _current_llm_model(),
            cwd=str(BASE),
        )
        logger.info("run_pm_dispatcher: finished _run_codex_json_task.")
    else:
        logger.info("run_pm_dispatcher: calling _call_ollama.")
        response = _call_ollama(
            system_prompt,
            {
                'instruction': instruction,
                'task': task or {},
            },
            model=(task or {}).get('preferred_model') or _current_llm_model(),
            task=task,
        )
        logger.info("run_pm_dispatcher: finished _call_ollama.")
    if response and isinstance(response, dict):
        return AgentResult(
            action=response.get('action', 'compute_ready'),
            summary=response.get('summary', 'Computed readiness for queued tasks.'),
            subtasks=response.get('subtasks') or [],
            metadata={'source': 'llm'},
        )
    return AgentResult(
        action='compute_ready',
        summary='Computed readiness for queued tasks (fallback).',
        subtasks=[],
        metadata={'source': 'fallback'},
    )
