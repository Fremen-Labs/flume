#!/usr/bin/env python3
"""Apply Flume patches to staged source files.

This script is called by build-package.sh after copying source files into
the staging directory. It replaces hardcoded paths, IPs, model names, and
integrates llm_client.py into the relevant modules.

IMPORTANT: Function body replacements must come BEFORE global variable renames
so the search strings still match (they reference the old variable names).

Usage:
    python3 patch_sources.py <staging_dir>
"""

import re
import sys
from pathlib import Path

if len(sys.argv) != 2:
    print(f'Usage: {sys.argv[0]} <staging_dir>', file=sys.stderr)
    sys.exit(1)

STAGING = Path(sys.argv[1])


def patch(path: Path, replacements: list) -> None:
    if not path.exists():
        print(f'  SKIP (not found): {path.relative_to(STAGING)}', flush=True)
        return
    text = path.read_text()
    original = text
    for old, new in replacements:
        if old in text:
            text = text.replace(old, new, 1)
        else:
            print(f'  WARN: pattern not found in {path.name}: {old[:70]!r}', flush=True)
    if text != original:
        path.write_text(text)
        print(f'  patched {path.relative_to(STAGING)}', flush=True)
    else:
        print(f'  unchanged {path.relative_to(STAGING)}', flush=True)


def patch_re(path: Path, replacements: list) -> None:
    """Apply regex-based replacements."""
    if not path.exists():
        print(f'  SKIP (not found): {path.relative_to(STAGING)}', flush=True)
        return
    text = path.read_text()
    original = text
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    if text != original:
        path.write_text(text)
        print(f'  patched {path.relative_to(STAGING)}', flush=True)
    else:
        print(f'  unchanged {path.relative_to(STAGING)}', flush=True)


# =============================================================================
# dashboard/server.py
# =============================================================================
print('Patching dashboard/server.py...', flush=True)
server_py = STAGING / 'dashboard' / 'server.py'

patch(server_py, [
    # Fix STATIC_ROOT (frontend dist location)
    (
        "STATIC_ROOT = Path('/root/agent-fremenlabs/dist')",
        "STATIC_ROOT = Path(os.environ.get('LOOM_FRONTEND_DIST', str(Path(__file__).parent.parent / 'frontend' / 'dist')))",
    ),

    # Fix WORKSPACE_ROOT
    (
        "WORKSPACE_ROOT = Path('/root/.openclaw/workspace')",
        "WORKSPACE_ROOT = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent)))",
    ),

    # Remove hardcoded seed project (Project-Site-IQ is specific to original machine)
    (
        '''\
    if not PROJECTS_REGISTRY.exists():
        seed = [
            {
                'id': 'Project-Site-IQ',
                'name': 'Project-Site-IQ',
                'repoUrl': '',
                'path': str(WORKSPACE_ROOT / 'Project-Site-IQ'),
                'created_at': '2026-01-01T00:00:00Z',
                'gitflow': {'autoPrOnApprove': True, 'defaultBranch': None},
            }
        ]
        PROJECTS_REGISTRY.write_text(json.dumps(seed, indent=2))
        return seed''',
        '''\
    if not PROJECTS_REGISTRY.exists():
        PROJECTS_REGISTRY.write_text(json.dumps([], indent=2))
        return []''',
    ),

    # LLM constants
    (
        "os.environ.get('OLLAMA_BASE_URL', 'http://10.10.1.15:11434')",
        "os.environ.get('LLM_BASE_URL', 'http://localhost:11434')",
    ),
    (
        "os.environ.get('OLLAMA_MODEL', 'qwen3-coder:30b')",
        "os.environ.get('LLM_MODEL', 'llama3.2')",
    ),
    ('OLLAMA_BASE_URL = ', 'LLM_BASE_URL = '),
    ('OLLAMA_MODEL = ',    'LLM_MODEL = '),
])

# Replace call_ollama() function body using regex (avoids \n escape issues)
if server_py.exists():
    text = server_py.read_text()
    original = text
    text = re.sub(
        r'def call_ollama\(messages\):.*?(?=\n\ndef _strip_json_blocks)',
        '''\
def call_ollama(messages):
    """Call the configured LLM and return the assistant's response text."""
    import llm_client
    return llm_client.chat(messages, model=LLM_MODEL, temperature=0.3, max_tokens=8192)''',
        text,
        flags=re.DOTALL,
    )
    # Fix hardcoded model strings in infer_model() (multiple occurrences)
    text = re.sub(r"return 'qwen3-coder:30b'", "return os.environ.get('LLM_MODEL', 'llama3.2')", text)
    if text != original:
        server_py.write_text(text)
        print('  patched dashboard/server.py (call_ollama + infer_model)', flush=True)


# =============================================================================
# worker-manager/agent_runner.py
# =============================================================================
print('Patching worker-manager/agent_runner.py...', flush=True)
runner_py = STAGING / 'worker-manager' / 'agent_runner.py'

patch(runner_py, [
    # Workspace path
    (
        "BASE = Path('/root/.openclaw/workspace')",
        "BASE = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent)))",
    ),
    # LLM constants
    (
        "os.environ.get('OLLAMA_BASE_URL', 'http://10.10.1.15:11434').rstrip('/')",
        "os.environ.get('LLM_BASE_URL', 'http://localhost:11434').rstrip('/')",
    ),
    (
        "os.environ.get('OLLAMA_MODEL', 'qwen3-coder:30b')",
        "os.environ.get('LLM_MODEL', 'llama3.2')",
    ),
    ('OLLAMA_BASE_URL = ', 'LLM_BASE_URL = '),
    ('OLLAMA_MODEL = ',    'LLM_MODEL = '),
])

# Clean up any stray remaining references (regex, all occurrences)
patch_re(runner_py, [
    (r'\bOLLAMA_MODEL\b',    'LLM_MODEL'),
    (r'\bOLLAMA_BASE_URL\b', 'LLM_BASE_URL'),
])

# Replace _call_ollama() and _call_ollama_tools() using regex (avoids \n escape issues)
if runner_py.exists():
    text = runner_py.read_text()
    original = text
    text = re.sub(
        r'def _call_ollama\(system_prompt:.*?(?=\n\n_IMPLEMENTER_TOOLS)',
        '''\
def _call_ollama(system_prompt: str, user_payload: dict[str, Any], model: Optional[str] = None) -> Optional[dict[str, Any]]:
    import llm_client
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': json.dumps(user_payload, indent=2)},
    ]
    try:
        content = llm_client.chat(messages, model=model or LLM_MODEL, temperature=0.2, max_tokens=2048)
        content = content.strip()
        if content.startswith('`' * 3):
            content = content.strip('`').replace('json\\n', '', 1).strip()
        return json.loads(content)
    except Exception:
        return None


''',
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r'def _call_ollama_tools\(messages:.*?(?=\n\ndef run_implementer)',
        '''\
def _call_ollama_tools(messages: list, tools: list, model: str) -> Optional[dict]:
    import sys
    import llm_client
    try:
        return llm_client.chat_with_tools(messages, tools, model=model, temperature=0.2, max_tokens=4096)
    except Exception as e:
        print(f'[agent_runner] _call_ollama_tools error: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
        return None''',
        text,
        flags=re.DOTALL,
    )
    if text != original:
        runner_py.write_text(text)
        print('  patched worker-manager/agent_runner.py (LLM functions)', flush=True)


# =============================================================================
# worker-manager/manager.py
# =============================================================================
print('Patching worker-manager/manager.py...', flush=True)
manager_py = STAGING / 'worker-manager' / 'manager.py'

if manager_py.exists():
    text = manager_py.read_text()
    original = text

    # Fix hardcoded BASE path
    text = text.replace(
        "BASE = Path('/root/.openclaw/workspace/worker-manager')",
        "BASE = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent))) / 'worker-manager'",
    )

    # Replace hardcoded execution hosts in WORKERS list
    text = re.sub(
        r"'execution_host':\s*'(elara|rocky-vm)'",
        "'execution_host': os.environ.get('EXECUTION_HOST', 'localhost')",
        text,
    )

    # Replace hardcoded model strings in WORKERS list
    text = re.sub(
        r"'model':\s*'qwen3-coder:30b'",
        "'model': os.environ.get('LLM_MODEL', 'llama3.2')",
        text,
    )

    if text != original:
        manager_py.write_text(text)
        print('  patched worker-manager/manager.py', flush=True)
    else:
        print('  unchanged worker-manager/manager.py', flush=True)


# =============================================================================
# worker-manager/worker_handlers.py
# =============================================================================
print('Patching worker-manager/worker_handlers.py...', flush=True)
wh_py = STAGING / 'worker-manager' / 'worker_handlers.py'

patch(wh_py, [
    (
        "BASE = Path('/root/.openclaw/workspace/worker-manager')",
        "BASE = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent))) / 'worker-manager'",
    ),
    (
        "PROJECTS_REGISTRY = Path('/root/.openclaw/workspace/projects.json')",
        "PROJECTS_REGISTRY = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent))) / 'projects.json'",
    ),
])


# =============================================================================
# memory/es/scripts — Python utility scripts
# =============================================================================
print('Patching memory/es scripts...', flush=True)
for script_name in ('query_memory.py', 'retrieve_context.py', 'write_memory.py'):
    script = STAGING / 'memory' / 'es' / 'scripts' / script_name
    patch(script, [
        (
            "BASE = Path('/root/.openclaw/workspace/worker-manager')",
            "BASE = Path(os.environ.get('LOOM_WORKSPACE', str(Path(__file__).parent.parent.parent.parent))) / 'worker-manager'",
        ),
    ])


# =============================================================================
# dashboard/run.sh — rewrite to use workspace-relative .env
# =============================================================================
print('Writing dashboard/run.sh...', flush=True)
run_sh = STAGING / 'dashboard' / 'run.sh'
run_sh.write_text('''\
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
set -a
source "${WORKSPACE_ROOT}/.env"
export LOOM_WORKSPACE="${WORKSPACE_ROOT}"
export LOOM_FRONTEND_DIST="${WORKSPACE_ROOT}/frontend/dist"
set +a
exec python3 "${SCRIPT_DIR}/server.py"
''')
run_sh.chmod(0o755)
print('  wrote dashboard/run.sh', flush=True)


# =============================================================================
# worker-manager/run.sh — rewrite to use workspace-relative .env
# =============================================================================
print('Writing worker-manager/run.sh...', flush=True)
wm_run = STAGING / 'worker-manager' / 'run.sh'
wm_run.write_text('''\
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
set -a
source "${WORKSPACE_ROOT}/.env"
export LOOM_WORKSPACE="${WORKSPACE_ROOT}"
export WORKER_MANAGER_POLL_SECONDS="${WORKER_MANAGER_POLL_SECONDS:-15}"
set +a

# Apply git identity from .env
if [ -n "${GIT_USER_NAME:-}" ]; then
    git config --global user.name "${GIT_USER_NAME}" 2>/dev/null || true
fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then
    git config --global user.email "${GIT_USER_EMAIL}" 2>/dev/null || true
fi

exec python3 "${SCRIPT_DIR}/manager.py"
''')
wm_run.chmod(0o755)
print('  wrote worker-manager/run.sh', flush=True)


# =============================================================================
# memory/es/examples/role-wrapper-demo.sh — fix hardcoded BASE path
# =============================================================================
demo_sh = STAGING / 'memory' / 'es' / 'examples' / 'role-wrapper-demo.sh'
if demo_sh.exists():
    text = demo_sh.read_text()
    original = text
    text = re.sub(
        r'BASE=/root/[^\n]+',
        'BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"',
        text,
    )
    if text != original:
        demo_sh.write_text(text)
        print('  patched memory/es/examples/role-wrapper-demo.sh', flush=True)

# =============================================================================
# memory/es/scripts/bootstrap_memory*.sh — fix hardcoded paths (examples)
# These are utility bootstrap scripts — replace hardcoded workspace paths
# =============================================================================
for bootstrap_name in ('bootstrap_memory.sh', 'bootstrap_memory_example.sh'):
    bs = STAGING / 'memory' / 'es' / 'scripts' / bootstrap_name
    if bs.exists():
        text = bs.read_text()
        original = text
        text = re.sub(r'/root/\.openclaw/workspace', '${LOOM_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}', text)
        if text != original:
            bs.write_text(text)
            print(f'  patched memory/es/scripts/{bootstrap_name}', flush=True)

# =============================================================================
# memory/es/ROLE_WRAPPERS_USAGE.md — documentation, just strip old paths
# =============================================================================
rw_doc = STAGING / 'memory' / 'es' / 'ROLE_WRAPPERS_USAGE.md'
if rw_doc.exists():
    text = rw_doc.read_text()
    original = text
    text = text.replace('/root/.openclaw/workspace', '${LOOM_WORKSPACE}')
    if text != original:
        rw_doc.write_text(text)
        print('  patched memory/es/ROLE_WRAPPERS_USAGE.md', flush=True)


print('\nAll patches applied successfully.', flush=True)
