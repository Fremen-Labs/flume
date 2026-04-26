from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pathlib import Path

from utils.logger import get_logger
from utils.url_helpers import is_remote_url
from utils.async_subprocess import run_cmd_async

from core.projects_store import load_projects_registry
from core.tasks import resolve_default_branch

logger = get_logger(__name__)
router = APIRouter()

@router.get("/api/repos/{project_id}/branches")
async def api_repo_branches(project_id: str):
    """
    Return git branches for a project.

    AP-4B: For remote repos (clone_status in ['indexed', 'cloned']) this calls
    the GitHostClient REST API and requires no local clone.
    For locally-mounted repos (clone_status='local') the original git subprocess
    path is used.

    When the repo is not yet available (still cloning/indexing), the response
    includes `cloneStatus` so the frontend can start the polling loop without
    an extra round-trip to /clone-status.
    """
    from utils.git_host_client import get_git_client, GitHostAuthError, GitHostError  # noqa

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    cs = proj.get("clone_status", "no_repo")
    clone_error = proj.get("clone_error")

    # In-flight states: clone/ingest still running — send polling hint to UI
    if cs in ("cloning", "indexing", "pending"):
        return {
            "gitAvailable": False,
            "cloneStatus": cs,
            "cloneError": None,
            "branches": [],
            "message": "Repository is being cloned in the background…",
        }

    # ── Local clone precedence check ──────────────────────────────────────────
    _local_path = proj.get("path") or ''
    has_local_clone = _local_path and Path(_local_path).joinpath('.git').exists()

    # ── Remote repo path: use GitHostClient REST API (no local clone available) ──
    repo_url = proj.get("repoUrl") or ""
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and is_remote_url(repo_url):
        try:
            client = get_git_client(proj)
            branches = client.get_branches()
            default  = client.get_default_branch()
            return {"gitAvailable": True, "branches": branches, "default": default}
        except GitHostAuthError as e:
            return JSONResponse(status_code=401, content={
                "gitAvailable": False,
                "error": "No credentials configured. Add a PAT in Settings → Repositories.",
                "detail": str(e)[:200],
            })
        except GitHostError as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})

    # ── Local repo path: original git subprocess (clone_status='local') ─────────
    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback for non-local projects.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path available. This project has no local clone."})
    repo_path = Path(_local_path)

    if not (repo_path / ".git").exists():
        if cs == "failed":
            message = f"Clone failed: {clone_error or 'Unknown error'}"
        else:
            message = (
                "This project is not a Git repository. "
                'Add one by creating the project with a clone URL or run "git init" in the project folder.'
            )
        return {
            "gitAvailable": False,
            "cloneStatus": cs,
            "cloneError": clone_error,
            "branches": [],
            "message": message,
        }

    try:
        rc, raw, err = await run_cmd_async(
            "git", "-C", str(repo_path), "branch", "-a", "--format=%(refname:short)",
            timeout=10,
        )
        if rc == 128:
            return JSONResponse(status_code=500, content={"error": "git branch exited 128: Repository refs may be corrupt."})
        if rc != 0:
            return JSONResponse(status_code=500, content={"error": f"git branch failed: {err}"})
        all_branches = [b.strip() for b in raw.splitlines() if b.strip()]
        seen: set = set()
        branches: list = []
        for b in all_branches:
            name = b.removeprefix("origin/") if b.startswith("origin/") else b
            if name and name != "HEAD" and name not in seen:
                seen.add(name)
                branches.append(name)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"git branch failed: {exc}"})

    default = await resolve_default_branch(
        repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
    )
    return {"gitAvailable": True, "branches": branches, "default": default}


@router.get("/api/repos/{project_id}/tree")
async def api_repo_tree(project_id: str, branch: str = ""):
    """
    Return a flat list of all git-tracked files/dirs for a given branch.
    AP-4B: Uses GitHostClient REST API for remote repos (no local clone required).
    """
    from utils.git_host_client import get_git_client, GitHostAuthError, GitHostError  # noqa

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    cs = proj.get("clone_status", "no_repo")
    repo_url = proj.get("repoUrl") or ""

    if cs in ("cloning", "indexing", "pending"):
        return JSONResponse(status_code=400, content={"error": "Repository is currently being cloned."})

    # ── Local clone precedence check ──────────────────────────────────────────
    _local_path = proj.get("path") or ''
    has_local_clone = _local_path and Path(_local_path).joinpath('.git').exists()

    # ── Remote repo: GitHostClient REST API ──────────────────────────────────
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and is_remote_url(repo_url):
        try:
            client = get_git_client(proj)
            if not branch:
                branch = client.get_default_branch()
            entries = client.get_tree(branch=branch)
            return {"branch": branch, "entries": entries}
        except GitHostAuthError as e:
            return JSONResponse(status_code=401, content={
                "error": "No credentials — add a PAT in Settings → Repositories.",
                "detail": str(e)[:200],
            })
        except GitHostError as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})

    # ── Local repo: git subprocess ────────────────────────────────────────────
    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path. This project has no local clone."})
    repo_path = Path(_local_path)
    if not (repo_path / ".git").exists():
        return JSONResponse(status_code=400, content={"error": "Not a git repository"})

    if not branch:
        branch = await resolve_default_branch(
            repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
        )

    try:
        rc, raw, err = await run_cmd_async(
            "git", "-C", str(repo_path), "ls-tree", "-r", "--long", "--full-tree", branch,
            timeout=30,
        )
        if rc != 0:
            return JSONResponse(status_code=400, content={"error": f"Could not read tree for branch '{branch}': {err}"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"git ls-tree failed: {exc}"})

    entries = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        meta = parts[0]
        meta_parts = meta.split()
        if len(meta_parts) < 3:
            continue
        obj_type = meta_parts[1]
        if len(parts) == 3:
            size = parts[1].strip()
            file_path = parts[2].strip()
        else:
            size = "-"
            file_path = parts[1].strip()
        entries.append({"path": file_path, "type": "blob" if obj_type == "blob" else "tree", "size": size})

    dirs_seen: set = set()
    dir_entries = []
    for e in entries:
        parts_path = e["path"].split("/")
        for depth in range(1, len(parts_path)):
            dir_path = "/".join(parts_path[:depth])
            if dir_path not in dirs_seen:
                dirs_seen.add(dir_path)
                dir_entries.append({"path": dir_path, "type": "tree", "size": "-"})

    return {"branch": branch, "entries": entries + dir_entries}


@router.get("/api/repos/{project_id}/file")
async def api_repo_file(project_id: str, path: str = "", branch: str = ""):
    """
    Return the content of a single file from the git tree.
    AP-4B: Uses GitHostClient REST API for remote repos (no local clone required).
    """
    from utils.git_host_client import get_git_client, GitHostAuthError, GitHostNotFoundError, GitHostError  # noqa

    if not path:
        return JSONResponse(status_code=400, content={"error": "path is required"})

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    cs = proj.get("clone_status", "no_repo")
    repo_url = proj.get("repoUrl") or ""

    # Sanitise path — prevent directory traversal
    clean_path = path.lstrip("/")
    if ".." in clean_path.split("/"):
        return JSONResponse(status_code=400, content={"error": "Invalid path"})

    # ── Local clone precedence check ──────────────────────────────────────────
    _local_path = proj.get("path") or ''
    has_local_clone = _local_path and Path(_local_path).joinpath('.git').exists()

    # ── Remote repo: GitHostClient REST API ──────────────────────────────────
    if not has_local_clone and cs in ("indexed", "cloned") and repo_url and is_remote_url(repo_url):
        try:
            client = get_git_client(proj)
            if not branch:
                branch = client.get_default_branch()
            content_bytes = client.get_file(clean_path, branch=branch)
            return _make_file_response(content_bytes, clean_path)
        except GitHostAuthError as e:
            return JSONResponse(status_code=401, content={
                "error": "No credentials — add a PAT in Settings → Repositories.",
                "detail": str(e)[:200],
            })
        except GitHostNotFoundError:
            return JSONResponse(status_code=404, content={"error": f"File '{clean_path}' not found on branch '{branch}'"})
        except GitHostError as e:
            return JSONResponse(status_code=500, content={"error": str(e)[:300]})

    # ── Local repo: git subprocess ────────────────────────────────────────────
    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path. This project has no local clone."})
    repo_path = Path(_local_path)
    if not (repo_path / ".git").exists():
        return JSONResponse(status_code=400, content={"error": "Not a git repository"})

    if not branch:
        branch = await resolve_default_branch(
            repo_path, override=proj.get("gitflow", {}).get("defaultBranch")
        )

    try:
        rc, out, err = await run_cmd_async(
            "git", "-C", str(repo_path), "show", f"{branch}:{clean_path}",
            timeout=15,
        )
        if rc != 0:
            if "does not exist" in err or "exists on disk" in err or rc == 128:
                return JSONResponse(status_code=404, content={"error": f"File '{clean_path}' not found on branch '{branch}'"})
            return JSONResponse(status_code=500, content={"error": f"git show failed: {err}"})
        content_bytes = out.encode("utf-8")
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"git show failed: {exc}"})

    # Detect binary by sniffing for null bytes in the first 8KB
    sample = content_bytes[:8192]
    is_binary = b"\x00" in sample
    if is_binary:
        return {"binary": True, "content": None, "size": len(content_bytes)}

    return {
        "binary": False,
        "content": content_bytes.decode("utf-8", errors="replace"),
        "size": len(content_bytes),
    }


def _make_file_response(content_bytes: bytes, path: str) -> dict:
    """Shared file response formatter for both git subprocess and GitHostClient paths."""
    sample = content_bytes[:8192]
    is_binary = b"\x00" in sample
    if is_binary:
        return {"binary": True, "content": None, "size": len(content_bytes)}
    return {
        "binary": False,
        "content": content_bytes.decode("utf-8", errors="replace"),
        "size": len(content_bytes),
    }


@router.get("/api/repos/{project_id}/diff")
async def api_repo_diff(project_id: str, base: str = "", head: str = ""):
    """Return a unified diff between two branches for a project."""
    if not base or not head:
        return JSONResponse(status_code=400, content={"error": "base and head branch parameters are required"})

    registry = load_projects_registry()
    proj = next((p for p in registry if p.get("id") == project_id), None)
    if not proj:
        return JSONResponse(status_code=404, content={"error": f"Project '{project_id}' not found"})

    # AP-12: Explicit guard — no silent WORKSPACE_ROOT fallback.
    _local_path = proj.get("path") or ''
    if not _local_path:
        return JSONResponse(status_code=400, content={"error": "No local repo path. Diff requires a locally-mounted repo."})
    repo_path = Path(_local_path)
    if not (repo_path / ".git").exists():
        return JSONResponse(status_code=400, content={"error": "Not a git repository"})

    if base == head:
        return {"base": base, "head": head, "files": [], "diff": "", "truncated": False, "identical": True}

    MAX_DIFF_LINES = 3000
    ref = f"{base}...{head}"

    # Best-effort fetch (non-blocking)
    try:
        await run_cmd_async("git", "-C", str(repo_path), "fetch", "origin", "--quiet", timeout=10)
    except Exception as _e:
        logger.debug("api_repo_diff: fetch failed (best-effort)", exc_info=True)

    files = []
    try:
        rc, stat_raw, stat_err = await run_cmd_async(
            "git", "-C", str(repo_path), "diff", "--stat", "--stat-width=1000", ref,
            timeout=15,
        )
        if rc == 0:
            for line in stat_raw.splitlines():
                parts = line.strip().split("|")
                if len(parts) != 2:
                    continue
                path_part = parts[0].strip()
                change_part = parts[1].strip()
                if not path_part or path_part.startswith("changed"):
                    continue
                ins = sum(1 for c in change_part if c == "+")
                dels = sum(1 for c in change_part if c == "-")
                files.append({"path": path_part, "insertions": ins, "deletions": dels, "status": "modified"})
    except Exception as _e:
        logger.debug("api_repo_diff: diff --stat failed (best-effort)", exc_info=True)

    diff_text = ""
    truncated = False
    try:
        rc, raw_diff, diff_err = await run_cmd_async(
            "git", "-C", str(repo_path), "diff", ref,
            timeout=30,
        )
        if rc == 0:
            diff_lines = raw_diff.splitlines()
            if len(diff_lines) > MAX_DIFF_LINES:
                diff_text = "\n".join(diff_lines[:MAX_DIFF_LINES])
                truncated = True
            else:
                diff_text = raw_diff
    except Exception as _e:
        logger.debug("api_repo_diff: git diff failed (best-effort)", exc_info=True)

    identical = not diff_text.strip() and not files
    return {
        "base": base, "head": head,
        "files": files, "diff": diff_text,
        "truncated": truncated, "identical": identical,
    }
