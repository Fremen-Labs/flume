#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.request
import urllib.parse
import uuid
from datetime import datetime

BASE = Path(__file__).resolve().parent
_SRC_ROOT = BASE.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


from flume_secrets import apply_runtime_config  # noqa: E402

# Merge .env (src then repo root, repo wins) + OpenBao KV; see flume_secrets.load_legacy_dotenv_into_environ
apply_runtime_config(_SRC_ROOT)

from llm_settings import load_effective_pairs  # noqa: E402

ES_URL = os.environ.get('ES_URL', 'https://localhost:9200').rstrip('/')
ES_API_KEY = os.environ.get('ES_API_KEY', '')
ES_VERIFY_TLS = os.environ.get('ES_VERIFY_TLS', 'false').lower() == 'true'
HOST = os.environ.get('DASHBOARD_HOST', '0.0.0.0')
PORT = int(os.environ.get('DASHBOARD_PORT', '8765'))
STATIC_ROOT = Path(os.environ.get('LOOM_FRONTEND_DIST', str(Path(__file__).parent.parent / 'frontend' / 'dist')))
WORKSPACE_ROOT = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent)))
WORKER_STATE = WORKSPACE_ROOT / 'worker-manager/state.json'
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

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


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


def load_projects_registry():
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
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_search",
        data=json.dumps(body).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {ES_API_KEY}',
        },
        method='GET',
    )
    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())


def es_index(index, doc):
    req = urllib.request.Request(
        f"{ES_URL}/{index}/_doc",
        data=json.dumps(doc).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'ApiKey {ES_API_KEY}',
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
            'Authorization': f'ApiKey {ES_API_KEY}',
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
            'Authorization': f'ApiKey {ES_API_KEY}',
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


def call_ollama(messages):
    """Call the configured LLM and return the assistant's response text."""
    import llm_client
    return llm_client.chat(messages, model=LLM_MODEL, temperature=0.3, max_tokens=8192)

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
    try:
        raw = call_ollama(llm_messages)
        message, plan = parse_llm_response(raw)
    except Exception as e:
        plan = simple_plan(repo, prompt)
        message = (
            "I've created an initial breakdown based on your request. "
            "Feel free to refine it — you can edit items directly or ask me to adjust anything."
        )

    if not plan or not plan.get('epics'):
        plan = simple_plan(repo, prompt)
        if not message:
            message = (
                "Here's an initial breakdown of your request. "
                "Let me know what you'd like to change."
            )

    session['draftPlan'] = plan
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
        raw = call_ollama(llm_messages)
        message, plan = parse_llm_response(raw)
    except Exception as e:
        message = f"I encountered an issue processing your request. Please try again. (Error: {str(e)[:100]})"
        plan = None

    if plan and plan.get('epics'):
        session['draftPlan'] = plan
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


def simple_plan(repo: str, prompt: str):
    """
    Temporary planner: generate a minimal epic/feature/story/task tree
    from a free-text prompt. This can be replaced by a real OpenClaw
    Intake/Planner agent later.
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
                        'title': 'Initial implementation',
                        'stories': [
                            {
                                'id': story_id,
                                'title': 'Implement core flow',
                                'acceptanceCriteria': [
                                    'Core path works end-to-end',
                                    'Basic happy-path tests are passing',
                                ],
                                'tasks': [
                                    {
                                        'id': task_id,
                                        'title': 'Implement core logic and tests',
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


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


def load_workers():
    if WORKER_STATE.exists():
        return json.loads(WORKER_STATE.read_text()).get('workers', [])
    return []


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
    find_res = es_search('agent-task-records', {
        'size': 1,
        'query': {'term': {'id': task_id}},
    }).get('hits', {}).get('hits', [])
    if not find_res:
        return None
    es_id = find_res[0].get('_id')
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
    es_post(f'agent-task-records/_update/{es_id}', {'doc': doc})
    return {'_id': es_id, 'id': task_id, **doc}


def task_history(task_id: str):
    task_hits = es_search('agent-task-records', {
        'size': 1,
        'query': {'term': {'id': task_id}},
    }).get('hits', {}).get('hits', [])
    if not task_hits:
        return None
    task = {'_id': task_hits[0].get('_id'), **task_hits[0].get('_source', {})}

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
    hits = es_search('agent-task-records', {
        'size': 1,
        'query': {'term': {'id': task_id}},
    }).get('hits', {}).get('hits', [])
    if not hits:
        return None, None
    h = hits[0]
    return h.get('_id'), h.get('_source', {})


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
        raise RuntimeError(
            'Elasticsearch not configured. ES_API_KEY is missing or invalid. '
            'Set it in OpenBao KV (secret/flume) or .env, or run: '
            'ELASTIC_PASSWORD=yourpassword bash install/setup/bootstrap-es-credentials.sh'
        )
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
    return {
        'workers': load_workers(),
        'tasks': [{'_id': h.get('_id'), **h.get('_source', {})} for h in tasks],
        'reviews': [{'_id': h.get('_id'), **h.get('_source', {})} for h in reviews],
        'failures': [{'_id': h.get('_id'), **h.get('_source', {})} for h in failures],
        'provenance': [{'_id': h.get('_id'), **h.get('_source', {})} for h in provenance],
        'repos': load_repos(),
        'projects': load_projects_registry(),
    }


# ─── Agent process control ────────────────────────────────────────────────────

WORKER_MANAGER_SCRIPT = WORKSPACE_ROOT / 'worker-manager' / 'manager.py'
WORKER_HANDLERS_SCRIPT = WORKSPACE_ROOT / 'worker-manager' / 'worker_handlers.py'
WORKER_ENV_FILE = WORKSPACE_ROOT / 'memory' / 'es' / '.env.local'


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

    if not pids['manager']:
        proc = subprocess.Popen(
            ['python3', str(WORKER_MANAGER_SCRIPT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        started.append({'role': 'manager', 'pid': proc.pid})

    if not pids['handlers']:
        proc = subprocess.Popen(
            ['python3', str(WORKER_HANDLERS_SCRIPT)],
            env=env,
            cwd=str(WORKSPACE_ROOT / 'worker-manager'),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        started.append({'role': 'handlers', 'pid': proc.pid})

    return {'ok': True, 'started': started, 'already_running': not started}


class Handler(BaseHTTPRequestHandler):
    def _json_response(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        body = self.rfile.read(length).decode() if length > 0 else '{}'
        return json.loads(body)

    def do_GET(self):
        if self.path == '/api/snapshot':
            try:
                self._json_response(200, load_snapshot())
            except Exception as e:
                err_msg = str(e)[:400]
                self._json_response(502, {'error': err_msg, 'code': 'ES_CONNECTION'})
            return

        if self.path == '/api/workflow/workers':
            self._json_response(200, {'workers': load_workers()})
            return

        if self.path == '/api/workflow/agents/status':
            try:
                self._json_response(200, agents_status())
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
            return

        if self.path == '/api/settings/llm':
            try:
                import llm_settings
                data = llm_settings.get_llm_settings_response(WORKSPACE_ROOT)
                self._json_response(200, data)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:300]})
            return

        if self.path == '/api/settings/repos':
            try:
                import repo_settings
                data = repo_settings.get_repo_settings_response(WORKSPACE_ROOT)
                self._json_response(200, data)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:300]})
            return

        queue_match = re.match(r'^/api/queue/([^/]+)$', self.path)
        if queue_match:
            repo_id = queue_match.group(1)
            try:
                self._json_response(200, {'repo': repo_id, 'items': queue_for_repo(repo_id)})
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
            return

        history_match = re.match(r'^/api/tasks/([^/]+)/history$', self.path)
        if history_match:
            task_id = history_match.group(1)
            try:
                data = task_history(task_id)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            if not data:
                self._json_response(404, {'error': 'Task not found'})
                return
            self._json_response(200, data)
            return

        task_git_match = re.match(r'^/api/tasks/([^/]+)/git$', self.path)
        if task_git_match:
            task_id = task_git_match.group(1)
            try:
                _, task = get_task_doc(task_id)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            if not task:
                self._json_response(404, {'error': 'Task not found'})
                return
            self._json_response(200, {
                'task_id': task_id,
                'branch': task.get('branch'),
                'target_branch': task.get('target_branch'),
                'commit_sha': task.get('commit_sha'),
                'commit_message': task.get('commit_message'),
                'pr_url': task.get('pr_url'),
                'pr_number': task.get('pr_number'),
                'pr_status': task.get('pr_status'),
            })
            return

        task_diff_match = re.match(r'^/api/tasks/([^/]+)/diff$', self.path)
        if task_diff_match:
            task_id = task_diff_match.group(1)
            try:
                data = task_diff(task_id)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200], 'branch': None, 'files': [], 'diff': '', 'truncated': False})
                return
            self._json_response(200, data)
            return

        task_commits_match = re.match(r'^/api/tasks/([^/]+)/commits$', self.path)
        if task_commits_match:
            task_id = task_commits_match.group(1)
            try:
                data = task_commits(task_id)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200], 'branch': None, 'commits': []})
                return
            self._json_response(200, data)
            return

        project_match = re.match(r'^/api/projects/([^/]+)$', self.path)
        if project_match:
            proj_id = project_match.group(1)
            registry = load_projects_registry()
            proj = next((p for p in registry if p['id'] == proj_id), None)
            if not proj:
                self._json_response(404, {'error': 'Project not found'})
                return
            self._json_response(200, proj)
            return

        session_match = re.match(r'^/api/intake/session/([^/]+)$', self.path)
        if session_match:
            session = load_session(session_match.group(1))
            if not session:
                self._json_response(404, {'error': 'Session not found'})
                return
            self._json_response(200, {
                'sessionId': session['id'],
                'repo': session['repo'],
                'status': session['status'],
                'messages': session['messages'],
                'plan': session['draftPlan'],
            })
            return

        # ── Repo branches ─────────────────────────────────────────────────────────
        _parsed_path = urllib.parse.urlparse(self.path)
        repo_branches_match = re.match(r'^/api/repos/([^/]+)/branches$', _parsed_path.path)
        if repo_branches_match:
            repo_id = urllib.parse.unquote(repo_branches_match.group(1))
            registry = load_projects_registry()
            proj = next((p for p in registry if p['id'] == repo_id), None)
            if not proj:
                self._json_response(404, {'error': 'Project not found'})
                return
            repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
            if not (repo_path / '.git').exists():
                self._json_response(404, {'error': 'Not a git repository'})
                return
            try:
                default_branch = resolve_default_branch(
                    repo_path, proj.get('gitflow', {}).get('defaultBranch')
                )
                raw = subprocess.check_output(
                    ['git', '-C', str(repo_path), 'branch', '--format=%(refname:short)'],
                    stderr=subprocess.DEVNULL,
                ).decode(errors='replace')
                branches = [b.strip() for b in raw.splitlines() if b.strip()]
                # Put default branch first
                if default_branch in branches:
                    branches.remove(default_branch)
                    branches.insert(0, default_branch)
                self._json_response(200, {'default': default_branch, 'branches': branches})
            except subprocess.CalledProcessError as exc:
                self._json_response(500, {'error': str(exc)})
            return

        # ── Repo file tree ────────────────────────────────────────────────────────
        repo_tree_match = re.match(r'^/api/repos/([^/]+)/tree$', _parsed_path.path)
        if repo_tree_match:
            repo_id = urllib.parse.unquote(repo_tree_match.group(1))
            qs = dict(urllib.parse.parse_qsl(_parsed_path.query))
            registry = load_projects_registry()
            proj = next((p for p in registry if p['id'] == repo_id), None)
            if not proj:
                self._json_response(404, {'error': 'Project not found'})
                return
            repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
            if not (repo_path / '.git').exists():
                self._json_response(404, {'error': 'Not a git repository'})
                return
            branch = qs.get('branch') or resolve_default_branch(
                repo_path, proj.get('gitflow', {}).get('defaultBranch')
            )
            try:
                raw = subprocess.check_output(
                    ['git', '-C', str(repo_path), 'ls-tree', '-r', '--long', branch],
                    stderr=subprocess.DEVNULL,
                ).decode(errors='replace')
                entries = []
                for line in raw.splitlines():
                    # format: <mode> <type> <sha> <size>\t<path>
                    parts = line.split('\t', 1)
                    if len(parts) != 2:
                        continue
                    meta, path = parts
                    meta_parts = meta.split()
                    obj_type = meta_parts[1] if len(meta_parts) >= 2 else 'blob'
                    size = meta_parts[3] if len(meta_parts) >= 4 else '-'
                    entries.append({'path': path, 'type': obj_type, 'size': size})
                # Also add directory entries by collecting parent paths
                dirs = set()
                for e in entries:
                    parts = e['path'].split('/')
                    for i in range(1, len(parts)):
                        dirs.add('/'.join(parts[:i]))
                for d in dirs:
                    entries.append({'path': d, 'type': 'tree', 'size': '-'})
                self._json_response(200, {'branch': branch, 'entries': entries})
            except subprocess.CalledProcessError as exc:
                self._json_response(500, {'error': f'git ls-tree failed: {exc}'})
            return

        # ── Repo single file ──────────────────────────────────────────────────────
        repo_file_match = re.match(r'^/api/repos/([^/]+)/file$', _parsed_path.path)
        if repo_file_match:
            repo_id = urllib.parse.unquote(repo_file_match.group(1))
            qs = dict(urllib.parse.parse_qsl(_parsed_path.query))
            file_path = qs.get('path', '')
            branch = qs.get('branch', 'HEAD')
            if not file_path:
                self._json_response(400, {'error': 'path query param required'})
                return
            registry = load_projects_registry()
            proj = next((p for p in registry if p['id'] == repo_id), None)
            if not proj:
                self._json_response(404, {'error': 'Project not found'})
                return
            repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
            if not (repo_path / '.git').exists():
                self._json_response(404, {'error': 'Not a git repository'})
                return
            try:
                content_bytes = subprocess.check_output(
                    ['git', '-C', str(repo_path), 'show', f'{branch}:{file_path}'],
                    stderr=subprocess.DEVNULL,
                )
                # Detect binary: if >20% non-printable bytes treat as binary
                sample = content_bytes[:4096]
                non_printable = sum(1 for b in sample if b < 9 or (13 < b < 32))
                is_binary = len(sample) > 0 and (non_printable / len(sample)) > 0.2
                if is_binary:
                    self._json_response(200, {'binary': True, 'size': len(content_bytes)})
                else:
                    self._json_response(200, {
                        'binary': False,
                        'content': content_bytes.decode('utf-8', errors='replace'),
                        'size': len(content_bytes),
                    })
            except subprocess.CalledProcessError:
                self._json_response(404, {'error': f'File not found: {file_path} on {branch}'})
            return

        # ── Repo branch diff ──────────────────────────────────────────────────────
        repo_diff_match = re.match(r'^/api/repos/([^/]+)/diff$', _parsed_path.path)
        if repo_diff_match:
            repo_id = urllib.parse.unquote(repo_diff_match.group(1))
            qs = dict(urllib.parse.parse_qsl(_parsed_path.query))
            base_branch = qs.get('base', '')
            head_branch = qs.get('head', '')
            if not base_branch or not head_branch:
                self._json_response(400, {'error': 'base and head query params are required'})
                return
            registry = load_projects_registry()
            proj = next((p for p in registry if p['id'] == repo_id), None)
            if not proj:
                self._json_response(404, {'error': 'Project not found'})
                return
            repo_path = Path(proj.get('path') or str(WORKSPACE_ROOT / repo_id))
            if not (repo_path / '.git').exists():
                self._json_response(404, {'error': 'Not a git repository'})
                return
            try:
                MAX_DIFF_LINES = 3000

                # Per-file summary via --stat
                files = []
                try:
                    stat_raw = subprocess.check_output(
                        ['git', '-C', str(repo_path), 'diff', f'{base_branch}...{head_branch}', '--stat', '--stat-width=200'],
                        stderr=subprocess.DEVNULL,
                    ).decode(errors='replace')
                    for line in stat_raw.splitlines():
                        line = line.strip()
                        if not line or line.startswith('(') or '|' not in line:
                            continue
                        parts = line.split('|')
                        if len(parts) < 2:
                            continue
                        path_part = parts[0].strip()
                        bars = parts[1].strip().split()
                        plus_count = bars[1].count('+') if len(bars) > 1 else 0
                        minus_count = bars[1].count('-') if len(bars) > 1 else 0
                        if '->' in path_part:
                            status = 'renamed'
                        elif plus_count > 0 and minus_count == 0:
                            status = 'added'
                        elif minus_count > 0 and plus_count == 0:
                            status = 'deleted'
                        else:
                            status = 'modified'
                        files.append({'path': path_part, 'insertions': plus_count, 'deletions': minus_count, 'status': status})
                except subprocess.CalledProcessError:
                    pass

                # Full unified diff
                diff_text = ''
                truncated = False
                try:
                    raw_diff = subprocess.check_output(
                        ['git', '-C', str(repo_path), 'diff', f'{base_branch}...{head_branch}', '-U3'],
                        stderr=subprocess.DEVNULL,
                    ).decode(errors='replace')
                    lines = raw_diff.splitlines(keepends=True)
                    if len(lines) > MAX_DIFF_LINES:
                        lines = lines[:MAX_DIFF_LINES]
                        truncated = True
                    diff_text = ''.join(lines)
                except subprocess.CalledProcessError:
                    pass

                self._json_response(200, {
                    'base': base_branch,
                    'head': head_branch,
                    'files': files,
                    'diff': diff_text,
                    'truncated': truncated,
                    'identical': len(files) == 0 and not diff_text.strip(),
                })
            except Exception as exc:
                self._json_response(500, {'error': str(exc)[:300]})
            return

        if self.path.startswith('/api/'):
            self.send_response(404)
            self.end_headers()
            return

        # Try to serve static asset first
        asset_path = STATIC_ROOT / self.path.lstrip('/')
        if asset_path.is_file():
            if asset_path.suffix in ('.js', '.mjs'):
                content_type = 'application/javascript'
            elif asset_path.suffix == '.css':
                content_type = 'text/css'
            elif asset_path.suffix in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                content_type = 'image/' + asset_path.suffix.lstrip('.').replace('jpg', 'jpeg')
            else:
                content_type = 'application/octet-stream'
            content = asset_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # Fallback: serve index.html for client-side routing (/agents, /projects, etc.)
        index_path = STATIC_ROOT / 'index.html'
        if not index_path.exists():
            self.send_response(500)
            self.end_headers()
            return
        content = index_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path == '/api/settings/llm':
            try:
                import llm_settings
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._json_response(400, {'error': 'Invalid JSON body'})
                return
            except Exception as e:
                self._json_response(502, {'error': str(e)[:300]})
                return
            ok, err, updates = llm_settings.validate_llm_settings(payload, WORKSPACE_ROOT)
            if not ok:
                self._json_response(422, {'ok': False, 'error': err})
                return
            try:
                llm_settings._update_env_keys(WORKSPACE_ROOT, updates)
            except Exception as e:
                self._json_response(502, {'ok': False, 'error': str(e)[:300]})
                return
            self._json_response(200, {'ok': True, 'restartRequired': True})
            return

        if self.path == '/api/settings/llm/oauth/refresh':
            try:
                import llm_settings
                ok, msg, extra = llm_settings.do_oauth_refresh(WORKSPACE_ROOT)
            except Exception as e:
                self._json_response(502, {'ok': False, 'error': str(e)[:300]})
                return
            if not ok:
                self._json_response(422, {'ok': False, 'error': msg})
                return
            self._json_response(200, {'ok': True, 'message': msg, 'restartRequired': True, **(extra or {})})
            return

        if self.path == '/api/settings/repos':
            try:
                import repo_settings
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._json_response(400, {'error': 'Invalid JSON body'})
                return
            except Exception as e:
                self._json_response(502, {'error': str(e)[:300]})
                return

            ok, err = repo_settings.update_repo_settings(WORKSPACE_ROOT, payload)
            if not ok:
                self._json_response(422, {'ok': False, 'error': err})
                return
            self._json_response(200, {'ok': True, 'restartRequired': True})
            return

        if self.path == '/api/workflow/agents/stop':
            try:
                result = agents_stop()
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            self._json_response(200, result)
            return

        if self.path == '/api/workflow/agents/start':
            try:
                result = agents_start()
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            self._json_response(200, result)
            return

        # ── Repo branch management (local branch list/delete) ────────────────
        repo_branches_delete_match = re.match(r'^/api/repos/([^/]+)/branches/delete$', self.path)
        if repo_branches_delete_match:
            repo_id = urllib.parse.unquote(repo_branches_delete_match.group(1))
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._json_response(400, {'error': 'Invalid JSON body'})
                return

            branches = payload.get('branches') or []
            force = bool(payload.get('force') or False)
            result = delete_repo_branches(repo_id=repo_id, branches=branches, force=force)
            if not result.get('ok'):
                self._json_response(422, result)
                return
            self._json_response(200, result)
            return

        if self.path == '/api/intake':
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            repo = str(payload.get('repo') or 'unassigned')
            prompt = str(payload.get('prompt') or '').strip()
            if not prompt:
                self.send_response(400)
                self.end_headers()
                return
            now = json.dumps(payload.get('timestamp') or '').strip('"') or \
                datetime.utcnow().isoformat() + 'Z'
            title = (prompt.splitlines()[0] or 'New request').strip()
            if len(title) > 80:
                title = title[:77] + '...'
            doc = {
                'id': f'task-{now}',
                'title': title,
                'objective': prompt,
                'repo': repo,
                'worktree': None,
                'owner': 'intake',
                'status': 'inbox',
                'priority': 'normal',
                'depends_on': [],
                'acceptance_criteria': [],
                'artifacts': [],
                'last_update': now,
                'needs_human': False,
                'risk': 'medium',
            }
            try:
                res = es_upsert('agent-task-records', doc['id'], doc)
            except Exception:
                self.send_response(502)
                self.end_headers()
                return
            self._json_response(201, {'ok': True, 'task': doc, 'es': res})
            return

        if self.path == '/api/intake/plan':
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            repo = str(payload.get('repo') or 'unassigned')
            prompt = str(payload.get('prompt') or '').strip()
            if not prompt:
                self.send_response(400)
                self.end_headers()
                return
            plan = simple_plan(repo, prompt)
            self._json_response(200, {'ok': True, 'plan': plan})
            return

        if self.path == '/api/intake/commit':
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            repo = str(payload.get('repo') or 'unassigned')
            plan = payload.get('plan') or {}
            try:
                docs, res = commit_plan(repo, plan)
            except Exception:
                self.send_response(502)
                self.end_headers()
                return
            self._json_response(201, {'ok': True, 'count': len(docs)})
            return

        # --- Planning Session Endpoints ---

        if self.path == '/api/intake/session':
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            repo = str(payload.get('repo') or 'unassigned')
            prompt = str(payload.get('prompt') or '').strip()
            if not prompt:
                self._json_response(400, {'error': 'prompt is required'})
                return
            try:
                session = create_planning_session(repo, prompt)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            self._json_response(201, {
                'sessionId': session['id'],
                'messages': session['messages'],
                'plan': session['draftPlan'],
            })
            return

        msg_match = re.match(r'^/api/intake/session/([^/]+)/message$', self.path)
        if msg_match:
            session_id = msg_match.group(1)
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            user_text = str(payload.get('text') or '').strip()
            current_plan = payload.get('plan')
            if not user_text:
                self._json_response(400, {'error': 'text is required'})
                return
            try:
                session = refine_session(session_id, user_text, current_plan)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            if not session:
                self._json_response(404, {'error': 'Session not found'})
                return
            self._json_response(200, {
                'sessionId': session['id'],
                'messages': session['messages'],
                'plan': session['draftPlan'],
            })
            return

        commit_match = re.match(r'^/api/intake/session/([^/]+)/commit$', self.path)
        if commit_match:
            session_id = commit_match.group(1)
            session = load_session(session_id)
            if not session:
                self._json_response(404, {'error': 'Session not found'})
                return
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                payload = {}
            final_plan = payload.get('plan') or session.get('draftPlan') or {}
            repo = session.get('repo', 'unassigned')
            try:
                docs, res = commit_plan(repo, final_plan)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            session['status'] = 'committed'
            session['draftPlan'] = final_plan
            save_session(session)
            self._json_response(201, {'ok': True, 'count': len(docs)})
            return

        # --- Bulk work item operations (archive / delete) ---

        if self.path == '/api/tasks/bulk-update':
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            repo = payload.get('repo')
            ids = payload.get('ids') or []
            action = str(payload.get('action') or '').lower()

            if not ids or action not in ('archive', 'delete'):
                self._json_response(400, {'error': 'ids and valid action (archive|delete) are required'})
                return

            query = {
                'bool': {
                    'must': [
                        {'terms': {'id': ids}},
                    ],
                }
            }
            if repo:
                query['bool']['must'].append({'term': {'repo': repo}})

            deleted_branches = []
            try:
                # Always remove associated git branches — archived items no longer
                # need an active branch, and deleted items obviously don't either.
                deleted_branches = delete_task_branches(ids, repo)

                if action == 'archive':
                    body = {
                        'query': query,
                        'script': {
                            'source': "ctx._source.status = params.status",
                            'lang': 'painless',
                            'params': {'status': 'archived'},
                        },
                    }
                    res = es_post('agent-task-records/_update_by_query?conflicts=proceed', body)
                else:  # delete — ES records are hard-deleted; ids are preserved in counters file
                    body = {'query': query}
                    res = es_post('agent-task-records/_delete_by_query?conflicts=proceed', body)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return

            self._json_response(200, {'ok': True, 'result': res, 'deleted_branches': deleted_branches})
            return

        create_pr_match = re.match(r'^/api/tasks/([^/]+)/create-pr$', self.path)
        if create_pr_match:
            task_id = create_pr_match.group(1)
            try:
                result = create_task_pr(task_id)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:300]})
                return
            code = 200 if result.get('ok') else 422
            self._json_response(code, result)
            return

        gitflow_match = re.match(r'^/api/projects/([^/]+)/gitflow$', self.path)
        if gitflow_match:
            proj_id = gitflow_match.group(1)
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._json_response(400, {'error': 'Invalid JSON body'})
                return
            registry = load_projects_registry()
            proj = next((p for p in registry if p['id'] == proj_id), None)
            if not proj:
                self._json_response(404, {'error': 'Project not found'})
                return
            if 'gitflow' not in proj:
                proj['gitflow'] = {'autoPrOnApprove': True, 'defaultBranch': None}
            if 'autoPrOnApprove' in payload:
                proj['gitflow']['autoPrOnApprove'] = bool(payload['autoPrOnApprove'])
            if 'defaultBranch' in payload:
                proj['gitflow']['defaultBranch'] = payload['defaultBranch'] or None
            save_projects_registry(registry)
            self._json_response(200, {'ok': True, 'project': proj})
            return

        delete_project_match = re.match(r'^/api/projects/([^/]+)/delete$', self.path)
        if delete_project_match:
            proj_id = delete_project_match.group(1)
            # Basic safety against path traversal / unexpected IDs.
            if not re.match(r'^[a-zA-Z0-9_\-\.]+$', proj_id):
                self._json_response(400, {'error': 'Invalid project id'})
                return

            # Optional payload (e.g. { force: true }) — currently unused.
            try:
                payload = self._read_json_body()
            except Exception:
                payload = {}
            force = bool(payload.get('force', False))

            try:
                registry = load_projects_registry()
                proj = next((p for p in registry if p.get('id') == proj_id), None)
                if not proj:
                    self._json_response(404, {'error': 'Project not found'})
                    return

                target_path = Path(proj.get('path') or (WORKSPACE_ROOT / proj_id))

                # Update registry first so a failing ES cleanup doesn't resurrect the project.
                registry = [p for p in registry if p.get('id') != proj_id]
                save_projects_registry(registry)

                # Remove repo directory (workspace repo).
                try:
                    if target_path.exists():
                        shutil.rmtree(target_path)
                except Exception as e:
                    # If filesystem cleanup fails, abort without reporting success.
                    self._json_response(500, {'error': 'Failed to remove project directory', 'detail': str(e)[:200]})
                    return

                # Best-effort ES cleanup (remove tasks/reviews/failures/provenance tied to this repo).
                # Indices use `repo` and/or `task_id` fields (based on worker document shapes).
                try:
                    task_hits = es_search('agent-task-records', {
                        'size': 5000,
                        'query': {'term': {'repo': proj_id}},
                    }).get('hits', {}).get('hits', [])
                    task_ids = [h.get('_source', {}).get('id') for h in task_hits]
                    task_ids = [t for t in task_ids if t]

                    # Delete tasks by repo.
                    es_post(
                        'agent-task-records/_delete_by_query?conflicts=proceed',
                        {'query': {'term': {'repo': proj_id}}},
                    )

                    # Delete failures/provenance by repo (they carry repo/project fields in worker).
                    es_post(
                        'agent-failure-records/_delete_by_query?conflicts=proceed',
                        {'query': {'term': {'repo': proj_id}}},
                    )
                    es_post(
                        'agent-provenance-records/_delete_by_query?conflicts=proceed',
                        {'query': {'term': {'repo': proj_id}}},
                    )

                    # Reviews don't always include repo, so delete by task_id list.
                    if task_ids:
                        es_post(
                            'agent-review-records/_delete_by_query?conflicts=proceed',
                            {'query': {'terms': {'task_id': task_ids}}},
                        )

                    # Handoffs are not shown in the snapshot, but deleting prevents orphaned history.
                    if task_ids:
                        es_post(
                            'agent-handoff-records/_delete_by_query?conflicts=proceed',
                            {'query': {'terms': {'task_id': task_ids}}},
                        )
                except Exception:
                    # Keep project deletion working even if ES is temporarily unavailable.
                    pass

                self._json_response(200, {'ok': True, 'projectDeleted': proj_id})
                return
            except Exception as e:
                self._json_response(502, {'error': str(e)[:300]})
                return

        transition_match = re.match(r'^/api/tasks/([^/]+)/transition$', self.path)
        if transition_match:
            task_id = transition_match.group(1)
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._json_response(400, {'error': 'Invalid JSON body'})
                return
            status = str(payload.get('status') or '').strip().lower()
            owner = payload.get('owner')
            needs_human = payload.get('needs_human')
            allowed = {'inbox', 'triaged', 'planned', 'ready', 'running', 'review', 'done', 'blocked', 'failed', 'archived'}
            if status not in allowed:
                self._json_response(400, {'error': 'Invalid status transition target'})
                return
            try:
                updated = transition_task(task_id, status, owner=owner, needs_human=needs_human)
            except Exception as e:
                self._json_response(502, {'error': str(e)[:200]})
                return
            if not updated:
                self._json_response(404, {'error': 'Task not found'})
                return
            self._json_response(200, {'ok': True, 'task': updated})
            return

        if self.path == '/api/projects':
            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            name = str(payload.get('name') or '').strip()
            repo_url = str(payload.get('repoUrl') or '').strip()

            if not name:
                self._json_response(400, {'error': 'Project name is required'})
                return

            # Sanitise name into a safe directory/id
            safe_id = re.sub(r'[^a-zA-Z0-9_\-\.]', '-', name).strip('-')
            if not safe_id:
                self._json_response(400, {'error': 'Project name contains no valid characters'})
                return

            registry = load_projects_registry()
            if any(p['id'] == safe_id for p in registry):
                self._json_response(409, {'error': f'A project named "{safe_id}" already exists'})
                return

            target_path = WORKSPACE_ROOT / safe_id

            if repo_url:
                if target_path.exists():
                    self._json_response(409, {'error': f'Directory "{safe_id}" already exists in workspace'})
                    return

                # If we have a GitHub token configured, inject it into HTTPS clone URLs so
                # `git clone` doesn't attempt interactive username/password prompts.
                git_url = repo_url
                gh_token = load_effective_pairs(WORKSPACE_ROOT).get('GH_TOKEN', '').strip()
                if (gh_token.startswith('"') and gh_token.endswith('"')) or (gh_token.startswith("'") and gh_token.endswith("'")):
                    gh_token = gh_token[1:-1].strip()
                if gh_token and repo_url.startswith('https://github.com/'):
                    # Transform:
                    #   https://github.com/org/repo -> https://<token>@github.com/org/repo
                    # (Avoid re-injecting if credentials are already present.)
                    if '://' in repo_url and '@' not in repo_url.split('://', 1)[1].split('/', 1)[0]:
                        git_url = re.sub(r'^https://github\.com/', f'https://{gh_token}@github.com/', repo_url)
                try:
                    result = subprocess.run(
                        ['git', 'clone', git_url, str(target_path)],
                        capture_output=True, text=True, timeout=120,
                    )
                except subprocess.TimeoutExpired:
                    self._json_response(504, {'error': 'Clone timed out after 120 seconds'})
                    return

                if result.returncode != 0:
                    stderr_lc = result.stderr.lower()
                    access_keywords = [
                        'authentication failed', 'permission denied',
                        'could not read password', 'repository not found',
                        'access denied', 'invalid credentials',
                        'the requested url returned error: 403',
                        'the requested url returned error: 401',
                        'could not read username', 'terminal prompts disabled',
                    ]
                    detail = result.stderr.strip()[:400]
                    if gh_token:
                        detail = detail.replace(gh_token, '***')

                    # Prevent repeated attempts from immediately failing on "directory exists".
                    # Safe because `safe_id` sanitization constrains this to `WORKSPACE_ROOT/<safe_id>`.
                    try:
                        if target_path.exists() and target_path.parent.resolve() == WORKSPACE_ROOT.resolve():
                            shutil.rmtree(target_path)
                    except Exception:
                        pass

                    if any(k in stderr_lc for k in access_keywords):
                        self._json_response(403, {
                            'error': 'Access denied — cannot clone repository.',
                            'detail': detail,
                        })
                    else:
                        self._json_response(422, {
                            'error': 'Failed to clone repository.',
                            'detail': detail,
                        })
                    return
            else:
                target_path.mkdir(parents=True, exist_ok=True)

            entry = {
                'id': safe_id,
                'name': name,
                'repoUrl': repo_url,
                'path': str(target_path),
                'created_at': datetime.utcnow().isoformat() + 'Z',
                'gitflow': {'autoPrOnApprove': True, 'defaultBranch': None},
            }
            registry.append(entry)
            save_projects_registry(registry)
            self._json_response(201, {'ok': True, 'project': entry})
            return

        self.send_response(404)
        self.end_headers()


if __name__ == '__main__':
    server = HTTPServer((HOST, PORT), Handler)
    print(f'Dashboard listening on http://{HOST}:{PORT}')
    server.serve_forever()
