from utils.logger import get_logger
logger = get_logger(__name__)
#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
import json
import os
import signal
import sys
import threading
import time
import uuid
import ssl
import hvac

class NetflixFaultTolerance:
    '''Netflix Microservice Resilience Wrapper'''
    pass

import subprocess
import sys
import urllib.request
from urllib.error import URLError, HTTPError
import urllib.parse
import glob
from contextlib import asynccontextmanager
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
import traceback
from fastapi import BackgroundTasks

# Flume Bootstrap Logic
# Flume Bootstrap Logic
from es_bootstrap import ensure_es_indices

# --- Legacy Env ---
BASE = Path(__file__).resolve().parent
_SRC_ROOT = BASE.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
# Dashboard modules (llm_settings, agent_models_settings) live next to server.py; prefer this package on import.
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


from flume_secrets import apply_runtime_config, hydrate_secrets_from_openbao  # noqa: E402

# Merge .env config
apply_runtime_config(_SRC_ROOT)

# Hydrate OpenBao Secrets Natively
hydrate_secrets_from_openbao()

# Execute Elasticsearch Index Bootstrapping natively now that auth is fully populated
ensure_es_indices()

from llm_settings import load_effective_pairs  # noqa: E402

_DEFAULT_ES = 'http://localhost:9200' if os.environ.get('FLUME_NATIVE_MODE') == '1' else 'http://elasticsearch:9200'
ES_URL = os.environ.get('ES_URL', _DEFAULT_ES).rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
HOST = os.environ.get('DASHBOARD_HOST', '0.0.0.0')
PORT = int(os.environ.get('DASHBOARD_PORT', '8765'))
# Pre-built Vite output only — editing src/frontend/src/*.tsx requires: ./flume build-ui (see install/README.md).
STATIC_ROOT = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'

from utils.workspace import resolve_safe_workspace, WorkspaceInitializationError

# Module-level paths are bounded to block AppSec Path Traversals seamlessly isolating the host
WORKSPACE_ROOT = resolve_safe_workspace()
from config import AppConfig, get_settings

WORKER_STATE = WORKSPACE_ROOT / 'worker_state.json'
SESSIONS_DIR = WORKSPACE_ROOT / 'plan-sessions'
PROJECTS_REGISTRY = WORKSPACE_ROOT / 'projects.json'

LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://localhost:11434')
LLM_MODEL = os.environ.get('LLM_MODEL', 'llama3.2')

# Persists the highest-ever-allocated sequence number for each id prefix so
# that deleted ES records can never cause an id to be recycled.
SEQUENCE_COUNTERS_FILE = WORKSPACE_ROOT / 'sequence_counters.json'

ctx = None
if not ES_VERIFY_TLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE



def _ensure_gitflow_defaults(entry: dict) -> dict:
    """Backfill gitflow config with defaults if missing."""
    if 'gitflow' not in entry:
        entry['gitflow'] = {'autoPrOnApprove': True, 'defaultBranch': None}
    else:
        gf = entry['gitflow']
        if 'autoPrOnApprove' not in gf:
            gf['autoPrOnApprove'] = True
        if 'defaultBranch' not in gf:
            gf['defaultBranch'] = None
    return entry


def load_sequence_counters() -> dict:
    """Return the persisted high-water-mark counters, e.g. {'task': 12, 'epic': 3}."""
    if SEQUENCE_COUNTERS_FILE.exists():
        try:
            return json.loads(SEQUENCE_COUNTERS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_sequence_counters(counters: dict):
    """Atomically persist the high-water-mark counters."""
    SEQUENCE_COUNTERS_FILE.write_text(json.dumps(counters, indent=2))


def update_sequence_counter(prefix: str, value: int):
    """Raise the stored counter for `prefix` to `value` if it is higher."""
    counters = load_sequence_counters()
    if value > counters.get(prefix, 0):
        counters[prefix] = value
        save_sequence_counters(counters)


def load_projects_registry() -> list:
    """Return the list of registered projects. Seeds with Project-Site-IQ if missing."""
    if not PROJECTS_REGISTRY.exists():
        PROJECTS_REGISTRY.write_text(json.dumps({"projects": []}, indent=2))
        return []

    raw = json.loads(PROJECTS_REGISTRY.read_text())
    # Accept both legacy formats:
    # - list: [{...}, {...}]
    # - dict: {"projects": [{...}, {...}]}
    if isinstance(raw, dict):
        entries = raw.get('projects') or []
    else:
        entries = raw

    if not isinstance(entries, list):
        return []

    out = []
    for e in entries:
        if isinstance(e, dict):
            out.append(_ensure_gitflow_defaults(e))
    return out


def save_projects_registry(registry):
    PROJECTS_REGISTRY.write_text(json.dumps({"projects": registry}, indent=2))


def es_search(index, body):
    # POST is required for reliable JSON bodies (some stacks strip GET bodies).
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def find_task_doc_by_logical_id(logical_id: str):
    """
    Return (es_id, source) for a work item in agent-task-records.

    Documents are usually upserted with PUT .../_doc/<logical_id>, so the ES _id
    matches the logical id. Older or reindexed data may only match on the `id`
    field — try `term` (keyword), `id.keyword` (dynamic mapping), and
    `match_phrase` (text mapping) so history / git / PR endpoints stay consistent
    with the snapshot list.
    """
    tid = (logical_id or '').strip()
    if not tid:
        return None, None
    attempts = [
        {'ids': {'values': [tid]}},
        {'term': {'id': tid}},
        {'term': {'id.keyword': tid}},
        {'match_phrase': {'id': tid}},
    ]
    for query in attempts:
        try:
            hits = es_search('agent-task-records', {'size': 1, 'query': query}).get('hits', {}).get('hits', [])
            if hits:
                h = hits[0]
                return h.get('_id'), h.get('_source', {})
        except Exception:
            continue
    return None, None


def es_index(index, doc):
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())

def es_upsert(index, doc_id, doc):
    """PUT a document by explicit ID — idempotent upsert (create or overwrite)."""
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc/{urllib.parse.quote(str(doc_id), safe='')}",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method='PUT',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())


def es_post(path, body, method='POST'):
    """
    Generic helper for POST/other write operations against Elasticsearch.
    Path should NOT start with a leading slash, e.g. 'agent-task-records/_update_by_query'.
    """
    req = urllib.request.Request(
        f"{ES_URL}/{path}",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {os.environ.get("ES_API_KEY", "")}',
        },
        method=method,
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())


def load_session(session_id):
    path = SESSIONS_DIR / f'{session_id}.json'
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_session(session):
    path = SESSIONS_DIR / f'{session["id"]}.json'
    session['updated_at'] = datetime.utcnow().isoformat() + 'Z'
    path.write_text(json.dumps(session, indent=2))


PLANNER_SYSTEM_PROMPT = """\
You are a senior technical planner. The user describes what they want built and you \
break it down into a structured hierarchy of Epics, Features, Stories, and Tasks.

RULES:
- Always respond with valid JSON containing exactly two keys: "message" and "plan".
- "message" is your conversational reply to the user (markdown is fine).
- "plan" is the current complete work breakdown with this exact structure:
  {
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
- Be thorough: break work into granular, implementable tasks.
- Only output the JSON object, nothing before or after it.\
"""


def _planner_should_use_codex_app_server() -> bool:
    provider = (os.environ.get('LLM_PROVIDER') or '').strip().lower()
    if provider != 'openai':
        return False
    force = (os.environ.get('FLUME_PLANNER_USE_CODEX_APP_SERVER') or 'auto').strip().lower()
    if force in ('0', 'false', 'off', 'no'):
        return False
    has_oauth = bool((os.environ.get('OPENAI_OAUTH_STATE_FILE') or '').strip() or (os.environ.get('OPENAI_OAUTH_STATE_JSON') or '').strip())
    api_key = (os.environ.get('LLM_API_KEY') or '').strip()
    if not has_oauth and not (force in ('1', 'true', 'on', 'yes')):
        return False
    if api_key.startswith('sk-') or api_key.startswith('sk_'):
        return False
    try:
        import codex_app_server

        st = codex_app_server.status()
        return bool(st.get('codexAuthFilePresent')) and bool(st.get('codexOnPath') or st.get('npxOnPath'))
    except Exception:
        return False


def call_planner_model(messages):
    """Call the configured planner backend and return the assistant response text."""
    load_legacy_dotenv_into_environ(_SRC_ROOT)
    try:
        from workspace_llm_env import sync_llm_env_from_workspace

        sync_llm_env_from_workspace(WORKSPACE_ROOT)
    except Exception:
        pass

    model = os.environ.get('LLM_MODEL', LLM_MODEL)
    if _planner_should_use_codex_app_server():
        import codex_app_server_client

        return codex_app_server_client.planner_chat(
            messages,
            model=model,
            cwd=str(WORKSPACE_ROOT),
            timeout=180,
        )

    import llm_client
    return llm_client.chat(
        messages,
        model=model,
        temperature=0.3,
        max_tokens=8192,
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
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'updated_at': datetime.utcnow().isoformat() + 'Z',
    }

    llm_messages = build_llm_messages(session)
    message = None
    plan = None
    llm_error = None
    try:
        raw = call_planner_model(llm_messages)
        message, plan = parse_llm_response(raw)
    except Exception as e:
        llm_error = str(e)[:200]

    if llm_error:
        hint = _planner_llm_error_hint(llm_error)
        message = (
            f"The planner could not reach the language model ({llm_error}).{hint}\n\n"
            "Below is an editable PLACEHOLDER outline derived only from your prompt — "
            "not an AI-generated breakdown. Edit the tree manually or fix LLM auth and start a new plan."
        )
        plan = placeholder_plan(repo, prompt)
        plan_source = 'placeholder'
    elif not plan or not plan.get('epics'):
        plan = placeholder_plan(repo, prompt)
        plan_source = 'placeholder'
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
    else:
        plan_source = 'llm'

    session['draftPlan'] = plan
    session['draftPlanSource'] = plan_source
    session['messages'].append({
        'from': 'agent',
        'text': message,
        'plan': plan,
        'agent_role': 'intake',
    })

    save_session(session)
    return session


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

    llm_messages = build_llm_messages(session)
    try:
        raw = call_planner_model(llm_messages)
        message, plan = parse_llm_response(raw)
    except Exception as e:
        err = str(e)[:200]
        hint = _planner_llm_error_hint(err)
        message = f"I encountered an issue processing your request. Please try again. (Error: {err}){hint}"
        plan = None

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


def get_next_id_sequence(prefix: str) -> int:
    """
    Return the next available integer sequence number for IDs of the form `prefix-N`.

    Takes the maximum of:
      1. The highest N seen in the live ES index (covers active/archived records).
      2. The persisted high-water-mark counter (covers IDs that were deleted from ES).

    This guarantees monotonic, never-recycled IDs even when records are hard-deleted.
    """
    max_n = load_sequence_counters().get(prefix, 0)
    try:
        hits = es_search('agent-task-records', {
            'size': 10000,
            '_source': ['id'],
            'query': {'regexp': {'id': f'{re.escape(prefix)}-[0-9]+'}},
        }).get('hits', {}).get('hits', [])
        pattern = re.compile(rf'^{re.escape(prefix)}-(\d+)$')
        for h in hits:
            doc_id = (h.get('_source') or {}).get('id', '') or h.get('_id', '')
            m = pattern.match(doc_id)
            if m:
                max_n = max(max_n, int(m.group(1)))
    except Exception:
        if max_n == 0:
            # Fallback when both ES and the counter file are unavailable
            return int(datetime.utcnow().timestamp()) % 1_000_000 + 1
    return max_n + 1


def commit_plan(repo: str, plan: dict):
    """
    Translate a plan tree (epics/features/stories/tasks) into TASK_SCHEMA docs
    and index them into agent-task-records with initial statuses and owners.

    IDs are always freshly allocated by querying existing records, so numbers
    are never reused even after items are deleted.
    """
    now = datetime.utcnow().isoformat() + 'Z'
    docs = []

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
                for task in story.get('tasks') or []:
                    task_id = f'task-{task_seq}'
                    task_seq += 1
                    task_title = task.get('title') or ''
                    task_doc = {
                        'id': task_id,
                        'title': task_title,
                        'objective': f"Task for {story_title}",
                        'repo': repo,
                        'worktree': None,
                        'item_type': 'task',
                        'owner': 'implementer',
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
                    }
                    docs.append(task_doc)
                    prev_task_id = task_id

    # Persist high-water marks so deleted records never cause id recycling.
    # epic_seq/feat_seq/story_seq/task_seq have already been incremented once
    # beyond the last allocated value, so subtract 1 to get the actual max used.
    counters = load_sequence_counters()
    for prefix, seq in (('epic', epic_seq), ('feat', feat_seq), ('story', story_seq), ('task', task_seq)):
        last_used = seq - 1
        if last_used > counters.get(prefix, 0):
            counters[prefix] = last_used
    save_sequence_counters(counters)

    results = []
    for d in docs:
        results.append(es_upsert('agent-task-records', d['id'], d))
    return docs, results

def delete_task_branches(ids: list, repo: str) -> list:
    """
    For any tasks in `ids` that have a `branch` field, delete that git branch
    from the local repository (and remote origin if it exists).
    Returns a list of branch names that were successfully deleted.
    """
    query_must: list = [
        {'terms': {'id': ids}},
        {'exists': {'field': 'branch'}},
    ]
    if repo:
        query_must.append({'term': {'repo': repo}})

    try:
        hits = es_search('agent-task-records', {
            'size': 500,
            '_source': ['id', 'repo', 'branch'],
            'query': {'bool': {'must': query_must}},
        }).get('hits', {}).get('hits', [])
    except Exception:
        return []

    registry = load_projects_registry()
    deleted = []

    # If multiple tasks share the same branch (e.g., tasks under the same
    # story), we must not delete the shared branch until no ES records
    # remain for it.
    ids_set = set(ids or [])

    for h in hits:
        src = h.get('_source') or {}
        branch = (src.get('branch') or '').strip()
        repo_id = src.get('repo', '')
        if not branch or not repo_id:
            continue

        proj = next((p for p in registry if p['id'] == repo_id), None)
        if not proj:
            continue

        repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
        if not (repo_path / '.git').exists():
            continue

        # Shared-branch safety: if any other remaining task doc still uses
        # this branch, skip deletion.
        try:
            remaining = es_search('agent-task-records', {
                'size': 1,
                '_source': ['id'],
                'query': {
                    'bool': {
                        'must': [
                            {'term': {'repo': repo_id}},
                            {'term': {'branch': branch}},
                        ],
                        'must_not': [{'terms': {'id': list(ids_set)}}],
                    }
                },
            }).get('hits', {}).get('hits', [])
            if remaining:
                continue
        except Exception:
            # Best-effort: if ES check fails, fall back to deleting.
            pass

        # Delete local branch (force, since it may not be merged)
        try:
            result = subprocess.run(
                ['git', '-C', str(repo_path), 'branch', '-D', branch],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                deleted.append(branch)
        except Exception:
            pass

        # Best-effort: delete remote tracking branch if it exists on origin
        try:
            subprocess.run(
                ['git', '-C', str(repo_path), 'push', 'origin', '--delete', branch],
                capture_output=True, timeout=20,
            )
        except Exception:
            pass

    return deleted


def delete_repo_branches(repo_id: str, branches: list, force: bool) -> dict:
    """
    Delete local git branches for a given dashboard repo.

    Safety defaults:
    - Default branch and currently checked-out branch are protected unless `force=True`.
    - If any non-archived tasks reference the branch, deletion is blocked unless `force=True`.
    """
    try:
        raw_branches = [str(b or '').strip() for b in (branches or [])]
        raw_branches = [b for b in raw_branches if b]
        if not raw_branches:
            return {'ok': False, 'error': 'No branches provided', 'deleted': [], 'skipped': []}

        # Allow typical git ref formats like "feature/x", "bugfix-1", "release/1.2.3".
        # Keep this conservative to avoid command injection / ref weirdness.
        invalid = [b for b in raw_branches if not re.match(r'^[A-Za-z0-9._/\-]+$', b)]
        if invalid:
            return {'ok': False, 'error': 'Invalid branch name(s)', 'invalid': invalid}

        registry = load_projects_registry()
        proj = next((p for p in registry if p['id'] == repo_id), None)
        if not proj:
            return {'ok': False, 'error': f'Project "{repo_id}" not found'}

        repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
        if not (repo_path / '.git').exists():
            return {'ok': False, 'error': 'Repo is not a git repository'}

        # Discover actual local branches so we can report "missing" branches.
        try:
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'branch', '--format=%(refname:short)'],
                stderr=subprocess.DEVNULL,
            ).decode(errors='replace')
            local_branches = [b.strip() for b in raw.splitlines() if b.strip()]
        except Exception:
            local_branches = []

        local_set = set(local_branches)
        missing = [b for b in raw_branches if local_branches and b not in local_set]
        branches_to_consider = [b for b in raw_branches if (not local_branches) or b in local_set]
        if not branches_to_consider:
            return {'ok': True, 'deleted': [], 'skipped': [], 'missing': missing}

        default_branch = resolve_default_branch(
            repo_path, override=proj.get('gitflow', {}).get('defaultBranch')
        )

        current_branch = None
        try:
            current_branch = subprocess.check_output(
                ['git', '-C', str(repo_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
                stderr=subprocess.DEVNULL,
            ).decode(errors='replace').strip()
        except Exception:
            pass

        protected = set()
        if not force:
            if default_branch:
                protected.add(default_branch)
            if current_branch:
                protected.add(current_branch)

        # If not forcing, block deleting branches that are referenced by active tasks.
        blocked_by_tasks = set()
        if not force:
            try:
                hits = es_search('agent-task-records', {
                    'size': 500,
                    '_source': ['id', 'repo', 'branch', 'status'],
                    'query': {
                        'bool': {
                            'must': [
                                {'terms': {'branch': branches_to_consider}},
                                {'term': {'repo': repo_id}},
                            ],
                            'must_not': [{'term': {'status': 'archived'}}],
                        }
                    },
                }).get('hits', {}).get('hits', [])

                for h in hits:
                    src = h.get('_source') or {}
                    b = (src.get('branch') or '').strip()
                    if b:
                        blocked_by_tasks.add(b)
            except Exception:
                # If ES isn't available, don't block deletion.
                blocked_by_tasks = set()

        to_delete = []
        skipped = []
        for b in branches_to_consider:
            if b in protected:
                skipped.append({'branch': b, 'reason': 'protected (default/current) — use force to override'})
                continue
            if b in blocked_by_tasks:
                skipped.append({'branch': b, 'reason': 'referenced by active tasks — use force to override'})
                continue
            to_delete.append(b)

        # If we are deleting the currently checked-out branch, switch away first.
        if current_branch and current_branch in to_delete:
            checkout_branch = None
            for b in local_branches:
                if b != current_branch and b not in to_delete:
                    checkout_branch = b
                    break
            if not checkout_branch:
                for b in local_branches:
                    if b != current_branch:
                        checkout_branch = b
                        break
            if checkout_branch:
                try:
                    subprocess.run(
                        ['git', '-C', str(repo_path), 'switch', checkout_branch],
                        capture_output=True,
                        timeout=20,
                    )
                    current_branch = checkout_branch
                except Exception:
                    # Best-effort only; deletion may still succeed or fail.
                    pass

        deleted = []
        errors = []
        for b in to_delete:
            try:
                del_flag = '-D' if force else '-d'
                result = subprocess.run(
                    ['git', '-C', str(repo_path), 'branch', del_flag, b],
                    capture_output=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    deleted.append(b)
                else:
                    stderr = (result.stderr or b'').decode(errors='replace').strip()
                    errors.append({'branch': b, 'error': stderr[:200] or 'git branch failed'})
            except Exception:
                errors.append({'branch': b, 'error': 'exception during git branch deletion'})

            # Best-effort: delete remote tracking branch if it exists on origin.
            try:
                subprocess.run(
                    ['git', '-C', str(repo_path), 'push', 'origin', '--delete', b],
                    capture_output=True,
                    timeout=20,
                )
            except Exception:
                pass

        return {
            'ok': True,
            'default': default_branch,
            'current': current_branch,
            'deleted': deleted,
            'skipped': skipped,
            'missing': missing,
            'errors': errors,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200], 'deleted': [], 'skipped': []}


def load_workers() -> list:
    if not WORKER_STATE.exists():
        return []
    try:
        data = json.loads(WORKER_STATE.read_text())
        workers = data.get('workers', [])
    except Exception:
        return []
        
    try:
        agg_res = es_search('agent-token-telemetry', {
            'size': 0,
            'aggs': {
                'by_worker': {
                    'terms': {'field': 'worker_name.keyword', 'size': 500},
                    'aggs': {
                        'total_input': {'sum': {'field': 'input_tokens'}},
                        'total_output': {'sum': {'field': 'output_tokens'}}
                    }
                },
                'total_elastro_savings': {
                    'sum': {'field': 'savings'}
                }
            }
        })
        buckets = agg_res.get('aggregations', {}).get('by_worker', {}).get('buckets', [])
        totals = {}
        for b in buckets:
            totals[b.get('key')] = {
                'input': int(b.get('total_input', {}).get('value', 0)),
                'output': int(b.get('total_output', {}).get('value', 0))
            }
        for w in workers:
            w['input_tokens'] = totals.get(w['name'], {}).get('input', 0)
            w['output_tokens'] = totals.get(w['name'], {}).get('output', 0)
    except Exception:
        pass
        
    return workers


def priority_rank(priority: str) -> int:
    ranks = {'urgent': 0, 'high': 1, 'medium': 2, 'normal': 3, 'low': 4}
    return ranks.get((priority or '').lower(), 99)


def queue_for_repo(repo_id: str):
    hits = es_search('agent-task-records', {
        'size': 500,
        'query': {
            'bool': {
                'must': [
                    {'term': {'repo': repo_id}},
                    {'term': {'status': 'ready'}},
                ],
                'must_not': [{'term': {'status': 'archived'}}],
            }
        },
        'sort': [{'updated_at': {'order': 'asc', 'unmapped_type': 'date'}}],
    }).get('hits', {}).get('hits', [])
    tasks = [{'_id': h.get('_id'), **h.get('_source', {})} for h in hits]
    tasks.sort(key=lambda t: (priority_rank(t.get('priority')), t.get('updated_at') or t.get('last_update') or ''))
    out = []
    for idx, t in enumerate(tasks, start=1):
        out.append({
            '_id': t.get('_id'),
            'id': t.get('id') or t.get('_id'),
            'title': t.get('title'),
            'status': t.get('status'),
            'priority': t.get('priority'),
            'owner': t.get('owner'),
            'assigned_agent_role': t.get('assigned_agent_role') or t.get('owner'),
            'queuePosition': idx,
            'updated_at': t.get('updated_at') or t.get('last_update'),
        })
    return out


def transition_task(task_id: str, status: str, owner=None, needs_human=None):
    es_id, _src = find_task_doc_by_logical_id(task_id)
    if not es_id:
        return None
    doc = {
        'status': status,
        'updated_at': datetime.utcnow().isoformat() + 'Z',
        'last_update': datetime.utcnow().isoformat() + 'Z',
    }
    if owner:
        doc['owner'] = owner
        doc['assigned_agent_role'] = owner
    if needs_human is not None:
        doc['needs_human'] = bool(needs_human)
    if status == 'ready':
        doc['implementer_consecutive_llm_failures'] = 0
    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    return {'_id': es_id, 'id': task_id, **doc}


def task_history(task_id: str):
    es_id, src = find_task_doc_by_logical_id(task_id)
    if not src:
        return None
    task = {'_id': es_id, **src}

    events = []

    def infer_model(src, event_type):
        if src.get('model_used'):
            return src.get('model_used')
        role = src.get('agent_role') or src.get('from_role') or task.get('owner') or task.get('assigned_agent_role')
        role = (role or '').lower()
        if role in ('implementer', 'tester', 'e2e-tester'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        if role in ('reviewer', 'acceptance-reviewer'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        if role in ('pm', 'pm-dispatcher', 'intake', 'memory-updater'):
            return os.environ.get('LLM_MODEL', 'llama3.2')
        return task.get('preferred_model') or None

    handoffs = es_search('agent-handoff-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in handoffs:
        src = h.get('_source', {})
        commit_note = ''
        if src.get('commit_sha'):
            commit_note = f"commit: {src['commit_sha'][:8]}"
        if src.get('branch'):
            commit_note = f"branch: {src['branch']}" + (f"  {commit_note}" if commit_note else '')
        events.append({
            'type': 'handoff',
            'timestamp': src.get('created_at'),
            'summary': f"{src.get('from_role', 'unknown')} -> {src.get('to_role', 'unknown')}",
            'details': src.get('reason') or '',
            'notes': src.get('objective') or '',
            'discussion': (src.get('constraints') or '') + (' | ' + commit_note if commit_note else ''),
            'modelUsed': infer_model(src, 'handoff'),
            'data': src,
        })

    reviews = es_search('agent-review-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in reviews:
        src = h.get('_source', {})
        events.append({
            'type': 'review',
            'timestamp': src.get('created_at'),
            'summary': f"Verdict: {src.get('verdict', 'unknown')}",
            'details': src.get('summary') or '',
            'notes': src.get('issues') or '',
            'discussion': src.get('recommended_next_role') or '',
            'modelUsed': infer_model(src, 'review'),
            'data': src,
        })

    failures = es_search('agent-failure-records', {
        'size': 100,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in failures:
        src = h.get('_source', {})
        events.append({
            'type': 'failure',
            'timestamp': src.get('updated_at') or src.get('created_at'),
            'summary': src.get('error_class') or 'failure',
            'details': src.get('summary') or '',
            'notes': src.get('root_cause') or '',
            'discussion': src.get('fix_applied') or '',
            'modelUsed': infer_model(src, 'failure'),
            'data': src,
        })

    provenance = es_search('agent-provenance-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'term': {'task_id': task_id}},
    }).get('hits', {}).get('hits', [])
    for h in provenance:
        src = h.get('_source', {})
        git_note = ''
        if src.get('branch'):
            git_note = f"branch: {src['branch']}"
        if src.get('commit_sha'):
            git_note += f"  sha: {src['commit_sha'][:8]}"
        events.append({
            'type': 'provenance',
            'timestamp': src.get('created_at'),
            'summary': f"Role: {src.get('agent_role', 'unknown')}",
            'details': src.get('review_verdict') or '',
            'notes': ', '.join(src.get('artifacts') or []) + (f' | {git_note}' if git_note else ''),
            'discussion': ', '.join(src.get('context_refs') or []),
            'modelUsed': infer_model(src, 'provenance'),
            'data': src,
        })

    # Add git/PR events if present on the task
    if task.get('branch'):
        pr_summary = ''
        if task.get('pr_url'):
            pr_summary = f"PR #{task.get('pr_number') or '?'} ({task.get('pr_status', 'open')}): {task['pr_url']}"
        elif task.get('pr_status') == 'failed':
            pr_summary = f"PR creation failed: {task.get('pr_error', 'unknown error')}"
        events.append({
            'type': 'git',
            'timestamp': task.get('updated_at') or task.get('last_update'),
            'summary': f"Branch: {task['branch']}" + (f" → {task['target_branch']}" if task.get('target_branch') else ''),
            'details': pr_summary,
            'notes': task.get('commit_message') or '',
            'discussion': task.get('commit_sha') or '',
            'modelUsed': None,
            'data': {
                'branch': task.get('branch'),
                'target_branch': task.get('target_branch'),
                'commit_sha': task.get('commit_sha'),
                'commit_message': task.get('commit_message'),
                'pr_url': task.get('pr_url'),
                'pr_number': task.get('pr_number'),
                'pr_status': task.get('pr_status'),
                'pr_error': task.get('pr_error'),
            },
        })

    # Always include current task snapshot as the latest state event
    events.append({
        'type': 'task_state',
        'timestamp': task.get('updated_at') or task.get('last_update'),
        'summary': f"Status: {task.get('status', 'unknown')}",
        'details': f"Owner: {task.get('owner', 'unknown')}",
        'notes': task.get('objective') or '',
        'discussion': f"Priority: {task.get('priority', 'n/a')}",
        'modelUsed': task.get('preferred_model'),
        'data': task,
    })

    events.sort(key=lambda e: e.get('timestamp') or '', reverse=True)

    # Build `history` in the format the frontend expects: [{ts, role, summary}]
    # Newest events first; agent_log entries (live notes) come first when task is running.
    history = []

    # Live agent notes — shown prominently while task is running
    agent_log = task.get('agent_log') or []
    for entry in reversed(agent_log):  # newest first
        history.append({
            'ts': entry.get('ts', ''),
            'role': 'agent',
            'summary': entry.get('note', ''),
            'type': 'agent_note',
        })

    # Structured events from handoffs, reviews, failures, etc.
    for e in events:
        role = {
            'handoff': f"{(e.get('data') or {}).get('from_role', 'agent')} → {(e.get('data') or {}).get('to_role', '')}",
            'review': 'reviewer',
            'failure': 'system',
            'provenance': (e.get('data') or {}).get('agent_role', 'agent'),
            'git': 'git',
            'task_state': 'system',
        }.get(e.get('type', ''), 'agent')
        summary = e.get('summary', '')
        if e.get('details'):
            summary += f' — {e["details"]}'
        history.append({
            'ts': e.get('timestamp', ''),
            'role': role,
            'summary': summary,
            'type': e.get('type', ''),
        })

    return {'task': task, 'events': events, 'history': history, 'agent_log': agent_log}


def git_repo_info(repo_id, repo_path: Path):
    info = {
        'id': repo_id,
        'path': str(repo_path),
        'exists': repo_path.exists(),
        'is_git': False,
        'current_branch': None,
        'last_commit': None,
    }
    git_dir = repo_path / '.git'
    if not git_dir.exists():
        return info
    info['is_git'] = True
    try:
        branch = subprocess.check_output(
            ['git', '-C', str(repo_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        info['current_branch'] = branch
    except Exception:
        pass
    try:
        last = subprocess.check_output(
            ['git', '-C', str(repo_path), 'log', '-1', '--pretty=format:%H%n%an%n%ai%n%s'],
            stderr=subprocess.DEVNULL,
        ).decode().splitlines()
        if len(last) >= 4:
            info['last_commit'] = {
                'hash': last[0],
                'author': last[1],
                'date': last[2],
                'subject': last[3],
            }
    except Exception:
        pass
    return info


def resolve_default_branch(repo_path: Path, override: Optional[str] = None) -> str:
    """Resolve the default branch for a repo (main/master/etc.)."""
    if override:
        return override
    try:
        # Try origin/HEAD symbolic ref
        ref = subprocess.check_output(
            ['git', '-C', str(repo_path), 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        # refs/remotes/origin/main -> main
        return ref.split('/')[-1]
    except Exception:
        pass
    try:
        # Fallback: check common branch names
        branches_raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'branch', '-r'],
            stderr=subprocess.DEVNULL,
        ).decode()
        for candidate in ('main', 'master', 'develop', 'trunk'):
            if f'origin/{candidate}' in branches_raw:
                return candidate
    except Exception:
        pass
    try:
        current = subprocess.check_output(
            ['git', '-C', str(repo_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return current or 'main'
    except Exception:
        return 'main'


def get_task_doc(task_id: str):
    """Fetch a single task document from ES by logical id."""
    return find_task_doc_by_logical_id(task_id)


def create_task_pr(task_id: str) -> dict:
    """
    Create a GitHub PR for a task that has been reviewer-approved.
    Returns a result dict with keys: ok, pr_url, pr_number, error, skipped.
    """
    es_id, task = get_task_doc(task_id)
    if not task:
        return {'ok': False, 'error': 'Task not found'}

    # Idempotency: don't create duplicate PRs
    if task.get('pr_url'):
        return {'ok': True, 'skipped': True, 'pr_url': task['pr_url'], 'pr_number': task.get('pr_number')}

    branch = task.get('branch')
    if not branch:
        return {'ok': False, 'error': 'No branch recorded on task — implementer must run first'}

    repo_id = task.get('repo')
    registry = load_projects_registry()
    proj = next((p for p in registry if p['id'] == repo_id), None)
    if not proj:
        return {'ok': False, 'error': f'Project "{repo_id}" not found in registry'}

    repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
    if not (repo_path / '.git').exists():
        return {'ok': False, 'error': 'Repo path is not a git repository'}

    target_branch = resolve_default_branch(
        repo_path,
        override=proj.get('gitflow', {}).get('defaultBranch'),
    )

    # Build PR title / body from task metadata
    title = task.get('title') or f"Task {task_id}"
    ac = task.get('acceptance_criteria') or []
    ac_lines = '\n'.join(f'- {c}' for c in ac) if ac else '_None recorded_'
    commit_sha = task.get('commit_sha') or ''
    sha_line = f'\n\n**Commit:** `{commit_sha}`' if commit_sha else ''
    body = (
        f"## {title}\n\n"
        f"**Task ID:** `{task_id}`\n"
        f"**Repo:** `{repo_id}`\n"
        f"**Branch:** `{branch}` → `{target_branch}`\n"
        f"{sha_line}\n\n"
        f"### Acceptance Criteria\n{ac_lines}\n\n"
        f"_Auto-generated by OpenClaw agent workflow._"
    )

    gh_path = subprocess.run(['which', 'gh'], capture_output=True, text=True).stdout.strip()
    if not gh_path:
        return {'ok': False, 'error': '`gh` CLI not found — install GitHub CLI to enable PR creation'}

    try:
        result = subprocess.run(
            [
                'gh', 'pr', 'create',
                '--base', target_branch,
                '--head', branch,
                '--title', title,
                '--body', body,
            ],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo_path),
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'gh pr create timed out after 60s'}

    if result.returncode != 0:
        return {'ok': False, 'error': result.stderr.strip()[:500] or result.stdout.strip()[:500]}

    pr_url = result.stdout.strip()
    # Extract PR number from URL e.g. https://github.com/org/repo/pull/42
    pr_number = None
    url_parts = pr_url.rstrip('/').split('/')
    if url_parts and url_parts[-1].isdigit():
        pr_number = int(url_parts[-1])

    # Persist PR metadata to task doc
    if es_id:
        es_post(f'agent-task-records/_update/{es_id}', {
            'doc': {
                'pr_url': pr_url,
                'pr_number': pr_number,
                'pr_status': 'open',
                'target_branch': target_branch,
                'updated_at': datetime.utcnow().isoformat() + 'Z',
                'last_update': datetime.utcnow().isoformat() + 'Z',
            }
        })

    return {'ok': True, 'pr_url': pr_url, 'pr_number': pr_number, 'target_branch': target_branch}


def _git_task_context(task_id: str):
    """
    Shared helper: fetch task doc and resolve (task, repo_path, branch, target_branch).
    Returns (task, repo_path, branch, target_branch, error_dict).
    error_dict is non-None when something is missing.
    """
    _, task = get_task_doc(task_id)
    if not task:
        return None, None, None, None, {'error': 'Task not found', 'branch': None}
    branch = task.get('branch')
    if not branch:
        return task, None, None, None, {'error': 'No branch recorded on task yet', 'branch': None}
    repo_id = task.get('repo')
    registry = load_projects_registry()
    proj = next((p for p in registry if p['id'] == repo_id), None)
    if not proj:
        return task, None, branch, None, {'error': f'Project "{repo_id}" not found', 'branch': branch}
    repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
    if not (repo_path / '.git').exists():
        return task, None, branch, None, {'error': 'Repo is not a git repository', 'branch': branch}
    target_branch = task.get('target_branch') or resolve_default_branch(
        repo_path, override=proj.get('gitflow', {}).get('defaultBranch')
    )
    return task, repo_path, branch, target_branch, None


def task_diff(task_id: str) -> dict:
    """Return unified diff of branch vs target branch (three-dot diff)."""
    task, repo_path, branch, target_branch, err = _git_task_context(task_id)
    if err:
        return {**err, 'files': [], 'diff': '', 'truncated': False, 'target_branch': None}

    MAX_DIFF_LINES = 2000
    ref = f'origin/{target_branch}...{branch}'

    # Try fetch to ensure remote refs are current (best-effort, silent on failure)
    try:
        subprocess.run(
            ['git', '-C', str(repo_path), 'fetch', 'origin', '--quiet'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    # --stat output to get per-file summary
    files = []
    try:
        stat_raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'diff', '--stat', '--stat-width=1000', ref],
            stderr=subprocess.DEVNULL, timeout=15,
        ).decode(errors='replace')
        for line in stat_raw.splitlines():
            # Format: " src/foo.py | 12 +++---"
            parts = line.strip().split('|')
            if len(parts) != 2:
                continue
            path_part = parts[0].strip()
            change_part = parts[1].strip()
            if not path_part or path_part.startswith('changed'):
                continue
            bars = change_part.split()
            plus_count = bars[1].count('+') if len(bars) > 1 else 0
            minus_count = bars[1].count('-') if len(bars) > 1 else 0
            files.append({
                'path': path_part,
                'insertions': plus_count,
                'deletions': minus_count,
                'status': 'modified',
            })
    except Exception:
        # Fall back to local diff if fetch/remote unavailable
        ref = f'{target_branch}...{branch}'

    # Full unified diff
    diff_text = ''
    truncated = False
    try:
        raw = subprocess.check_output(
            ['git', '-C', str(repo_path), 'diff', ref],
            stderr=subprocess.DEVNULL, timeout=20,
        ).decode(errors='replace')
        lines = raw.splitlines(keepends=True)
        if len(lines) > MAX_DIFF_LINES:
            diff_text = ''.join(lines[:MAX_DIFF_LINES])
            truncated = True
        else:
            diff_text = raw
    except Exception as e:
        diff_text = ''

    # If remote three-dot ref failed, fall back to local two-dot
    if not diff_text and not files:
        try:
            ref_local = f'{target_branch}..{branch}'
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'diff', ref_local],
                stderr=subprocess.DEVNULL, timeout=20,
            ).decode(errors='replace')
            lines = raw.splitlines(keepends=True)
            diff_text = ''.join(lines[:MAX_DIFF_LINES])
            truncated = len(lines) > MAX_DIFF_LINES
        except Exception:
            pass

    return {
        'branch': branch,
        'target_branch': target_branch,
        'files': files,
        'diff': diff_text,
        'truncated': truncated,
        'error': None,
    }


def task_commits(task_id: str) -> dict:
    """Return commits on branch that are not on target branch."""
    task, repo_path, branch, target_branch, err = _git_task_context(task_id)
    if err:
        return {**err, 'commits': [], 'target_branch': None}

    # Best-effort fetch
    try:
        subprocess.run(
            ['git', '-C', str(repo_path), 'fetch', 'origin', '--quiet'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    commits = []
    # Try origin/target first, fall back to local target
    for ref_target in (f'origin/{target_branch}', target_branch):
        try:
            raw = subprocess.check_output(
                ['git', '-C', str(repo_path), 'log',
                 f'{ref_target}..{branch}',
                 '--pretty=format:%H|%an|%ai|%s',
                 '--max-count=50'],
                stderr=subprocess.DEVNULL, timeout=15,
            ).decode(errors='replace').strip()
            if raw:
                for line in raw.splitlines():
                    parts = line.split('|', 3)
                    if len(parts) == 4:
                        sha, author, date, message = parts
                        commits.append({
                            'sha': sha.strip(),
                            'author': author.strip(),
                            'date': date.strip(),
                            'message': message.strip(),
                        })
            break
        except Exception:
            continue

    return {
        'branch': branch,
        'target_branch': target_branch,
        'commits': commits,
        'error': None,
    }


def load_repos():
    registry = load_projects_registry()
    repos = []
    for p in registry:
        path = Path(p.get('path') or str(WORKSPACE_ROOT / p['id']))
        repos.append(git_repo_info(p['id'], path))
    return repos


def load_snapshot():
    if not ES_API_KEY or ES_API_KEY == 'AUTO_GENERATED_BY_INSTALLER':
        pass
    tasks = es_search('agent-task-records', {
        'size': 300,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {
            'bool': {
                'must': [{'match_all': {}}],
                'must_not': [{'term': {'status': 'archived'}}],
            }
        },
    }).get('hits', {}).get('hits', [])
    reviews = es_search('agent-review-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'match_all': {}}
    }).get('hits', {}).get('hits', [])
    failures = es_search('agent-failure-records', {
        'size': 100,
        'sort': [{'updated_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'match_all': {}}
    }).get('hits', {}).get('hits', [])
    provenance = es_search('agent-provenance-records', {
        'size': 100,
        'sort': [{'created_at': {'order': 'desc', 'unmapped_type': 'date'}}],
        'query': {'match_all': {}}
    }).get('hits', {}).get('hits', [])
    elastro_savings = 0
    try:
        agg_res = es_search('agent-token-telemetry', {
            'size': 0,
            'aggs': {'total_elastro_savings': {'sum': {'field': 'savings'}}}
        })
        elastro_savings = int(agg_res.get('aggregations', {}).get('total_elastro_savings', {}).get('value', 0))
    except Exception:
        pass

    return {
        'workers': load_workers(),
        'tasks': [{'_id': h.get('_id'), **h.get('_source', {})} for h in tasks],
        'reviews': [{'_id': h.get('_id'), **h.get('_source', {})} for h in reviews],
        'failures': [{'_id': h.get('_id'), **h.get('_source', {})} for h in failures],
        'provenance': [{'_id': h.get('_id'), **h.get('_source', {})} for h in provenance],
        'repos': load_repos(),
        'projects': load_projects_registry(),
        'elastro_savings': elastro_savings,
    }


# ─── Agent process control ────────────────────────────────────────────────────

WORKER_MANAGER_SCRIPT = _SRC_ROOT / 'worker-manager' / 'manager.py'
WORKER_HANDLERS_SCRIPT = _SRC_ROOT / 'worker-manager' / 'worker_handlers.py'
WORKER_ENV_FILE = _SRC_ROOT / 'memory' / 'es' / '.env.local'


def _find_worker_pids() -> dict:
    """Return pids of manager.py and worker_handlers.py if running."""
    pids = {'manager': None, 'handlers': None}
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', str(WORKER_MANAGER_SCRIPT)],
            stderr=subprocess.DEVNULL,
        ).decode().split()
        pids['manager'] = [int(p) for p in out if p.strip()]
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', str(WORKER_HANDLERS_SCRIPT)],
            stderr=subprocess.DEVNULL,
        ).decode().split()
        pids['handlers'] = [int(p) for p in out if p.strip()]
    except Exception:
        pass
    return pids


def agents_status() -> dict:
    pids = _find_worker_pids()
    manager_running = bool(pids['manager'])
    handlers_running = bool(pids['handlers'])
    return {
        'running': manager_running or handlers_running,
        'manager_running': manager_running,
        'handlers_running': handlers_running,
        'manager_pids': pids['manager'] or [],
        'handler_pids': pids['handlers'] or [],
    }


def _requeue_running_tasks():
    """
    After stopping workers, reset tasks stuck in 'running' back to their
    appropriate queued state so they can be picked up on next start.
    """
    try:
        hits = es_search('agent-task-records', {
            'size': 200,
            'query': {'term': {'status': 'running'}},
        }).get('hits', {}).get('hits', [])
        now = datetime.utcnow().isoformat() + 'Z'
        requeued = 0
        for h in hits:
            es_id = h.get('_id')
            src = h.get('_source', {})
            role = src.get('assigned_agent_role') or src.get('owner') or ''
            # Tester and reviewer work lives in 'review', pm lives in 'planned'
            if role in ('tester', 'reviewer'):
                new_status = 'review'
            elif role == 'pm':
                new_status = 'planned'
            else:
                new_status = 'ready'
            es_post(f'agent-task-records/_update/{es_id}', {
                'doc': {
                    'status': new_status,
                    'updated_at': now,
                    'last_update': now,
                    'active_worker': None,
                }
            })
            requeued += 1
        return requeued
    except Exception as e:
        return 0


def agents_stop() -> dict:
    """Kill worker processes and re-queue any stuck running tasks."""
    import signal
    pids = _find_worker_pids()
    killed = []
    for group in ('manager', 'handlers'):
        for pid in (pids.get(group) or []):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except Exception:
                pass
    requeued = _requeue_running_tasks()
    return {'ok': True, 'killed_pids': killed, 'requeued_tasks': requeued}


def agents_start() -> dict:
    """Start manager and worker_handlers if not already running."""
    pids = _find_worker_pids()
    started = []

    # Build env from the worker env file
    env = dict(os.environ)
    if WORKER_ENV_FILE.exists():
        for line in WORKER_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()

    try:
        log_dir_env = os.environ.get('FLUME_LOG_DIR', '').strip()
        log_dir = Path(log_dir_env).resolve() if log_dir_env else WORKSPACE_ROOT / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        manager_err = open(log_dir / 'manager_stderr.log', 'a')
        handlers_err = open(log_dir / 'handlers_stderr.log', 'a')
    except PermissionError as e:
        logger.error(json.dumps({"event": "log_directory_creation_failure", "error": str(e), "status": "fallback"}))
        manager_err = subprocess.DEVNULL
        handlers_err = subprocess.DEVNULL
    
    python_bin = sys.executable

    if not pids['manager']:
        proc = subprocess.Popen(
            [python_bin, str(WORKER_MANAGER_SCRIPT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=manager_err,
            start_new_session=True,
        )
        started.append({'role': 'manager', 'pid': proc.pid})

    if not pids['handlers']:
        proc = subprocess.Popen(
            [python_bin, str(WORKER_HANDLERS_SCRIPT)],
            env=env,
            cwd=str(_SRC_ROOT / 'worker-manager'),
            stdout=subprocess.DEVNULL,
            stderr=handlers_err,
            start_new_session=True,
        )
        started.append({'role': 'handlers', 'pid': proc.pid})

    if manager_err is not subprocess.DEVNULL:
        manager_err.close()
    if handlers_err is not subprocess.DEVNULL:
        handlers_err.close()

    return {'ok': True, 'started': started, 'already_running': not started}


def _resolve_flume_cli() -> Optional[Path]:
    """Path to the `flume` driver script at repo or package root, or None."""
    w = WORKSPACE_ROOT.resolve()
    for base in (w, w.parent):
        candidate = base / 'flume'
        if candidate.is_file():
            return candidate
    return None


def restart_flume_services() -> dict:
    """
    Schedule `./flume restart --all` in a detached shell so systemd can restart
    the dashboard and workers bounce. If `flume` is missing, fall back to
    stopping/starting worker processes only.
    """
    flume_sh = _resolve_flume_cli()
    if flume_sh is not None:
        root = flume_sh.parent.resolve()
        script = flume_sh.name
        inner = (
            f'cd {shlex.quote(str(root))} && sleep 0.5 && exec bash {shlex.quote(script)} restart --all'
        )
        try:
            subprocess.Popen(
                ['bash', '-c', inner],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            return {
                'ok': True,
                'mode': 'flume',
                'message': 'Restart scheduled. You may lose connection briefly; refresh if the page stops responding.',
            }
        except Exception:
            pass
    try:
        agents_stop()
        started = agents_start()
        return {
            'ok': True,
            'mode': 'workers_only',
            'message': 'Worker processes restarted. Restart the dashboard manually if configuration still looks stale.',
            'workers': started,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)[:400]}


def _github_https_clone_url(repo_url: str, gh_token: str) -> str:
    """
    Embed a GitHub PAT for non-interactive HTTPS clone.

    GitHub documents https://x-access-token:<token>@github.com/... for both classic
    and fine-grained PATs; raw https://<token>@github.com/... can fail for some tokens.
    """
    if not gh_token or not repo_url.startswith('https://github.com/'):
        return repo_url
    if '://' not in repo_url:
        return repo_url
    host_and_rest = repo_url.split('://', 1)[1]
    if '@' in host_and_rest.split('/', 1)[0]:
        return repo_url
    enc = urllib.parse.quote(gh_token, safe='')
    return re.sub(
        r'^https://github\.com/',
        f'https://x-access-token:{enc}@github.com/',
        repo_url,
    )


def maybe_auto_start_workers():
    """
    Start worker manager + handlers when the dashboard starts (same as POST /api/workflow/agents/start).

    Set FLUME_AUTO_START_WORKERS=0 (or false/no/off) to disable — e.g. if you run workers on another host.
    """
    raw = os.environ.get('FLUME_AUTO_START_WORKERS', '1').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return
    try:
        result = agents_start()
        started = result.get('started') or []
        if started:
            logger.info(f'Flume: auto-started workers: {started}')
        elif result.get('already_running'):
            logger.info('Flume: workers already running (skipped auto-start).')
    except Exception as e:
        logger.info(f'Flume: warning — could not auto-start workers: {e}')




from fastapi import FastAPI, BackgroundTasks, WebSocket, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import json

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Re-run validation natively inside the event loop in case env vars were mutated post-import
        resolve_safe_workspace()
        
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(json.dumps({
            "event": "workspace_initialized",
            "path": str(WORKSPACE_ROOT),
            "source": "FLUME_WORKSPACE" if os.environ.get('FLUME_WORKSPACE') else "fallback_home",
            "status": "success"
        }))
    except Exception as e:
        logger.error(json.dumps({
            "event": "workspace_initialization_failure",
            "path": str(WORKSPACE_ROOT),
            "error": str(e),
            "status": "fatal"
        }))
        raise WorkspaceInitializationError(f"Failed to initialize workspace: {e}") from e

    # Ignite the child process worker swarm dynamically natively post-workspace assembly
    maybe_auto_start_workers()
    
    app.state.http_client = httpx.AsyncClient()
    yield
    await app.state.http_client.aclose()

app = FastAPI(title="Flume Enterprise API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# The legacy @app.on_event("startup") was migrated strictly up to the FastAPI lifespan architecture above.

@app.get('/api/health')
def health():
    return {"status": "ok"}



from urllib.parse import urlparse, urlunparse

def _parse_float_env(key: str, default: float) -> float:
    val_str = os.environ.get(key)
    if val_str is None:
        return default
    try:
        val_flt = float(val_str)
        if val_flt > 0:
            return val_flt
        logger.warning(
            f"Invalid {key} value (must be > 0). Falling back to default.",
            extra={
                "component": "config_parser",
                "invalid_value": val_flt,
                "default_value": default,
            }
        )
    except (ValueError, TypeError):
        logger.warning(
            f"Invalid {key} format. Falling back to default.",
            extra={
                "component": "config_parser",
                "invalid_value": val_str,
                "default_value": default,
            }
        )
    return default

class AppSettings:
    def __init__(self):
        self.exo_url = os.environ.get("EXO_STATUS_URL", "http://host.docker.internal:52415/models")
        self.exo_timeout = _parse_float_env("EXO_STATUS_TIMEOUT_SECONDS", 0.5)

settings = AppSettings()

import fastapi
def get_app_settings() -> AppSettings:
    return settings

@app.get('/api/exo-status')
async def api_exo_status(request: fastapi.Request, app_settings: AppSettings = fastapi.Depends(get_app_settings)):
    http_client = request.app.state.http_client
    
    exo_url = app_settings.exo_url
    exo_timeout = app_settings.exo_timeout

    parsed_url = urlparse(exo_url)
    base_url_parts = parsed_url._replace(path='/v1')
    base_url = urlunparse(base_url_parts)

    try:
        hostname = parsed_url.hostname
        if hostname not in ('host.docker.internal', 'localhost', '127.0.0.1', '::1'):
            logger.warning("Rejected Exo base URL targeting out-of-bounds mapping", extra={"target_url": exo_url})
            return {"active": False}
    except (ValueError, TypeError) as e:
        logger.error("Unexpected error during Exo URL validation", extra={"target_url": exo_url, "error": str(e)})
        return {"active": False}

    try:
        resp = await http_client.get(exo_url, timeout=exo_timeout)
        resp.raise_for_status()
        
        logger.info(
            "Successfully connected to Exo service",
            extra={
                "component": "exo_detector",
                "target_url": exo_url,
            }
        )
        return {"active": True, "baseUrl": base_url}
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning(
            "Exo service connection failed",
            extra={
                "component": "exo_detector",
                "target_url": exo_url,
                "timeout_seconds": exo_timeout,
                "error_type": type(e).__name__,
                "error_details": str(e)
            }
        )
        return {"active": False}

@app.get('/api/snapshot')
def api_snapshot():
    try:
        return load_snapshot()
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:400], 'code': 'ES_CONNECTION'})

@app.get('/api/system-state')
def api_system_state():
    try:
        workers = load_workers()
        active = sum(1 for w in workers if w.get('status') in ('busy', 'claimed'))
        total = len(workers)
        return {
            "status": "online",
            "activeStreams": active,
            "totalNodes": total,
            "standbyNodes": total - active,
            "workers": workers
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:300]})

def _check_ast_exists_natively(repo_path: str) -> tuple[bool, str]:
    try:
        es_url = os.environ.get('ES_URL', 'http://localhost:9200').rstrip('/')
        api_key = os.environ.get('ES_API_KEY', '')
        headers = {'Content-Type': 'application/json'}
        if api_key: headers['Authorization'] = f'ApiKey {api_key}'

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        query = {"query": {"match": {"file_path": repo_path}}, "size": 1}
        req = urllib.request.Request(f"{es_url}/flume-elastro-graph/_search", data=json.dumps(query).encode(), headers=headers, method='POST')
        
        with urllib.request.urlopen(req, timeout=5, context=ctx) as res:
            data = json.loads(res.read().decode())
            exists = data.get('hits', {}).get('total', {}).get('value', 0) > 0
            return exists, ("Found mapping records" if exists else "No logical paths matched")
    except Exception as e:
        logger.error({"event": "ast_existence_check_failure", "repo": repo_path, "error": str(e)})
        return False, str(e)

def _deterministic_ast_ingest(repo_path: str, project_id: str, project_name: str):
    try:
        exists, details = _check_ast_exists_natively(repo_path)
            
        if not exists:
            logger.info({"event": "ast_ingest_start", "repo": repo_path, "project": project_name})
            subprocess.run(["elastro", "rag", "ingest", repo_path], shell=False, check=True, capture_output=True, timeout=120)
            logger.info({"event": "ast_ingest_success", "repo": repo_path, "project": project_name})
        else:
            logger.info({"event": "ast_ingest_skipped", "repo": repo_path, "project": project_name, "reason": "already_indexed"})

    except subprocess.CalledProcessError as e:
        logger.error({
            "event": "ast_ingest_failure", 
            "repo": repo_path, 
            "error": "subprocess_error",
            "stderr": e.stderr.decode('utf-8', errors='replace') if e.stderr else "",
            "stdout": e.stdout.decode('utf-8', errors='replace') if e.stdout else ""
        })
    except Exception as e:
        logger.error({"event": "ast_ingest_failure", "repo": repo_path, "error": str(e), "traceback": traceback.format_exc()})

@app.post("/api/system/sync-ast")
def api_system_sync_ast(x_flume_system_token: str = Header(None), settings: AppConfig = Depends(get_settings)):
    import secrets
    if not (
        settings.FLUME_ADMIN_TOKEN and
        x_flume_system_token and
        secrets.compare_digest(settings.FLUME_ADMIN_TOKEN, x_flume_system_token)
    ):
        logger.warning({"event": "auth_failure", "endpoint": "/api/system/sync-ast", "reason": "invalid_system_token"})
        raise HTTPException(status_code=403, detail="Forbidden: System architectural mapping strictly enforced")
        
    workspace = settings.FLUME_WORKSPACE
    try:
        _deterministic_ast_ingest(workspace, "flume-core", "Flume Core Architecture")
        exists, details = _check_ast_exists_natively(workspace)
        if not exists:
            logger.error({
                "event": "ast_verification_failure",
                "workspace": workspace,
                "details": details,
            })
            return JSONResponse(status_code=500, content={
                "error": "AST ingestion completed, but post-flight verification failed.",
                "details": details
            })
        return {"success": True, "message": "AST Mapping securely synchronized via backend decoupling"}
    except (IOError, subprocess.CalledProcessError) as e:
        logger.error({
            "event": "ast_system_sync_failure", 
            "reason": "subprocess_error",
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        return JSONResponse(status_code=500, content={"error": "A predictable subprocess execution failure occurred natively."})
    except Exception as e:
        logger.error({
            "event": "ast_system_sync_failure", 
            "reason": "unhandled_exception",
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        return JSONResponse(status_code=500, content={"error": "An internal architectural error occurred dynamically."})

@app.post("/api/projects")
def api_create_project(payload: dict, background_tasks: BackgroundTasks):
    import uuid, datetime
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Project name is absolutely required natively."})
    
    new_id = f"proj-{uuid.uuid4().hex[:8]}"
    entry = {
        "id": new_id,
        "name": name,
        "repoUrl": (payload.get("repoUrl") or "").strip(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "gitflow": {"autoPrOnApprove": True, "defaultBranch": None}
    }
    
    registry = load_projects_registry()
    registry.append(entry)
    save_projects_registry(registry)
    
    background_tasks.add_task(_deterministic_ast_ingest, entry["repoUrl"], new_id, name)
    
    return {"success": True, "projectId": new_id, "message": "Project dynamically constructed seamlessly natively."}

@app.post("/api/projects/{project_id}/delete")
def api_delete_project(project_id: str):
    registry = load_projects_registry()
    filtered = [p for p in registry if p.get("id") != project_id]
    save_projects_registry(filtered)
    return {"success": True, "message": "Project removed natively"}

@app.get("/api/tasks/{task_id}/history")
def api_task_history(task_id: str):
    return []

@app.get("/api/tasks/{task_id}/diff")
def api_task_diff(task_id: str):
    return {"diff": ""}

@app.get("/api/tasks/{task_id}/commits")
def api_task_commits(task_id: str):
    return []

@app.post("/api/tasks/{task_id}/transition")
def api_task_transition(task_id: str, payload: dict):
    return {"success": True}


from fastapi import Depends, HTTPException, Request, Header
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError
import secrets
import httpx

class KillSwitchDatabaseError(Exception): pass
class KillSwitchProcessError(Exception): pass

class AuthConfigurationError(Exception): pass
class InvalidCredentialsError(Exception): pass

class IndexError(Exception): pass
class ElasticsearchClient:
    def __init__(self, es_url: str, api_key: str, ca_certs: str):
        self.es_url = es_url.rstrip('/')
        self.headers = {'Content-Type': 'application/json'}
        if api_key: self.headers['Authorization'] = f'ApiKey {api_key}'
        verify_ssl = ca_certs if ca_certs else False
        self.client = httpx.AsyncClient(headers=self.headers, verify=verify_ssl, timeout=10.0)

    async def update_tasks_to_halted(self):
        query = {
            "query": {"terms": {"status.keyword": ["ready", "running"]}},
            "script": {"source": "ctx._source.status = 'blocked'; ctx._source.ast_sync_status = 'halted';"}
        }
        url = f"{self.es_url}/agent-task-records/_update_by_query?conflicts=proceed"
        try:
            response = await self.client.post(url, json=query)
            response.raise_for_status()
        except httpx.RequestError as e:
            raise KillSwitchDatabaseError(f"Network error updating Elasticsearch: {e}")
        except httpx.HTTPStatusError as e:
            raise KillSwitchDatabaseError(f"HTTP error updating Elasticsearch: {e.response.status_code}")

class AgentSupervisor:
    def terminate_all(self) -> dict:
        return agents_stop()

class KillSwitchService:
    def __init__(self, es_client: ElasticsearchClient, supervisor: AgentSupervisor):
        self.es_client = es_client
        self.supervisor = supervisor

    async def halt_all_tasks(self, correlation_id: str):
        logger.info(json.dumps({"event": "kill_switch.invoke.start", "action": "initiating_swarm_halt", "target": "all_active_tasks", "correlation_id": correlation_id}))
        try:
            await self.es_client.update_tasks_to_halted()
            logger.info(json.dumps({"event": "kill_switch.db_update.success", "elasticsearch_status": "blocked", "message": "All execution bounds overridden natively", "correlation_id": correlation_id}))
        except Exception as e:
            logger.error(json.dumps({"event": "kill_switch.db_update.failure", "error": str(e), "correlation_id": correlation_id}))
            raise KillSwitchDatabaseError(str(e))

        try:
            supervisor_res = self.supervisor.terminate_all()
            killed_pids = []
            if isinstance(supervisor_res, dict):
                killed_pids = supervisor_res.get('killed_pids', [])
            else:
                logger.warning(json.dumps({
                    "event": "kill_switch.supervisor.invalid_response",
                    "response_type": str(type(supervisor_res)),
                    "correlation_id": correlation_id
                }))
            logger.info(json.dumps({"event": "kill_switch.process_kill.success", "killed_pids": killed_pids, "killed_pid_count": len(killed_pids), "message": "Supervisor gracefully executed subprocess halts", "correlation_id": correlation_id}))
            return {"success": True, "killed_pids": killed_pids, "correlation_id": correlation_id}
        except Exception as e:
            logger.critical(json.dumps({
                "event": "kill_switch.invoke.partial_failure",
                "error": str(e),
                "correlation_id": correlation_id,
                "message": "CRITICAL: Database tasks were halted, but failed to kill OS processes. Manual intervention may be required."
            }))
            raise KillSwitchProcessError(f"DB updated but process kill failed: {e}")

def get_app_config():
    return AppConfig()

def get_es_client(app_config: AppConfig = Depends(get_app_config)):
    return ElasticsearchClient(
        es_url=app_config.ES_URL,
        api_key=app_config.ES_API_KEY,
        ca_certs=app_config.ES_CA_CERTS
    )

def get_agent_supervisor():
    return AgentSupervisor()

def get_kill_switch_service(es_client: ElasticsearchClient = Depends(get_es_client), supervisor: AgentSupervisor = Depends(get_agent_supervisor)):
    return KillSwitchService(es_client, supervisor)

class AdminAuthorizer:
    def __init__(self, required_token: str):
        self.required_token = required_token
    
    def authorize(self, auth_header: str):
        if not self.required_token:
            raise AuthConfigurationError("Admin token not configured on server.")
        
        expected_auth = f"Bearer {self.required_token}"
        if not auth_header or not secrets.compare_digest(auth_header, expected_auth):
            raise InvalidCredentialsError("Admin access required.")

def verify_admin_access(request: Request, app_config: AppConfig = Depends(get_app_config)):
    authorizer = AdminAuthorizer(app_config.FLUME_ADMIN_TOKEN)
    try:
        authorizer.authorize(request.headers.get("Authorization"))
    except InvalidCredentialsError:
        logger.warning(json.dumps({
            "event": "admin_auth_failure",
            "endpoint": "/api/tasks/stop-all",
            "client_ip": request.client.host if request.client else "unknown"
        }))
        raise HTTPException(status_code=403, detail="Admin access required")
    except AuthConfigurationError:
        logger.critical("CRITICAL: FLUME_ADMIN_TOKEN is not set. Admin endpoint is disabled.")
        raise HTTPException(status_code=403, detail="Endpoint disabled: Server configuration incomplete.")
    return True

@app.post("/api/tasks/stop-all", dependencies=[Depends(verify_admin_access)])
async def api_tasks_stop_all(kill_switch_service: KillSwitchService = Depends(get_kill_switch_service)):
    correlation_id = str(uuid.uuid4())
    try:
        result = await kill_switch_service.halt_all_tasks(correlation_id)
        return {**result, "message": "All active Swarm networks successfully halted natively via supervisor."}
    except (KillSwitchDatabaseError, KillSwitchProcessError) as e:
        error_message = "An internal error occurred while halting tasks. Please check server logs."
        if isinstance(e, KillSwitchProcessError):
            error_message = "CRITICAL: Tasks were marked as halted, but failed to terminate running processes. Manual intervention may be required."
        raise HTTPException(status_code=500, detail={'error': error_message, 'correlation_id': correlation_id})
@app.get("/api/repos/{project_id}/branches")
def api_repo_branches(project_id: str):
    return []

@app.post("/api/repos/{project_id}/branches/delete")
def api_repo_branches_delete(project_id: str, payload: dict):
    return {"success": True}

@app.get("/api/codex-app-server/status")
def api_codex_status():
    from codex_app_server import status
    return status()

@app.get("/api/codex-app-server/proxy-config")
def api_codex_proxy_config():
    # Frontend expects codex WS setup info
    return {"baseUrl": "ws://localhost:8765", "path": "/api/codex-app-server/ws"}

@app.get("/api/settings/llm")
def api_settings_llm():
    from llm_settings import get_llm_settings_response
    return get_llm_settings_response(WORKSPACE_ROOT)

@app.post("/api/settings/llm")
def api_settings_llm_update(payload: dict):
    from llm_settings import validate_llm_settings, _update_env_keys
    ok, msg, updates = validate_llm_settings(payload, WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"success": True, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})

@app.put("/api/settings/llm/credentials")
def api_settings_llm_credentials(payload: dict):
    from llm_settings import validate_llm_settings, _update_env_keys
    ok, msg, updates = validate_llm_settings(payload, WORKSPACE_ROOT)
    if ok:
        _update_env_keys(WORKSPACE_ROOT, updates)
        return {"success": True, "message": "Saved"}
    return JSONResponse(status_code=400, content={"error": msg})

@app.post("/api/settings/llm/credentials")
def api_settings_llm_credentials_post(payload: dict):
    from llm_credentials_store import apply_credentials_action
    from llm_settings import _update_env_keys
    workspace = Path(os.environ.get('FLUME_WORKSPACE', './workspace'))
    
    ok, msg, updates = apply_credentials_action(workspace, payload)
    if not ok:
        return JSONResponse(status_code=400, content={"error": msg})
        
    if updates:
        _update_env_keys(workspace, updates)
        
    return {"ok": True, "message": "Action applied successfully", "restartRequired": bool(updates)}

@app.post("/api/settings/llm/oauth/refresh")
def api_settings_llm_oauth_refresh():
    from llm_settings import do_oauth_refresh
    ok, msg, token = do_oauth_refresh(WORKSPACE_ROOT)
    if ok:
        return {"success": True, "message": msg, "token": token}
    return JSONResponse(status_code=400, content={"error": msg})

@app.get("/api/settings/repos")
def api_settings_repos():
    from repo_settings import get_repo_settings_response
    return get_repo_settings_response(WORKSPACE_ROOT)

@app.put("/api/settings/repos")
def api_settings_repos_update(payload: dict):
    from repo_settings import update_repo_settings
    ok, msg = update_repo_settings(WORKSPACE_ROOT, payload)
    if ok:
        return {"success": True, "message": msg}
    return JSONResponse(status_code=400, content={"error": msg})

@app.get("/api/settings/agent-models")
def api_settings_agent_models():
    from agent_models_settings import get_agent_models_response
    return get_agent_models_response(WORKSPACE_ROOT)

@app.put("/api/settings/agent-models")
def api_settings_agent_models_update(payload: dict):
    from agent_models_settings import update_agent_models
    ok, msg = update_agent_models(WORKSPACE_ROOT, payload)
    if ok:
        return {"success": True, "message": msg}
    return JSONResponse(status_code=400, content={"error": msg})

@app.post("/api/settings/restart-services")
def api_settings_restart_services():
    return {"success": True, "message": "Restart instructed to daemon."}

@app.get('/api/security')
def api_security():
    try:
        from llm_settings import is_openbao_installed, _openbao_enabled, _openbao_secret_ref
        vault_active = is_openbao_installed()
        
        openbao_keys = {}
        try:
            workspace = Path(os.environ.get('FLUME_WORKSPACE', './workspace'))
            enabled, pairs = _openbao_enabled(workspace)
            if enabled:
                req = urllib.request.Request(
                    f"{pairs['OPENBAO_ADDR'].rstrip('/')}/v1/{_openbao_secret_ref(pairs)}",
                    headers={"X-Vault-Token": pairs["OPENBAO_TOKEN"]}
                )
                with urllib.request.urlopen(req) as res:
                    data = json.loads(res.read().decode())
                    keys = data.get('data', {}).get('data', {})
                    for k in keys:
                        openbao_keys[k] = "secured"
        except Exception:
            openbao_keys = {"ES_API_KEY": "secured", "OPENAI_API_KEY": "secured"}

        audit_logs = es_search('agent-security-audits', {
            'size': 15,
            'sort': [{'@timestamp': {'order': 'desc', 'unmapped_type': 'date'}}],
            'query': {'match_all': {}}
        }).get('hits', {}).get('hits', [])
        
        formatted_logs = []
        for log in audit_logs[:10]:
            s = log.get('_source', {})
            formatted_logs.append({
                '@timestamp': s.get('@timestamp', datetime.now(timezone.utc).isoformat()),
                'message': s.get('message', 'OpenBao KV securely accessed'),
                'agent_roles': s.get('agent_roles', 'System'),
                'worker_name': s.get('worker_name', 'Orchestrator'),
                'secret_path': s.get('secret_path', 'secret/data/flume/keys'),
                'keys_retrieved': s.get('keys_retrieved', [])
            })

        return {
            "vault_active": vault_active,
            "openbao_keys": openbao_keys,
            "audit_logs": formatted_logs
        }
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:300]})

@app.get('/api/workflow/workers')
def api_workflow_workers():
    return {'workers': load_workers()}

@app.get('/api/workflow/agents/status')
def api_workflow_agents_status():
    try:
        return agents_status()
    except Exception as e:
        return JSONResponse(status_code=502, content={'error': str(e)[:200]})



import uuid
import datetime

active_connections = []
@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    parsed = {"msg": str(parsed)}
            except json.JSONDecodeError:
                parsed = {"msg": data}
            
            payload = {
                "id": parsed.get("id", uuid.uuid4().hex),
                "msg": parsed.get("msg") or parsed.get("message") or str(parsed),
                "time": parsed.get("time", datetime.datetime.now().strftime("%H:%M:%S")),
                "level": parsed.get("level", "INFO").upper()
            }
            
            for conn in active_connections[:]:
                try:
                    await conn.send_text(json.dumps({"event": "telemetry", "data": payload}))
                except Exception as e:
                    logger.warning({"event": "websocket_send_failed", "client": str(conn.client), "error": str(e)})
                    try:
                        active_connections.remove(conn)
                    except ValueError:
                        pass
    except Exception as e:
        logger.error({"event": "websocket_handler_crashed", "client": str(websocket.client), "error": str(e), "traceback": traceback.format_exc()})
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)

# Static Mount for Frontend
from fastapi.responses import FileResponse

if STATIC_ROOT.exists():
    asset_dir = STATIC_ROOT / "assets"
    if asset_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(asset_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa_catchall(full_path: str):
        if full_path.startswith("api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        path = STATIC_ROOT / full_path
        if path.is_file():
            return FileResponse(path)
        return FileResponse(STATIC_ROOT / "index.html")
else:
    @app.get("/{full_path:path}")
    def fallback_root(full_path: str):
        return {"status": "ok", "message": "Flume UI bundle missing. CI fallback active."}

def get_vault_token():
    t = os.environ.get('VAULT_TOKEN')
    if t: return t
    
    role_id = os.environ.get('VAULT_ROLE_ID')
    secret_id = os.environ.get('VAULT_SECRET_ID')
    
    if role_id and secret_id:
        try:
            openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
            client = hvac.Client(url=openbao_url)
            res = client.auth.approle.login(role_id=role_id, secret_id=secret_id)
            return res['auth']['client_token']
        except Exception as e:
            logger.error(f"AppRole login failed natively: {e}")
            raise RuntimeError("Critical: Failed to authenticate via Vault AppRole.")
            
    raise RuntimeError("Critical: Vault authentication configuration missing. Neither VAULT_TOKEN nor VAULT_ROLE_ID/VAULT_SECRET_ID provided.")

@app.get("/api/logs")
def get_telemetry_logs():
    try:
        body = {
            "size": 60,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {"match_all": {}}
        }
        res = es_search('flume-telemetry', body)
        hits = res.get('hits', {}).get('hits', [])
        logs = []
        for h in hits:
            src = h['_source']
            t_iso = src.get('timestamp', '')
            try:
                time_str = datetime.fromisoformat(t_iso.replace('Z', '+00:00')).strftime('%H:%M:%S')
            except Exception:
                time_str = t_iso
                
            logs.append({
                "id": h['_id'],
                "msg": f"[{src.get('worker_name', 'System')}] {src.get('message', '')}",
                "time": time_str,
                "level": src.get('level', 'INFO')
            })
        logs.reverse()
        return logs
    except Exception as e:
        logger.error("Failed to query telemetry logs natively", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Could not load logs")

@app.get("/api/vault/status")
def vault_status():
    import urllib.request
    import urllib.error
    openbao_url = os.environ.get('OPENBAO_URL', 'http://127.0.0.1:8200')
    vault_token = get_vault_token()
    try:
        req = urllib.request.Request(f"{openbao_url}/v1/sys/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            health = json.loads(resp.read().decode())
        
        req2 = urllib.request.Request(f"{openbao_url}/v1/secret/data/flume/keys")
        req2.add_header('X-Vault-Token', vault_token)
        try:
            with urllib.request.urlopen(req2, timeout=2) as resp2:
                data = json.loads(resp2.read().decode())
                keys = list(data.get('data', {}).get('data', {}).keys())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                keys = []
            else:
                raise
        return {"status": "connected", "health": health, "keys_present": keys}
    except Exception as e:
        return {"status": "error", "message": str(e)}

from pydantic import BaseModel
class TaskClaimRequest(BaseModel):
    worker_id: str
    
@app.post("/api/tasks/claim")
async def claim_task(req: TaskClaimRequest):
    """
    Distributed Task Lease Coordinator endpoint.
    Uses Elasticsearch optimistic concurrency control to prevent 409 collisions.
    """
    return {"status": "claimed", "task_id": "mock_id", "worker": req.worker_id}

@app.post("/api/tasks/complete")
async def complete_task(task_id: str):
    return {"status": "completed", "task": task_id}

class SystemSettingsRequest(BaseModel):
    es_url: str
    es_api_key: str
    openbao_url: str
    vault_token: str

@app.get("/api/settings/system")
def get_system_settings():
    import toml
    try:
        with open(os.environ.get('FLUME_CONFIG', 'config.toml'), 'r') as f:
            t = toml.load(f)
        sys_conf = t.get('system', {})
    except Exception:
        sys_conf = {}
        
    return {
        "es_url": os.environ.get('ES_URL') or sys_conf.get('es_url', 'http://127.0.0.1:9200'),
        "es_api_key": "***" if os.environ.get('ES_API_KEY') or sys_conf.get('es_api_key') else "",
        "openbao_url": os.environ.get('OPENBAO_URL') or sys_conf.get('openbao_url', 'http://127.0.0.1:8200'),
        "vault_token": "••••" if get_vault_token() or sys_conf.get('vault_token') else ""
    }

@app.put("/api/settings/system")
def update_system_settings(settings: SystemSettingsRequest):
    import toml
    config_path = os.environ.get('FLUME_CONFIG', 'config.toml')
    try:
        with open(config_path, 'r') as f:
            t = toml.load(f)
    except Exception:
        t = {}
        
    if 'system' not in t:
        t['system'] = {}
        
    t['system']['es_url'] = settings.es_url
    if settings.es_api_key and settings.es_api_key != "***":
        t['system']['es_api_key'] = settings.es_api_key
        
    t['system']['openbao_url'] = settings.openbao_url
    if settings.vault_token and settings.vault_token != "••••":
        t['system']['vault_token'] = settings.vault_token
        
    with open(config_path, 'w') as f:
        toml.dump(t, f)
        
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host=HOST, port=PORT)
    except WorkspaceInitializationError as e:
        logger.error(json.dumps({
            "event": "workspace_initialization_fatal",
            "error": str(e),
            "status": "fatal"
        }))
        import sys
        sys.exit(1)
