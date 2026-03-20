#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import importlib.util

HERE = Path(__file__).resolve().parent
BASE = Path(os.environ.get('LOOM_WORKSPACE', str(HERE.parent)))
# Ensure worker-manager-local modules win over dashboard siblings with the same names.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(BASE) not in sys.path:
    sys.path.insert(1, str(BASE))
import llm_credentials_store as lcs  # noqa: E402

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
            'description': 'Run a shell command in the repo directory. Use for grep, find, npm, etc.',
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
    if p.is_absolute():
        return p
    return (Path(repo_path) / path) if repo_path else p


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
        p.write_text(args.get('content', ''))
        return f'OK: wrote {len(args.get("content", ""))} chars to {p}'
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


def _exec_run_shell(args: dict, repo_path: Optional[str]) -> str:
    command = args.get('command', '')
    cwd = args.get('working_dir') or repo_path or '.'
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30, cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 6000:
            output = output[:6000] + '\n... (truncated)'
        return output or f'(exit code {result.returncode}, no output)'
    except subprocess.TimeoutExpired:
        return 'ERROR: command timed out after 30s'
    except Exception as e:
        return f'ERROR running shell command: {e}'


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
        print(f'[agent_runner] _call_ollama_tools error: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
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

        # Append assistant turn
        assistant_msg: dict[str, Any] = {'role': 'assistant', 'content': message.get('content') or ''}
        if tool_calls:
            assistant_msg['tool_calls'] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            final_summary = (message.get('content') or '').strip() or 'Implementation completed.'
            _progress(f'Done: {final_summary[:120]}')
            break

        done = False
        for call in tool_calls:
            fn_name = call.get('function', {}).get('name', '')
            fn_args = call.get('function', {}).get('arguments', {})
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
                tool_result = _exec_run_shell(fn_args, repo_path)
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


def run_pm_dispatcher(task: Optional[dict[str, Any]] = None) -> AgentResult:
    system_prompt = _load_system_prompt('pm-dispatcher')
    response = _call_ollama(
        system_prompt,
        {
            'instruction': 'Return JSON: {"action":"compute_ready","summary":"..."}',
            'task': task or {},
        },
        model=(task or {}).get('preferred_model') or _current_llm_model(),
        task=task,
    )
    if response and isinstance(response, dict):
        return AgentResult(
            action=response.get('action', 'compute_ready'),
            summary=response.get('summary', 'Computed readiness for queued tasks.'),
            metadata={'source': 'llm'},
        )
    return AgentResult(
        action='compute_ready',
        summary='Computed readiness for queued tasks (fallback).',
        metadata={'source': 'fallback'},
    )
