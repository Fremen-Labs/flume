import json
import urllib.request
import urllib.error
from utils.exceptions import SAFE_EXCEPTIONS
import uuid
import threading
import time
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Optional

from utils.logger import get_logger
from utils.workspace import resolve_safe_workspace
from config import get_settings
from core.elasticsearch import es_upsert
from core.sessions_store import load_session, save_session, _utcnow_iso, _iso_elapsed_seconds
from core.counters import get_next_id_sequence, es_counter_set_hwm



logger = get_logger(__name__)
WORKSPACE_ROOT = resolve_safe_workspace()


def _planner_debug_log(event: str, **fields):
    # AP-6: planner-debug.log removed — structured debug output goes to stdout now.
    # Filter to DEBUG level so these are silent in default INFO deployments.
    logger.debug(json.dumps({'event': event, **fields}, ensure_ascii=False))


def _planner_runtime_config() -> dict:
    from server import _sync_llm_runtime_env, LLM_MODEL
    from llm_settings import load_effective_pairs, resolve_effective_ollama_base_url
    _sync_llm_runtime_env()
    pairs = load_effective_pairs(WORKSPACE_ROOT)
    provider = (pairs.get('LLM_PROVIDER') or get_settings().LLM_PROVIDER or 'ollama').strip().lower()
    model = (pairs.get('LLM_MODEL') or get_settings().LLM_MODEL or LLM_MODEL).strip()
    # FLUME_PLANNER_MODEL lets operators use a lighter/faster model for planning
    # independently of the agent model (e.g. qwen2.5-coder:7b for planning speed
    # while gemma4:26b handles code implementation).
    planner_model_override = get_settings().FLUME_PLANNER_MODEL.strip()
    if planner_model_override:
        model = planner_model_override
    if provider == 'ollama':
        base_url = resolve_effective_ollama_base_url(pairs).strip()
    else:
        # explicitly handle None to allow "" (empty string) to pass natively to llm_client
        if pairs.get('LLM_BASE_URL') is not None:
            base_url = str(pairs.get('LLM_BASE_URL')).strip()
        elif get_settings().LLM_BASE_URL is not None:
            base_url = get_settings().LLM_BASE_URL.strip()
        else:
            base_url = ''
        # When base_url is empty for a managed provider, resolve the provider's
        # default URL from the catalog rather than falling back to localhost:11434.
        if not base_url and provider != 'ollama':
            from llm_settings import PROVIDER_CATALOG  # type: ignore
            for entry in PROVIDER_CATALOG:
                if entry.get('id') == provider:
                    base_url = (entry.get('baseUrlDefault') or '').rstrip('/')
                    break
            # Also check if 'grok' should map to 'xai' catalog entry
            if not base_url and provider == 'grok':
                for entry in PROVIDER_CATALOG:
                    if entry.get('id') == 'xai':
                        base_url = (entry.get('baseUrlDefault') or '').rstrip('/')
                        break
    parsed = urlparse(base_url) if base_url else None
    host = parsed.netloc or parsed.path if parsed else ''
    cfg = {
        'provider': provider,
        'model': model,
        'baseUrl': base_url,
        'host': host,
        'usingCodexAppServer': _planner_should_use_codex_app_server(),
    }
    _planner_debug_log(
        'runtime_config',
        provider=provider,
        model=model,
        baseUrl=base_url,
        envProvider=(get_settings().LLM_PROVIDER or '').strip(),
        envModel=(get_settings().LLM_MODEL or '').strip(),
        envBaseUrl=(get_settings().LLM_BASE_URL or '').strip(),
        pairProvider=(pairs.get('LLM_PROVIDER') or '').strip(),
        pairModel=(pairs.get('LLM_MODEL') or '').strip(),
        pairBaseUrl=(pairs.get('LLM_BASE_URL') or '').strip(),
        pairLocalOllamaBaseUrl=(pairs.get('LOCAL_OLLAMA_BASE_URL') or '').strip(),
    )
    return cfg


def _planner_request_timeout_seconds(config: Optional[dict] = None) -> int:
    cfg = config or _planner_runtime_config()
    provider = (cfg.get('provider') or '').lower()
    base_url = (cfg.get('baseUrl') or '').lower()
    default_timeout = int(str(get_settings().FLUME_PLANNER_TIMEOUT_SECONDS))
    if provider == 'ollama' or ('11434' in base_url) or ('ollama' in base_url):
        return max(default_timeout, 300)
    return default_timeout


def _build_planning_status(stage: str = 'queued') -> dict:
    cfg = _planner_runtime_config()
    return {
        'stage': stage,
        'provider': cfg.get('provider'),
        'model': cfg.get('model'),
        'baseUrl': cfg.get('baseUrl'),
        'host': cfg.get('host'),
        'usingCodexAppServer': cfg.get('usingCodexAppServer'),
        'connectionTestStartedAt': None,
        'connectionTestDurationMs': None,
        'connectionTestOk': None,
        'connectionTestResult': None,
        'requestStartedAt': None,
        'requestElapsedSeconds': None,
        'timeoutSeconds': _planner_request_timeout_seconds(cfg),
        'failureText': None,
        'lastUpdatedAt': _utcnow_iso(),
    }


def _update_planning_status(session: dict, **updates) -> dict:
    status = session.get('planningStatus') or _build_planning_status()
    status.update({k: v for k, v in updates.items() if v is not None or k in updates})
    started_at = status.get('requestStartedAt')
    elapsed = _iso_elapsed_seconds(started_at)
    if elapsed is not None:
        status['requestElapsedSeconds'] = elapsed
    status['lastUpdatedAt'] = _utcnow_iso()
    session['planningStatus'] = status
    return status


def _test_planner_connection(status: dict) -> dict:
    """Probe the configured LLM endpoint and update status with the result."""
    provider  = (status.get('provider') or '').lower().strip()
    base_url  = (status.get('baseUrl') or '').rstrip('/')
    api_key   = (get_settings().LLM_API_KEY or '').strip()
    if not api_key:
        try:
            from llm_settings import _openbao_get_all
            import pathlib
            bao = _openbao_get_all(pathlib.Path(get_settings().FLUME_DATA_DIR))
            api_key = bao.get('LLM_API_KEY', '').strip()
        except SAFE_EXCEPTIONS:
            pass
    started = time.time()
    status['connectionTestStartedAt'] = _utcnow_iso()

    # ── Build provider-specific test URL & headers ─────────────────────────

    headers: dict = {}
    url: str = base_url   # default fallback

    if provider == 'ollama':
        # Ollama local inference is orchestrated by the Flume Gateway Node Mesh.
        # Test the Gateway's registry endpoint to confirm mesh connectivity.
        gw_url = get_settings().GATEWAY_URL
        
        # Fallback to localhost if running outside Docker or during DNS race condition
        if 'gateway:' in gw_url:
            import socket
            from urllib.parse import urlparse
            try:
                parsed = urlparse(gw_url)
                if parsed.hostname:
                    socket.gethostbyname(parsed.hostname)
            except SAFE_EXCEPTIONS:
                gw_url = gw_url.replace('gateway', '127.0.0.1')
                
        url = f"{gw_url}/api/nodes"
    else:
        if not base_url:
            status['connectionTestOk']     = False
            status['connectionTestResult'] = (
                f'No base URL configured for provider "{provider}". '
                'Check Settings → LLM Provider.'
            )
            status['connectionTestDurationMs'] = round((time.time() - started) * 1000, 1)
            return status

        # Apply specific routing rules, identical to how tests were previously performed
        # If base_url is still the Ollama default but provider is cloud, override it
        if base_url == "http://localhost:11434" and provider not in ("ollama", "exo"):
            if provider in ("xai", "grok"):
                base_url = "https://api.x.ai"
            elif provider == "openai":
                base_url = "https://api.openai.com"
            elif provider == "anthropic":
                base_url = "https://api.anthropic.com"
                
        if provider == 'exo':
            norm = base_url
            if norm.endswith('/v1'):
                norm = norm[:-3]
            url = norm.rstrip('/') + '/v1/models'
        elif provider == 'anthropic':
            root = base_url if base_url else 'https://api.anthropic.com'
            url  = root.rstrip('/') + '/v1/models'
            if api_key:
                headers['x-api-key']         = api_key
                headers['anthropic-version']  = '2023-06-01'
        elif provider == 'gemini':
            url = 'https://generativelanguage.googleapis.com/v1beta/models'
            if api_key:
                url += f'?key={api_key}'
        elif provider in ('openai', 'openai_compatible', 'xai', 'grok', 'mistral', 'cohere'):
            # OpenAI-compatible family: strip a trailing /v1 that catalog URLs
            # (e.g. Grok → https://api.x.ai/v1) already include, then append /v1/models.
            norm = base_url
            if norm.endswith('/v1'):
                norm = norm[:-3]
            url = norm.rstrip('/') + '/v1/models'
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'
        else:
            norm = base_url
            if norm.endswith('/v1'):
                norm = norm[:-3]
            url = norm.rstrip('/') + '/v1/models'
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'

    # ── Execute the probe ──────────────────────────────────────────────────
    try:
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            status_code = getattr(resp, 'status', 200)
            status['connectionTestOk']     = True
            if provider == 'ollama':
                status['connectionTestResult'] = f'NODE MESH connection OK — Gateway HTTP {status_code}'
            else:
                status['connectionTestResult'] = f'{provider.upper()} connection OK — {url} responded HTTP {status_code}'
    except urllib.error.HTTPError as he:
        status['connectionTestOk']     = False
        if provider == 'ollama':
             status['connectionTestResult'] = f'NODE MESH connection FAILED — Gateway HTTP {he.code}: {he.reason}'
        else:
             status['connectionTestResult'] = f'{provider.upper()} connection FAILED — {url} returned HTTP {he.code}: {he.reason}'
    except SAFE_EXCEPTIONS as exc:
        status['connectionTestOk']     = False
        if provider == 'ollama':
             status['connectionTestResult'] = f'NODE MESH connection FAILED — {exc}'
        else:
             status['connectionTestResult'] = f'{provider.upper()} connection FAILED — {exc}'
        
        logger.warning(f"Plan New Work modal connection test failed: {exc}")

    status['connectionTestDurationMs'] = round((time.time() - started) * 1000, 1)
    return status




def _complete_planner_turn(session: dict, message: str, plan: Optional[dict], plan_source: str, failure_text: Optional[str] = None):
    session['draftPlan'] = plan
    session['draftPlanSource'] = plan_source
    session['messages'].append({
        'from': 'agent',
        'text': message,
        'plan': plan,
        'agent_role': session.get('agent_role', 'intake'),
    })
    _update_planning_status(
        session,
        stage='ready' if not failure_text else 'failed',
        failureText=failure_text,
    )
    save_session(session)



PLANNER_SYSTEM_PROMPT = """\
You are a senior technical planner. The user describes what they want built and you \
break it down into a structured hierarchy of Epics, Features, Stories, and Tasks.

RULES:
- Always respond with valid JSON containing exactly two keys: "message" and "plan".
- "message" is your conversational reply to the user (markdown is fine).
- "plan" is the current complete work breakdown with this exact structure:
  {
    "complexityScore": <1-10>,
    "epics": [
      {
        "id": "epic-<n>",
        "title": "...",
        "description": "...",
        "features": [
          {
            "id": "feat-<n>",
            "title": "...",
            "stories": [
              {
                "id": "story-<n>",
                "title": "...",
                "acceptanceCriteria": ["..."],
                "tasks": [
                  { "id": "task-<n>", "title": "..." }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
- When the user asks to add, remove, or modify items, return the full updated plan.
- Use short, descriptive IDs (epic-1, feat-1, story-1, task-1, etc.).
- Only output the JSON object, nothing before or after it.

COMPLEXITY-PROPORTIONAL PLANNING (critical):
- Match task granularity to ACTUAL complexity. Do NOT over-decompose simple work.
- TRIVIAL changes (update a URL, fix a typo, change a config value, swap a constant):
  produce 1-2 tasks MAXIMUM. One task for the change, optionally one for verification.
- SINGLE-COMPONENT changes (add a feature to one module, update one API endpoint):
  produce 3-5 tasks.
- CROSS-CUTTING changes (new API + UI + database + tests): use full decomposition.
- NEVER create separate tasks for "locate the file" and "make the change" — the
  implementer agent has AST search and file-read tools built in.
- NEVER create a task that assumes an artifact exists without evidence (e.g.,
  "replace the SVG icon" when no SVG was mentioned by the user).
- Combine all verification steps (lint, test, visual check) into ONE task unless
  the project has distinct test suites requiring separate execution.
- A single-file edit should NEVER produce more than 3 tasks total.\
"""


def _planner_should_use_codex_app_server() -> bool:
    provider = (get_settings().LLM_PROVIDER or '').strip().lower()
    if provider != 'openai':
        return False
    force = (get_settings().FLUME_PLANNER_USE_CODEX_APP_SERVER or 'auto').strip().lower()
    if force in ('0', 'false', 'off', 'no'):
        return False
    has_oauth = bool((get_settings().OPENAI_OAUTH_STATE_FILE or '').strip() or (get_settings().OPENAI_OAUTH_STATE_JSON or '').strip())
    api_key = (get_settings().LLM_API_KEY or '').strip()
    if not has_oauth and force not in ('1', 'true', 'on', 'yes'):
        return False
    if api_key.startswith('sk-') or api_key.startswith('sk_'):
        return False
    try:
        import codex_app_server  # type: ignore

        st = codex_app_server.status()
        return bool(st.get('codexAuthFilePresent')) and bool(st.get('codexOnPath') or st.get('npxOnPath'))
    except SAFE_EXCEPTIONS:
        return False


def call_planner_model(messages, timeout_seconds: Optional[int] = None):
    """Call the configured planner backend and return the assistant response text."""
    cfg = _planner_runtime_config()
    from server import LLM_MODEL
    model = cfg.get('model') or LLM_MODEL
    timeout_seconds = timeout_seconds or _planner_request_timeout_seconds(cfg)
    logger.info(
        json.dumps({
            'event': 'planner_request',
            'agent_role': 'intake',
            'model': model,
            'timeoutSeconds': timeout_seconds,
            'messageCount': len(messages or []),
        }, ensure_ascii=False)
    )
    if cfg.get('usingCodexAppServer'):
        import codex_app_server_client  # type: ignore

        return codex_app_server_client.planner_chat(
            messages,
            model=model,
            cwd=str(WORKSPACE_ROOT),
            timeout=timeout_seconds,
        )

    from utils import llm_client
    return llm_client.chat(
        messages,
        model=model,
        temperature=0.3,
        max_tokens=8192,
        timeout_seconds=timeout_seconds,
        ollama_think=False,
        agent_role='intake',
    )

def _strip_json_blocks(text: str) -> str:
    """Remove trailing ```json ... ``` or bare {...} JSON blocks from a message string."""
    # Remove fenced code blocks (```json ... ``` or ``` ... ```)
    text = re.sub(r'```(?:json)?\s*\{[\s\S]*?\}\s*```', '', text)
    # Remove any remaining fenced blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove bare top-level JSON objects that start on their own line
    text = re.sub(r'(?m)^\s*\{[\s\S]*\}\s*$', '', text)
    return text.strip()


def parse_llm_response(raw_text):
    """
    Extract message and plan from the LLM's JSON response.
    Tries direct JSON parse first, then regex extraction.
    Always strips any embedded JSON/code blocks from the message text.
    """
    cleaned = raw_text.strip()
    # Strip <think> reasoning blocks that disrupt JSON parsers
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', cleaned).strip()
    
    # Unwrap outer markdown fence if the entire response is wrapped
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)

    try:
        obj = json.loads(cleaned)
        if 'message' in obj and 'plan' in obj:
            return _strip_json_blocks(obj['message']), obj['plan']
    except json.JSONDecodeError:
        pass

    # Try to find an embedded JSON object
    json_match = re.search(r'\{[\s\S]*\}', cleaned)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            if 'message' in obj and 'plan' in obj:
                return _strip_json_blocks(obj['message']), obj['plan']
        except json.JSONDecodeError:
            pass

    # Fall back: return the raw text with any JSON/code blocks scrubbed out
    return _strip_json_blocks(cleaned), None


def _planner_llm_error_hint(err: str) -> str:
    """Short user-facing hint after a planner LLM call fails (OAuth, connectivity, etc.)."""
    timeout_secs = _planner_request_timeout_seconds()
    if 'timed out' in err.lower() or 'TimeoutError' in err or 'Read timed out' in err:
        return (
            f' Planning exceeded the {timeout_secs}s timeout. Your model may be too slow for complex plans. '
            f'Try: (1) use a smaller/faster model, (2) simplify your prompt, or '
            f'(3) increase the timeout with `flume start --planner-timeout {timeout_secs * 2}`.'
        )
    if 'Connection refused' in err or 'Errno 111' in err:
        return (
            ' Check that LLM_PROVIDER/LLM_BASE_URL match your setup (e.g. OpenAI + gpt-5.4, not Ollama on localhost). '
            'Save Settings again or run ./flume restart after changing .env.'
        )
    if '401' in err or 'Unauthorized' in err:
        if 'model.request' in err:
            return (
                ' ChatGPT/Codex **browser OAuth** tokens do not include **model.request** (OpenAI does not allow '
                'that scope on /oauth/authorize for the Codex client), but **/v1/chat/completions** still requires it. '
                'Plan New Work and similar calls need an OpenAI **platform API key** (sk-…): Settings → LLM → '
                'Auth mode → API Key, from https://platform.openai.com/api-keys — OAuth alone cannot satisfy this API.'
            )
        if 'api.responses.write' in err:
            return (
                ' Token lacks api.responses.write for /v1/responses. With current Flume, Codex OAuth usually routes '
                'to chat/completions instead; if you still see responses in the error, restart all services. '
                'Otherwise use a platform sk- API key.'
            )
        return (
            ' For ChatGPT/Codex OAuth, the access token may be expired: open Settings → LLM and use '
            '"Refresh OAuth token", or run ./flume codex-oauth refresh, then save settings.'
        )
    return ''


def build_llm_messages(session):
    """Build the Ollama message list from a session's conversation history."""
    msgs = [{'role': 'system', 'content': PLANNER_SYSTEM_PROMPT}]

    for m in session.get('messages', []):
        if m['from'] == 'user':
            content = m['text']
            if m.get('plan'):
                content += f'\n\nCurrent plan state:\n```json\n{json.dumps(m["plan"], indent=2)}\n```'
            msgs.append({'role': 'user', 'content': content})
        elif m['from'] == 'agent':
            response_obj = {'message': m['text'], 'plan': m.get('plan', {})}
            msgs.append({'role': 'assistant', 'content': json.dumps(response_obj)})

    return msgs


def create_planning_session(repo, prompt):
    session_id = f'plan-{uuid.uuid4().hex[:12]}'
    session = {
        'id': session_id,
        'repo': repo,
        'status': 'active',
        'agent_role': 'intake',
        'messages': [
            {'from': 'user', 'text': prompt, 'plan': None}
        ],
        'draftPlan': None,
        'draftPlanSource': None,
        'planningStatus': _build_planning_status(stage='queued'),
        'created_at': _utcnow_iso(),
        'updated_at': _utcnow_iso(),
    }
    save_session(session)
    
    # Run connection test synchronously to fail fast
    status = _update_planning_status(session, stage='testing_connection')
    _test_planner_connection(status)
    if status.get('connectionTestOk') is False:
        status['stage'] = 'failed'
        status['failureText'] = status.get('connectionTestResult') or 'Connection test failed.'
        save_session(session)
        raise ValueError(status['failureText'])

    session_copy = dict(session)
    threading.Thread(target=_run_initial_planning, args=(session_copy,), daemon=True).start()
    return session


def _run_initial_planning(session: dict):
    if not session:
        return

    # Skip testing connection, already done
    status = session.get('planningStatus', {})

    llm_messages = build_llm_messages(session)
    timeout_seconds = _planner_request_timeout_seconds(status)
    _update_planning_status(session, stage='requesting_plan', requestStartedAt=_utcnow_iso(), timeoutSeconds=timeout_seconds, failureText=None)
    save_session(session)
    message = None
    plan = None
    llm_error = None
    try:
        raw = call_planner_model(llm_messages, timeout_seconds=timeout_seconds)
        message, plan = parse_llm_response(raw)
    except SAFE_EXCEPTIONS as e:
        llm_error = str(e)[:300]

    if llm_error:
        hint = _planner_llm_error_hint(llm_error)
        message = (
            f"The planner could not reach the language model ({llm_error}).{hint}\n\n"
            "Below is an editable PLACEHOLDER outline derived only from your prompt — "
            "not an AI-generated breakdown. Edit the tree manually or fix LLM auth and start a new plan."
        )
        plan = placeholder_plan(session.get('repo') or '', session['messages'][0].get('text') or '')
        _complete_planner_turn(session, message, plan, 'placeholder', llm_error)
        return
    if not plan or not plan.get('epics'):
        plan = placeholder_plan(session.get('repo') or '', session['messages'][0].get('text') or '')
        prior = (message or '').strip()
        if prior:
            message = (
                f"{prior}\n\n"
                "Note: The model did not return a valid plan JSON, so the work breakdown below is a "
                "placeholder template you can edit manually."
            )
        else:
            message = (
                "The model did not return a usable plan structure. "
                "Below is an editable placeholder template; try again or adjust your LLM settings."
            )
        _complete_planner_turn(session, message, plan, 'placeholder')
        return
    _complete_planner_turn(session, message, plan, 'llm')


def refine_session(session_id, user_text, current_plan):
    session = load_session(session_id)
    if not session:
        return None

    session['messages'].append({
        'from': 'user',
        'text': user_text,
        'plan': current_plan,
    })

    if current_plan:
        session['draftPlan'] = current_plan

    status = _update_planning_status(session, stage='testing_connection')
    _test_planner_connection(status)
    if status.get('connectionTestOk') is False:
        status['stage'] = 'failed'
        status['failureText'] = status.get('connectionTestResult') or 'Connection test failed.'
        save_session(session)
        raise ValueError(status['failureText'])
    save_session(session)

    llm_messages = build_llm_messages(session)
    timeout_seconds = _planner_request_timeout_seconds(session.get('planningStatus'))
    _update_planning_status(session, stage='requesting_plan', requestStartedAt=_utcnow_iso(), timeoutSeconds=timeout_seconds, failureText=None)
    save_session(session)
    try:
        raw = call_planner_model(llm_messages, timeout_seconds=timeout_seconds)
        message, plan = parse_llm_response(raw)
    except SAFE_EXCEPTIONS as e:
        err = str(e)[:300]
        hint = _planner_llm_error_hint(err)
        message = f"I encountered an issue processing your request. Please try again. (Error: {err}){hint}"
        plan = None
        _update_planning_status(session, stage='failed', failureText=err)

    if plan and plan.get('epics'):
        session['draftPlan'] = plan
        session['draftPlanSource'] = 'llm'
    else:
        plan = session['draftPlan']

    session['messages'].append({
        'from': 'agent',
        'text': message,
        'plan': plan,
        'agent_role': session.get('agent_role', 'intake'),
    })

    if session.get('planningStatus', {}).get('stage') != 'failed':
        _update_planning_status(session, stage='ready', failureText=None)
    save_session(session)
    return session


def placeholder_plan(repo: str, prompt: str):
    """
    Minimal epic/feature/story/task skeleton when the LLM is unavailable or returns no plan.

    Titles are intentionally labeled as placeholders so the UI is not mistaken for AI output.
    """
    title = (prompt.splitlines()[0] or 'New request').strip()
    if len(title) > 80:
        title = title[:77] + '...'
    epic_id = 'epic-1'
    feature_id = 'feat-1'
    story_id = 'story-1'
    task_id = 'task-1'
    return {
        'repo': repo,
        'epics': [
            {
                'id': epic_id,
                'title': title,
                'description': prompt,
                'features': [
                    {
                        'id': feature_id,
                        'title': '[Placeholder] Rename this feature',
                        'stories': [
                            {
                                'id': story_id,
                                'title': '[Placeholder] Rename this story',
                                'acceptanceCriteria': [
                                    '[Placeholder] Add acceptance criteria',
                                    '[Placeholder] Add another criterion',
                                ],
                                'tasks': [
                                    {
                                        'id': task_id,
                                        'title': '[Placeholder] Add a concrete task',
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


# Backward-compatible name for scripts/tests
simple_plan = placeholder_plan



def _extract_target_file(title: str) -> str | None:
    """Extract a likely filename target from a task title."""
    match = re.search(r'\b([\w\.\-]+\.(?:tsx|ts|js|jsx|py|go|html|css|md|json|yml|yaml))\b', title, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return None


def _coalesce_story_tasks(tasks: list[dict]) -> list[dict]:
    """Auto-merge adjacent tasks that modify the same file into a single compound task."""
    if not tasks:
        return []
    coalesced = []
    curr = dict(tasks[0])
    for task in tasks[1:]:
        t_file = _extract_target_file(task.get('title', ''))
        c_file = _extract_target_file(curr.get('title', ''))
        if t_file and c_file and t_file == c_file:
            curr['title'] = f"Compound Task: {curr.get('title', '')} (+ {task.get('title', '')})"
            curr_obj = curr.get('objective', '') or ''
            task_obj = task.get('objective', '') or ''
            curr['objective'] = f"{curr_obj}\n\n- {task.get('title', '')}: {task_obj}".strip()
            curr['_coalesced_count'] = curr.get('_coalesced_count', 1) + 1
        else:
            coalesced.append(curr)
            curr = dict(task)
    coalesced.append(curr)
    
    # Log any coalescing using the centralized logger
    for t in coalesced:
        if t.get('_coalesced_count', 1) > 1:
            logger.info("Task Coalescing Engine: Merged %d tasks into a single compound task targeting %s", 
                        t['_coalesced_count'], _extract_target_file(t.get('title', '')))
    return coalesced


def _count_plan_tasks(plan: dict) -> int:
    """Count total leaf tasks across all epics/features/stories after coalescing."""
    total = 0
    for epic in plan.get('epics') or []:
        for feat in epic.get('features') or []:
            for story in feat.get('stories') or []:
                coalesced = _coalesce_story_tasks(story.get('tasks') or [])
                total += len(coalesced)
    return total


def commit_plan(repo: str, plan: dict):
    """
    Translate a plan tree (epics/features/stories/tasks) into TASK_SCHEMA docs
    and index them into agent-task-records with initial statuses and owners.

    IDs are always freshly allocated by querying existing records, so numbers
    are never reused even after items are deleted.

    FAST PATH: When the plan has ≤2 total tasks, skip the epic/feature/story
    hierarchy entirely and create tasks directly as 'ready'. This eliminates
    structural overhead for trivial changes (e.g., URL updates, typo fixes).
    """
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    docs = []

    # ── Adaptive LLM Routing ────────────────────────────────────────────────
    complexity_score = plan.get('complexityScore', 5)
    fast_model = get_settings().FLUME_FAST_MODEL or 'o3-mini'
    routing_model = fast_model if complexity_score <= 3 else None
    if routing_model:
        logger.info(f"Adaptive Routing: complexity={complexity_score}, routing tasks to {routing_model}")

    # ── Fast path: ≤3 tasks → skip hierarchy, create tasks directly ────────
    total_tasks = _count_plan_tasks(plan)
    if 0 < total_tasks <= 3:
        task_seq = get_next_id_sequence('task')
        prev_task_id = None
        for epic in plan.get('epics') or []:
            for feat in epic.get('features') or []:
                for story in feat.get('stories') or []:
                    ac = story.get('acceptanceCriteria') or []
                    for task in _coalesce_story_tasks(story.get('tasks') or []):
                        task_id = f'task-{task_seq}'
                        task_seq += 1
                        task_doc = {
                            'id': task_id,
                            'title': task.get('title') or '',
                            'objective': epic.get('description') or story.get('title') or '',
                            'repo': repo,
                            'worktree': None,
                            'item_type': 'task',
                            'owner': 'implementer',
                            'assigned_agent_role': 'implementer',
                            'status': 'ready' if prev_task_id is None else 'planned',
                            'priority': 'normal',
                            'parent_id': None,
                            'depends_on': [prev_task_id] if prev_task_id else [],
                            'acceptance_criteria': ac,
                            'artifacts': [],
                            'last_update': now,
                            'needs_human': False,
                            'risk': 'medium',
                            'preferred_model': routing_model,
                        }
                        docs.append(task_doc)
                        prev_task_id = task_id
        es_counter_set_hwm('task', task_seq - 1)
        task_ids = [d['id'] for d in docs]
        logger.info(
            "commit_plan: FAST PATH — %d tasks created directly (no epic/feature/story hierarchy): %s",
            len(docs), task_ids,
        )
        results = []
        for d in docs:
            results.append(es_upsert('agent-task-records', d['id'], d))
        return docs, results

    # ── Standard path: full epic/feature/story/task hierarchy ───────────────
    # Allocate monotonically-increasing sequence numbers for each item type.
    # These are fetched once before the loop so we don't make N round-trips.
    epic_seq = get_next_id_sequence('epic')
    feat_seq = get_next_id_sequence('feat')
    story_seq = get_next_id_sequence('story')
    task_seq = get_next_id_sequence('task')

    epics = plan.get('epics') or []
    for epic in epics:
        epic_id = f'epic-{epic_seq}'
        epic_seq += 1
        epic_title = epic.get('title') or ''
        epic_desc = epic.get('description') or ''
        epic_doc = {
            'id': epic_id,
            'title': epic_title,
            'objective': epic_desc,
            'repo': repo,
            'worktree': None,
            'item_type': 'epic',
            'owner': 'pm',
            'status': 'planned',
            'priority': 'high',
            'parent_id': None,
            'depends_on': [],
            'acceptance_criteria': [],
            'artifacts': [],
            'last_update': now,
            'needs_human': False,
            'risk': 'medium',
        }
        docs.append(epic_doc)

        for feature in epic.get('features') or []:
            feat_id = f'feat-{feat_seq}'
            feat_seq += 1
            feat_title = feature.get('title') or ''
            feat_doc = {
                'id': feat_id,
                'title': feat_title,
                'objective': f"Feature of {epic_title}",
                'repo': repo,
                'worktree': None,
                'item_type': 'feature',
                'owner': 'pm',
                'status': 'planned',
                'priority': 'medium',
                'parent_id': epic_id,
                # depends_on drives UI hierarchy; features become ready when epic is done
                'depends_on': [epic_id],
                'acceptance_criteria': [],
                'artifacts': [],
                'last_update': now,
                'needs_human': False,
                'risk': 'medium',
            }
            docs.append(feat_doc)

            for story in feature.get('stories') or []:
                story_id = f'story-{story_seq}'
                story_seq += 1
                story_title = story.get('title') or ''
                ac = story.get('acceptanceCriteria') or []
                story_doc = {
                    'id': story_id,
                    'title': story_title,
                    'objective': f"Story for {feat_title}",
                    'repo': repo,
                    'worktree': None,
                    'item_type': 'story',
                    'owner': 'pm',
                    'status': 'planned',
                    'priority': 'medium',
                    'parent_id': feat_id,
                    'depends_on': [feat_id],
                    'acceptance_criteria': ac,
                    'artifacts': [],
                    'last_update': now,
                    'needs_human': False,
                    'risk': 'medium',
                }
                docs.append(story_doc)

                # Tasks within a story run sequentially: each depends on the
                # previous task so the implementer can't start task N+1 until
                # task N is fully done. The first task is immediately 'ready'.
                prev_task_id = None
                for task in _coalesce_story_tasks(story.get('tasks') or []):
                    task_id = f'task-{task_seq}'
                    task_seq += 1
                    task_title = task.get('title') or ''
                    task_doc = {
                        'id': task_id,
                        'title': task_title,
                        'objective': task.get('objective') or f"Task for {story_title}",
                        'repo': repo,
                        'worktree': None,
                        'item_type': 'task',
                        'owner': 'implementer',
                        'assigned_agent_role': 'implementer',
                        # First task in the story starts ready; subsequent ones
                        # are planned and get promoted after the previous task is done.
                        'status': 'ready' if prev_task_id is None else 'planned',
                        'priority': 'normal',
                        'parent_id': story_id,
                        # depends_on: previous task for ordering (UI hierarchy uses parent_id)
                        'depends_on': [prev_task_id] if prev_task_id else [],
                        'acceptance_criteria': ac,
                        'artifacts': [],
                        'last_update': now,
                        'needs_human': False,
                        'risk': 'medium',
                        'preferred_model': routing_model,
                    }
                    docs.append(task_doc)
                    prev_task_id = task_id

    # Persist high-water marks atomically in ES so deleted records never cause id recycling.
    # epic_seq/feat_seq/story_seq/task_seq have already been incremented once
    # beyond the last allocated value, so subtract 1 to get the actual max used.
    for prefix, seq in (('epic', epic_seq), ('feat', feat_seq), ('story', story_seq), ('task', task_seq)):
        es_counter_set_hwm(prefix, seq - 1)

    results = []
    for d in docs:
        results.append(es_upsert('agent-task-records', d['id'], d))
    return docs, results
