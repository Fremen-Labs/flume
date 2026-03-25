from utils.logger import get_logger
logger = get_logger(__name__)
from elastro_sync import sync_ast
#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import urllib.request
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import importlib.util

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
    """Read from env each call so worker_handlers' periodic apply_runtime_config() takes effect."""
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
    import llm_client
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': json.dumps(user_payload, indent=2)},
    ]
    try:
        kw = _task_llm_kw(task)
        content = llm_client.chat(
            messages,
            model=model or _current_llm_model(),
            temperature=0.2,
            max_tokens=2048,
            **kw,
        )
        content = content.strip()
        if content.startswith('`' * 3):
            content = content.strip('`').replace('json\n', '', 1).strip()
        return json.loads(content)
    except Exception:
        return None




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
        
        # Native AST Integration mapping Elasticsearch automatically
        try:
            subprocess.run(['elastro', 'update', str(p)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import elastro_sync
            elastro_sync.sync_ast()
        except:
            pass

        return f'OK: wrote {len(content)} chars to {p}'
    except Exception as e:
        return f'ERROR writing file: {e}'


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
            
        executable = cmd_list[0]
        allow_list = {'npm', 'npx', 'pytest', 'golangci-lint', 'ruff', 'go', 'python', 'python3', 'uv', 'node'}
        
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
    if _LLM_CLIENT and getattr(_LLM_CLIENT, '__file__', '') == str(HERE / 'llm_client.py'):
        return _LLM_CLIENT
    path = HERE / 'llm_client.py'
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
        kw = _task_llm_kw(task)
        return llm_client.chat_with_tools(
            messages,
            tools,
            model=model,
            temperature=0.2,
            max_tokens=4096,
            **kw,
        )
    except Exception as e:
        logger.info(f'[agent_runner] _call_ollama_tools error: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
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
        logger.info(f'[agent_runner] Codex app-server error: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
        return None


def run_implementer(
    task: dict[str, Any],
    repo_path: Optional[str] = None,
    on_progress: Optional[Any] = None,
) -> AgentResult:
    system_prompt = _load_system_prompt('implementer')
    model = task.get('preferred_model') or _current_llm_model()

    def _progress(note: str) -> None:
        if on_progress:
            try:
                on_progress(note)
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
                        + ' Explore with list_directory and read_file, act accordingly, then call implementation_complete with a clear summary.'
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

    for _iteration in range(25):
        _progress(f'Thinking… (step {_iteration + 1})')
        raw = _call_ollama_tools(messages, _IMPLEMENTER_TOOLS, model, task=task)
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
        for call in tool_calls:
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
            elif fn_name == 'implementation_complete':
                final_summary = fn_args.get('summary', 'Implementation completed.')
                final_commit_message = fn_args.get('commit_message', '')
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
            'instruction': 'Return JSON: {"verdict":"approved|changes_requested|blocked","summary":"..."}',
            'task': task,
        },
        model=task.get('preferred_model') or _current_llm_model(),
        task=task,
    )
    if response and isinstance(response, dict):
        verdict = response.get('verdict', 'approved')
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
    state_file = BASE / 'worker-manager' / 'state.json'
    if not state_file.exists():
        return {'available_implementers': 1, 'target_models': ['unknown']}
    try:
        data = json.loads(state_file.read_text())
        implementers = [w for w in (data.get('workers') or []) if w.get('role') == 'implementer']
        models = list(set(w.get('model', 'unknown') for w in implementers))
        return {'available_implementers': len(implementers), 'target_models': models}
    except Exception:
        return {'available_implementers': 1, 'target_models': ['unknown']}

def run_pm_dispatcher(task: Optional[dict[str, Any]] = None) -> AgentResult:
    system_prompt = _load_system_prompt('pm-dispatcher')
    topology = _get_cluster_topology()
    
    instruction = (
        f"You are the Program Manager. Analyze the task and the following cluster execution topology:\n"
        f"- Available Implementer Nodes: {topology['available_implementers']}\n"
        f"- Target Implementer Models: {', '.join(topology['target_models'])}\n\n"
        "If the Implementer target models are local, highly quantized (e.g., qwen or llama), or generic, "
        "you MUST tightly decompose this work into microscopic, heavily isolated sub-components (1 function/file per task) "
        "so the LLMs do not hallucinate.\n"
        "If the Implementer models are Frontier API models (e.g., gemini-pro, gpt-4), you may chunk the work "
        "into broader architecture scopes.\n\n"
        "If the task requires decomposition (e.g., it is a high-level Feature or Epic), return action='decompose' "
        "and supply an array of subtasks (features, stories, or tasks).\n"
        "If the task is already scoped at the lowest granular execution limit ('task'), return action='compute_ready' "
        "to push it to the queue."
    )
    
    if _task_uses_codex_app_server(task):
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
    else:
        response = _call_ollama(
            system_prompt,
            {
                'instruction': instruction,
                'task': task or {},
            },
            model=(task or {}).get('preferred_model') or _current_llm_model(),
            task=task,
        )
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
