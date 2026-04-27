import os
import json
import asyncio
import subprocess
import traceback
import httpx
# ruff: noqa: E402
from pathlib import Path

from utils.logger import get_logger
from utils.workspace import resolve_safe_workspace
from core.projects_store import _update_project_registry_field

logger = get_logger(__name__)
WORKSPACE_ROOT = resolve_safe_workspace()

from core.elasticsearch import ES_URL, _get_auth_headers

async def _check_ast_exists_natively(http_client: httpx.AsyncClient, repo_path: str) -> tuple[bool, str]:
    try:
        es_url = ES_URL
        headers = {'Content-Type': 'application/json'}
        headers.update(_get_auth_headers())

        elastro_index = os.environ.get("FLUME_ELASTRO_INDEX", "flume-elastro-graph")
        query = {"query": {"match": {"file_path": repo_path}}, "size": 1}
        
        response = await http_client.post(f"{es_url}/{elastro_index}/_search", json=query, headers=headers, timeout=5.0)
        response.raise_for_status()
        
        data = response.json()
        exists = data.get('hits', {}).get('total', {}).get('value', 0) > 0
        return exists, ("Found mapping records" if exists else "No logical paths matched")
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError) as e:
        logger.error(json.dumps({"event": "ast_existence_check_failure", "repo": repo_path, "error": str(e)}))
        return False, str(e)


async def _deterministic_ast_ingest(http_client: httpx.AsyncClient, repo_path: str, project_id: str, project_name: str) -> bool:
    try:
        # Sanitize remote Git URLs into guaranteed physical volume paths via basename isolation
        local_path = repo_path
        if repo_path.startswith('http') or repo_path.startswith('git@'):
            import urllib.parse
            parsed = urllib.parse.urlparse(repo_path)
            basename = os.path.basename(parsed.path).replace('.git', '')
            local_path = str(WORKSPACE_ROOT / basename)

        exists, details = await _check_ast_exists_natively(http_client, local_path)
            
        if not exists:
            logger.info(json.dumps({"event": "ast_ingest_start", "repo": local_path, "project": project_name}))
            elastro_index = os.environ.get("FLUME_ELASTRO_INDEX", "flume-elastro-graph")
            # Use the venv binary directly — avoids uv run re-installing elastro
            # on every call and works reliably inside the non-interactive container.
            elastro_bin = Path("/opt/venv/bin/elastro")
            if not elastro_bin.exists():
                import shutil
                resolved = shutil.which("elastro")
                if resolved:
                    elastro_bin = Path(resolved)
                else:
                    logger.warning(json.dumps({
                        "event": "ast_ingest_skipped",
                        "repo": local_path,
                        "project": project_name,
                        "reason": "elastro_not_installed",
                        "hint": "elastro>=0.2.0 is now in pyproject.toml — rebuild the Docker image to enable AST ingestion.",
                    }))
                    return False
            
            # Pass ES connection env vars so elastro targets the cluster
            # instead of defaulting to localhost:9200 inside the container.
            # Elastro reads ELASTIC_URL / ELASTIC_HOST (see elastro/config/defaults.py)
            # and auth via ELASTIC_ELASTICSEARCH_AUTH_API_KEY (see elastro/config/loader.py).
            # Source these from the same ES_URL / ES_API_KEY that OpenBao hydrated
            # into os.environ at startup — matching the clone process secret path.
            elastro_env = os.environ.copy()
            resolved_es_url = ES_URL or os.environ.get("ES_URL", "http://elasticsearch:9200")
            resolved_api_key = os.environ.get("ES_API_KEY", "")
            
            # Elastro native env vars (elastro/config/defaults.py reads ELASTIC_URL)
            elastro_env["ELASTIC_URL"] = resolved_es_url
            elastro_env["ELASTIC_ELASTICSEARCH_HOSTS"] = resolved_es_url
            
            # Also set the decomposed host/port/protocol for full compatibility
            from urllib.parse import urlparse
            _parsed = urlparse(resolved_es_url)
            elastro_env["ELASTIC_HOST"] = _parsed.hostname or "elasticsearch"
            elastro_env["ELASTIC_PORT"] = str(_parsed.port or 9200)
            elastro_env["ELASTIC_PROTOCOL"] = _parsed.scheme or "http"
            
            # Auth: elastro config loader reads ELASTIC_ELASTICSEARCH_AUTH_API_KEY
            if resolved_api_key:
                elastro_env["ELASTIC_ELASTICSEARCH_AUTH_API_KEY"] = resolved_api_key
                elastro_env["ELASTIC_ELASTICSEARCH_AUTH_TYPE"] = "api_key"
                
            logger.info(json.dumps({"event": "ast_ingest_env", "elastic_url": resolved_es_url, "has_api_key": bool(resolved_api_key)}))
            
            proc = await asyncio.create_subprocess_exec(
                str(elastro_bin), "rag", "ingest", local_path, "-i", elastro_index,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=elastro_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, "elastro", stdout, stderr)
                
            logger.info(json.dumps({"event": "ast_ingest_success", "repo": local_path, "project": project_name}))
            return True

        else:
            logger.info(json.dumps({"event": "ast_ingest_skipped", "repo": local_path, "project": project_name, "reason": "already_indexed"}))
            return True

    except subprocess.CalledProcessError as e:
        logger.error(json.dumps({
            "event": "ast_ingest_failure", 
            "repo": repo_path, 
            "error": "subprocess_error",
            "stderr": e.stderr.decode('utf-8', errors='replace') if e.stderr else "",
            "stdout": e.stdout.decode('utf-8', errors='replace') if e.stdout else ""
        }))
        return False
    except (asyncio.TimeoutError, ValueError, OSError, RuntimeError) as e:
        logger.error(json.dumps({"event": "ast_ingest_failure", "repo": repo_path, "error": str(e), "traceback": traceback.format_exc()}))
        return False

async def _clone_and_setup_project(
    http_client: httpx.AsyncClient,
    project_id: str,
    project_name: str,
    repo_url: str,
    dest_path: Path,
) -> None:
    """
    Background task: clone a remote git repository into dest_path, run AST
    ingestion, then DELETE the local clone and mark clone_status='indexed'.

    AP-4B: After this function completes the project has no persistent local
    clone — all browse/diff/branch data is served via the GitHostClient REST
    API. The local path is only needed for the AST ingest run.
    """
    logger.info(json.dumps({
        "event": "project_clone_start",
        "project_id": project_id,
        "repo_url": repo_url,
        "dest": str(dest_path),
    }))

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Pre-clone directory triage ───────────────────────────────────────
        # The /workspace bind-mount persists across `flume destroy`, so a
        # previous failed/aborted clone can leave a partial .git directory.
        # git refuses to clone into such a dir with:
        #   BUG: refs/files-backend.c: initial ref transaction called with existing refs
        #
        # Strategy:
        #  • Complete clone  (.git/HEAD exists AND packed-refs or refs/heads)
        #    → skip git clone, proceed directly to AST ingestion
        #  • Partial .git  (HEAD missing OR refs empty)
        #    → wipe dest_path, proceed with clean clone
        #  • Directory exists but no .git
        #    → wipe dest_path, proceed with clean clone
        if dest_path.exists():
            git_dir = dest_path / '.git'
            head_file = git_dir / 'HEAD'
            refs_dir = git_dir / 'refs' / 'heads'
            packed_refs = git_dir / 'packed-refs'
            is_complete = (
                git_dir.exists()
                and head_file.exists()
                and (
                    (refs_dir.exists() and any(refs_dir.iterdir()))
                    or packed_refs.exists()
                )
            )
            if is_complete:
                logger.info(json.dumps({
                    "event": "project_clone_skip",
                    "project_id": project_id,
                    "reason": "already_cloned_ast_ingest_only",
                }))
                # Fall through to AST ingestion below
            else:
                # Partial or broken state — wipe and start fresh.
                import shutil as _shutil
                logger.warning(json.dumps({
                    "event": "project_clone_stale_dir_removed",
                    "project_id": project_id,
                    "dest": str(dest_path),
                    "reason": "partial_or_broken_git_dir",
                }))
                _shutil.rmtree(dest_path, ignore_errors=True)

        if not dest_path.exists():
            proc = await asyncio.create_subprocess_exec(
                'git', 'clone', '--', repo_url, str(dest_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError('git clone timed out after 300 seconds')

            if proc.returncode != 0:
                err_msg = stderr.decode('utf-8', errors='replace').strip()
                raise RuntimeError(f'git clone exited {proc.returncode}: {err_msg[:400]}')

            logger.info(json.dumps({
                "event": "project_clone_success",
                "project_id": project_id,
                "dest": str(dest_path),
            }))

        # ── AST ingestion (inline — local clone available at this point) ─────
        _update_project_registry_field(
            project_id,
            path=str(dest_path),
            clone_status='indexing',
            clone_error=None,
        )
        ast_ok = await _deterministic_ast_ingest(http_client, str(dest_path), project_id, project_name)

        # ── AP-4B: Delete local clone after ingestion ─────────────────────────
        # The local clone is no longer needed — browse/diff/branch data is
        # served via the GitHostClient REST API henceforth.
        import shutil as _shutil2
        _shutil2.rmtree(dest_path, ignore_errors=True)
        logger.info(json.dumps({
            "event": "project_clone_deleted_post_ingest",
            "project_id": project_id,
            "dest": str(dest_path),
            "reason": "AP-4B: ephemeral clone — local path not retained after AST ingest",
        }))

        _update_project_registry_field(
            project_id,
            path=None,           # No persistent local path retained
            clone_status='indexed' if ast_ok else 'ast_failed',
            clone_error=None if ast_ok else 'AST ingestion failed — check dashboard logs',
            ast_indexed=ast_ok,  # Workers check this at task-claim time
        )

    except (asyncio.TimeoutError, RuntimeError, OSError, ValueError) as exc:
        err_str = str(exc)[:500]
        logger.error(json.dumps({
            "event": "project_clone_failure",
            "project_id": project_id,
            "error": err_str,
        }))
        _update_project_registry_field(
            project_id,
            clone_status='failed',
            clone_error=err_str,
        )
