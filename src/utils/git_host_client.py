"""
git_host_client.py — Provider-agnostic Git remote API client for Flume.

Replaces local `git -C <path>` subprocess calls in dashboard endpoints with
REST API calls to GitHub or Azure DevOps. This eliminates the pod-local clone
dependency that was the root cause of AP-4 (K8s readiness issue #177).

Supported providers:
    GitHub          → GitHub REST API v3 (api.github.com)
    Azure DevOps    → ADO Git REST API 7.1 (dev.azure.com)

Usage:
    client = get_git_client(proj_doc)   # proj_doc is _source from flume-projects
    branches = client.get_branches()
    entries  = client.get_tree(branch="main")
    content  = client.get_file("src/foo.py", branch="main")
    diff     = client.get_diff(base="main", head="feature/task-123")
    commits  = client.get_commits(branch="feature/task-123", base="main")
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from utils.logger import get_logger

logger = get_logger("git_host_client")


# ---------------------------------------------------------------------------
# Shared exceptions
# ---------------------------------------------------------------------------

class GitHostError(Exception):
    """Base class for all GitHostClient errors."""

class GitHostAuthError(GitHostError):
    """Raised when credentials are missing or rejected (401/403)."""

class GitHostNotFoundError(GitHostError):
    """Raised when the repo, branch, or file does not exist (404)."""


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class GitHostClient(ABC):
    """Abstract interface for a remote git host."""

    @abstractmethod
    def get_branches(self) -> list[str]:
        """Return all branch names (local + remote)."""

    @abstractmethod
    def get_default_branch(self) -> str:
        """Return the repository's default branch (main / master / etc.)."""

    @abstractmethod
    def get_tree(self, branch: str = "") -> list[dict]:
        """
        Return a flat list of all blob and tree entries for the given branch.
        Each entry: {"path": str, "type": "blob"|"tree", "size": str}
        """

    @abstractmethod
    def get_file(self, path: str, branch: str = "") -> bytes:
        """
        Return the raw bytes of a file at the given path and branch.
        Raises GitHostNotFoundError if the file does not exist.
        """

    @abstractmethod
    def get_diff(self, base: str, head: str) -> dict:
        """
        Return a diff summary between two refs.
        Returns: {
            "base": str, "head": str,
            "files": [{"path": str, "insertions": int, "deletions": int}],
            "diff": str,        # unified diff text (may be empty for large diffs)
            "truncated": bool,
        }
        """

    @abstractmethod
    def get_commits(self, branch: str, base: str = "") -> list[dict]:
        """
        Return commits on `branch` not in `base`.
        Each commit: {"sha": str, "author": str, "date": str, "message": str}
        """

    def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        """
        Create a pull request. Default raises NotImplementedError;
        concrete subclasses override this when supported.
        Returns: {"pr_url": str, "pr_number": int|None}
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support create_pull_request"
        )


# ---------------------------------------------------------------------------
# Shared HTTP helper
# ---------------------------------------------------------------------------

def _http_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict | None = None,
    extra_headers: dict | None = None,
) -> Any:
    """
    Perform an authenticated HTTP request and return parsed JSON.
    Raises GitHostAuthError on 401/403, GitHostNotFoundError on 404,
    and GitHostError for other non-2xx responses.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode() if body is not None else None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode(errors="replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            logger.warning("Git API auth failure", extra={"structured_data": {"url": url, "status": e.code}})
            raise GitHostAuthError(
                f"Authentication failed ({e.code}) for {url}: {body_text}"
            ) from e
        if e.code == 404:
            logger.debug("Git API resource not found", extra={"structured_data": {"url": url}})
            raise GitHostNotFoundError(
                f"Not found ({e.code}) for {url}: {body_text}"
            ) from e
        logger.error("Git API HTTP error", extra={"structured_data": {"url": url, "status": e.code, "body": body_text}})
        raise GitHostError(
            f"HTTP {e.code} for {url}: {body_text}"
        ) from e
    except Exception as exc:
        logger.error("Git API request failed", extra={"structured_data": {"url": url, "error": str(exc)}})
        raise GitHostError(f"Request failed for {url}: {exc}") from exc


def _http_raw(url: str, *, token: str) -> bytes:
    """Return raw bytes from a URL (for file content)."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.debug("Git raw file not found", extra={"structured_data": {"url": url}})
            raise GitHostNotFoundError(f"File not found: {url}") from e
        if e.code in (401, 403):
            logger.warning("Git raw file auth failure", extra={"structured_data": {"url": url, "status": e.code}})
            raise GitHostAuthError(f"Auth failed ({e.code}): {url}") from e
        logger.error("Git raw file HTTP error", extra={"structured_data": {"url": url, "status": e.code}})
        raise GitHostError(f"HTTP {e.code}: {url}") from e
    except Exception as exc:
        logger.error("Git raw file request failed", extra={"structured_data": {"url": url, "error": str(exc)}})
        raise GitHostError(f"Request failed: {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# GitHub implementation
# ---------------------------------------------------------------------------

class GitHubClient(GitHostClient):
    """
    GitHub REST API v3 client.
    Docs: https://docs.github.com/en/rest
    """

    BASE = "https://api.github.com"

    def __init__(self, owner: str, repo: str, token: str) -> None:
        self.owner = owner
        self.repo  = repo
        self.token = token
        self._default_branch: str | None = None

    def _url(self, path: str) -> str:
        return f"{self.BASE}/repos/{self.owner}/{self.repo}/{path.lstrip('/')}"

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = self._url(path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _http_json(
            url,
            token=self.token,
            extra_headers={"X-GitHub-Api-Version": "2022-11-28"},
        )

    def _post(self, path: str, body: dict) -> Any:
        return _http_json(
            self._url(path),
            token=self.token,
            method="POST",
            body=body,
            extra_headers={"X-GitHub-Api-Version": "2022-11-28"},
        )

    def get_default_branch(self) -> str:
        if self._default_branch:
            return self._default_branch
        repo_info = self._get("")
        self._default_branch = repo_info.get("default_branch", "main")
        return self._default_branch

    def get_branches(self) -> list[str]:
        # GitHub paginates at 100 per page; fetch up to 5 pages (500 branches)
        branches: list[str] = []
        page = 1
        while True:
            data = self._get("branches", {"per_page": 100, "page": page})
            if not data:
                break
            branches.extend(b["name"] for b in data if b.get("name"))
            if len(data) < 100:
                break
            page += 1
            if page > 5:
                break
        return branches

    def get_tree(self, branch: str = "") -> list[dict]:
        if not branch:
            branch = self.get_default_branch()
        data = self._get(f"git/trees/{branch}", {"recursive": "1"})
        entries: list[dict] = []
        for item in data.get("tree", []):
            entries.append({
                "path": item.get("path", ""),
                "type": "blob" if item.get("type") == "blob" else "tree",
                "size": str(item.get("size", "-")),
            })
        return entries

    def get_file(self, path: str, branch: str = "") -> bytes:
        if not branch:
            branch = self.get_default_branch()
        clean = path.lstrip("/")
        data = self._get(f"contents/{clean}", {"ref": branch})
        # GitHub returns base64-encoded content for files < 1MB
        content_b64 = data.get("content", "")
        if not content_b64:
            # Large file — fall back to download_url (raw content)
            download_url = data.get("download_url")
            if download_url:
                return _http_raw(download_url, token=self.token)
            raise GitHostNotFoundError(f"No content returned for {path}")
        # Remove newlines GitHub inserts into the base64 block
        return base64.b64decode(content_b64.replace("\n", ""))

    def get_diff(self, base: str, head: str) -> dict:
        MAX_DIFF = 80_000
        try:
            data = self._get(
                f"compare/{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(head, safe='')}"
            )
        except GitHostNotFoundError:
            return {
                "base": base, "head": head,
                "files": [], "diff": "",
                "truncated": False, "error": f"Branch '{head}' not found on remote",
            }

        files = []
        for f in data.get("files", []):
            files.append({
                "path": f.get("filename", ""),
                "insertions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "status": f.get("status", "modified"),
            })

        # GitHub doesn't return a unified diff directly; compose from per-file patches
        diff_parts = []
        for f in data.get("files", []):
            patch = f.get("patch", "")
            if patch:
                diff_parts.append(
                    f"diff --git a/{f.get('filename')} b/{f.get('filename')}\n{patch}"
                )
        diff_text = "\n".join(diff_parts)
        truncated = False
        if len(diff_text) > MAX_DIFF:
            diff_text = diff_text[:MAX_DIFF] + "\n\n... [diff truncated at 80k chars] ..."
            truncated = True

        return {
            "base": base, "head": head,
            "files": files,
            "diff": diff_text,
            "truncated": truncated,
        }

    def get_commits(self, branch: str, base: str = "") -> list[dict]:
        if not base:
            base = self.get_default_branch()
        # Use compare to get commits on branch not in base
        try:
            data = self._get(
                f"compare/{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(branch, safe='')}"
            )
        except GitHostNotFoundError:
            return []
        commits = []
        for c in data.get("commits", [])[:50]:
            commit_obj = c.get("commit", {})
            author_obj = commit_obj.get("author", {})
            commits.append({
                "sha": c.get("sha", "")[:40],
                "author": author_obj.get("name", ""),
                "date": author_obj.get("date", ""),
                "message": commit_obj.get("message", "").split("\n")[0],
            })
        return commits

    def create_pull_request(self, title: str, body: str, head: str, base: str) -> dict:
        data = self._post("pulls", {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        })
        pr_url = data.get("html_url", "")
        pr_number = data.get("number")
        return {"pr_url": pr_url, "pr_number": pr_number}


# ---------------------------------------------------------------------------
# Azure DevOps implementation
# ---------------------------------------------------------------------------

class AzureDevOpsClient(GitHostClient):
    """
    Azure DevOps Git REST API 7.1 client.
    Docs: https://learn.microsoft.com/en-us/rest/api/azure/devops/git/
    """

    API_VERSION = "7.1-preview.1"

    def __init__(
        self,
        org: str,
        project: str,
        repo: str,
        token: str,
        base_url: str = "https://dev.azure.com",
    ) -> None:
        self.org     = org
        self.project = project
        self.repo    = repo
        self.token   = token
        self.base_url = base_url.rstrip("/")
        self._default_branch: str | None = None

    def _url(self, path: str) -> str:
        enc_project = urllib.parse.quote(self.project, safe="")
        enc_repo    = urllib.parse.quote(self.repo, safe="")
        return (
            f"{self.base_url}/{self.org}/{enc_project}/_apis"
            f"/git/repositories/{enc_repo}/{path.lstrip('/')}"
        )

    def _ado_token(self) -> str:
        # ADO uses Basic auth with a PAT (base64 ":PAT")
        raw = f":{self.token}"
        return base64.b64encode(raw.encode()).decode()

    def _get(self, path: str, params: dict | None = None) -> Any:
        p = {"api-version": self.API_VERSION}
        if params:
            p.update(params)
        url = self._url(path) + "?" + urllib.parse.urlencode(p)
        return _http_json(
            url,
            token="",  # ADO uses Basic, not Bearer; use extra_headers
            extra_headers={"Authorization": f"Basic {self._ado_token()}"},
        )

    def _post(self, path: str, body: dict, params: dict | None = None) -> Any:
        p = {"api-version": self.API_VERSION}
        if params:
            p.update(params)
        url = self._url(path) + "?" + urllib.parse.urlencode(p)
        return _http_json(
            url,
            token="",
            method="POST",
            body=body,
            extra_headers={"Authorization": f"Basic {self._ado_token()}"},
        )

    def get_default_branch(self) -> str:
        if self._default_branch:
            return self._default_branch
        data = self._get("")
        raw = data.get("defaultBranch", "refs/heads/main")
        # "refs/heads/main" → "main"
        self._default_branch = raw.replace("refs/heads/", "")
        return self._default_branch

    def get_branches(self) -> list[str]:
        data = self._get("refs", {"filter": "heads"})
        branches = []
        for ref in data.get("value", []):
            name = ref.get("name", "")
            if name.startswith("refs/heads/"):
                branches.append(name[len("refs/heads/"):])
        return branches

    def get_tree(self, branch: str = "") -> list[dict]:
        if not branch:
            branch = self.get_default_branch()
        # ADO items endpoint with recursionLevel=full
        data = self._get("items", {
            "scopePath": "/",
            "recursionLevel": "full",
            "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch",
        })
        entries: list[dict] = []
        for item in data.get("value", []):
            path = item.get("path", "").lstrip("/")
            if not path:
                continue
            is_folder = item.get("isFolder", False)
            entries.append({
                "path": path,
                "type": "tree" if is_folder else "blob",
                "size": "-",
            })
        return entries

    def get_file(self, path: str, branch: str = "") -> bytes:
        if not branch:
            branch = self.get_default_branch()
        clean = "/" + path.lstrip("/")
        data = self._get("items", {
            "path": clean,
            "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch",
            "$format": "octetStream",
        })
        # ADO returns bytes as the response body when $format=octetStream
        # but our _get parses JSON — we need raw bytes here
        # Use _http_raw with the built URL instead
        p = {
            "api-version": self.API_VERSION,
            "path": clean,
            "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch",
            "$format": "octetStream",
        }
        url = self._url("items") + "?" + urllib.parse.urlencode(p)
        return _http_raw(url, token=f"Basic {self._ado_token()}")

    def get_diff(self, base: str, head: str) -> dict:
        # ADO diffs endpoint: compare two version descriptors
        data = self._get("diffs/commits", {
            "baseVersionDescriptor.version": base,
            "baseVersionDescriptor.versionType": "branch",
            "targetVersionDescriptor.version": head,
            "targetVersionDescriptor.versionType": "branch",
        })
        files: list[dict] = []
        for change in data.get("changes", []):
            item = change.get("item", {})
            change_type = change.get("changeType", "edit")
            files.append({
                "path": item.get("path", "").lstrip("/"),
                "insertions": 0,   # ADO diff endpoint doesn't provide line counts
                "deletions": 0,
                "status": change_type,
            })
        # ADO does not provide a unified diff via its REST API without
        # per-file fetching; return an empty diff text with the file list
        return {
            "base": base, "head": head,
            "files": files, "diff": "",
            "truncated": False,
        }

    def get_commits(self, branch: str, base: str = "") -> list[dict]:
        if not base:
            base = self.get_default_branch()
        # ADO commits search with itemVersion and compareVersion
        data = self._get("commits", {
            "searchCriteria.itemVersion.version": branch,
            "searchCriteria.itemVersion.versionType": "branch",
            "searchCriteria.compareVersion.version": base,
            "searchCriteria.compareVersion.versionType": "branch",
            "searchCriteria.$top": "50",
        })
        commits: list[dict] = []
        for c in data.get("value", []):
            author = c.get("author", {})
            commits.append({
                "sha": c.get("commitId", "")[:40],
                "author": author.get("name", ""),
                "date": author.get("date", ""),
                "message": c.get("comment", "").split("\n")[0],
            })
        return commits

    def create_pull_request(self, title: str, body: str, head: str, base: str) -> dict:
        data = self._post("pullrequests", {
            "title": title,
            "description": body,
            "sourceRefName": f"refs/heads/{head}",
            "targetRefName": f"refs/heads/{base}",
        })
        pr_id  = data.get("pullRequestId")
        pr_url = data.get("url", "")
        # Construct a browser-friendly URL
        if pr_id:
            enc_project = urllib.parse.quote(self.project, safe="")
            enc_repo    = urllib.parse.quote(self.repo, safe="")
            pr_url = (
                f"https://dev.azure.com/{self.org}/{enc_project}/"
                f"_git/{enc_repo}/pullrequest/{pr_id}"
            )
        return {"pr_url": pr_url, "pr_number": pr_id}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _parse_github_owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub HTTPS or SSH URL."""
    import re
    # HTTPS: https://github.com/owner/repo[.git]
    m = re.match(
        r"https?://(?:[^@]*@)?github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$",
        repo_url.strip(),
    )
    if m:
        return m.group(1), m.group(2)
    # SSH: git@github.com:owner/repo[.git]
    m = re.match(r"git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?$", repo_url.strip())
    if m:
        return m.group(1), m.group(2)
    return None


def _parse_ado_components(repo_url: str) -> tuple[str, str, str] | None:
    """
    Extract (org, project, repo) from an Azure DevOps URL.

    Handles all common ADO HTTPS clone URL formats:
      • https://dev.azure.com/<org>/<project>/_git/<repo>
      • https://<user>@dev.azure.com/<org>/<project>/_git/<repo>   ← PAT clone URL
      • https://<org>.visualstudio.com/<project>/_git/<repo>
      • https://<org>.visualstudio.com/DefaultCollection/<project>/_git/<repo>
    """
    import re

    url = repo_url.strip()

    # Normalise: strip trailing .git and trailing slash
    url = re.sub(r"\.git$", "", url).rstrip("/")

    # Strip embedded username/PAT before the host: https://user@host → https://host
    # This covers: https://mentat-automation@dev.azure.com/...
    url = re.sub(r"(https?://)([^@]+@)", r"\1", url)

    # Pattern 1: https://dev.azure.com/<org>/<project>/_git/<repo>
    m = re.match(
        r"https?://dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?]+)",
        url,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)

    # Pattern 2: https://<org>.visualstudio.com/DefaultCollection/<project>/_git/<repo>
    m = re.match(
        r"https?://([^.]+)\.visualstudio\.com/DefaultCollection/([^/]+)/_git/([^/?]+)",
        url,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)

    # Pattern 3: https://<org>.visualstudio.com/<project>/_git/<repo>
    m = re.match(
        r"https?://([^.]+)\.visualstudio\.com/([^/]+)/_git/([^/?]+)",
        url,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)

    return None


def _get_github_token() -> str:
    """Resolve a GitHub PAT from environment variables."""
    return (
        os.environ.get("GH_TOKEN", "")
        or os.environ.get("GITHUB_TOKEN", "")
    ).strip()


def _get_ado_token() -> str:
    """Resolve an ADO PAT from environment variables."""
    return (
        os.environ.get("ADO_TOKEN", "")
        or os.environ.get("ADO_PERSONAL_ACCESS_TOKEN", "")
    ).strip()


def get_git_client(proj: dict) -> GitHostClient:
    """
    Factory: return the appropriate GitHostClient for a project document
    (as stored in the `flume-projects` Elasticsearch index).

    ``proj`` must contain at least ``repoUrl``.

    Raises GitHostError if the repo URL cannot be parsed or no credentials
    are available. The caller is responsible for handling these.
    """
    from utils.git_credentials import detect_repo_type  # noqa: PLC0415

    repo_url   = (proj.get("repoUrl") or proj.get("repo_url") or "").strip()
    repo_type  = detect_repo_type(repo_url)

    if repo_type == "github":
        parsed = _parse_github_owner_repo(repo_url)
        if not parsed:
            raise GitHostError(f"Cannot parse GitHub owner/repo from: {repo_url}")
        owner, repo = parsed

        # Prefer tokens loaded by the token store (OpenBao-backed) if available
        token = ""
        try:
            import sys
            from pathlib import Path
            _ws_root = Path(__file__).resolve().parent.parent
            if str(_ws_root) not in sys.path:
                sys.path.insert(0, str(_ws_root))
            import github_tokens_store  # noqa: PLC0415
            from utils.workspace import resolve_safe_workspace  # noqa: PLC0415
            ws = resolve_safe_workspace()
            raw = github_tokens_store.get_active_token_plain(ws)
            if raw and "OPENBAO_DELEGATED" not in raw:
                token = raw
        except Exception as e:
            logger.warning(
                "GitHub token resolution from OpenBao failed — falling back to env",
                extra={"structured_data": {"error": str(e)}},
            )
        if not token:
            token = _get_github_token()
        if not token:
            raise GitHostAuthError(
                "No GitHub PAT configured. Add a token in Settings → Repositories."
            )
        return GitHubClient(owner=owner, repo=repo, token=token)

    if repo_type == "ado":
        parsed_ado = _parse_ado_components(repo_url)
        if not parsed_ado:
            raise GitHostError(f"Cannot parse ADO org/project/repo from: {repo_url}")
        org, project, repo = parsed_ado

        token = ""
        try:
            import sys
            from pathlib import Path
            _ws_root = Path(__file__).resolve().parent.parent
            if str(_ws_root) not in sys.path:
                sys.path.insert(0, str(_ws_root))
            import ado_tokens_store  # noqa: PLC0415
            from utils.workspace import resolve_safe_workspace  # noqa: PLC0415
            ws = resolve_safe_workspace()
            raw = ado_tokens_store.get_active_token_plain(ws)
            if raw and "OPENBAO_DELEGATED" not in raw:
                token = raw
        except Exception as e:
            logger.warning(
                "ADO token resolution from OpenBao failed — falling back to env",
                extra={"structured_data": {"error": str(e)}},
            )
        if not token:
            token = _get_ado_token()
        if not token:
            raise GitHostAuthError(
                "No Azure DevOps PAT configured. Add a token in Settings → Repositories."
            )
        return AzureDevOpsClient(org=org, project=project, repo=repo, token=token)

    raise GitHostError(
        f"No GitHostClient implementation for provider type '{repo_type}' "
        f"(URL: {repo_url}). Supported: github, ado."
    )
