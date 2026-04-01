"""
git_credentials.py — Shared Git credential embedding utility for Flume.

Provides a provider-agnostic adapter for embedding PATs into HTTPS remote URLs
before clone/push operations. Uses the x-access-token format uniformly across
all providers for maximum security and compatibility.

Future providers: extend _PROVIDER_ADAPTERS with a new key and URL rewrite rule.
"""
import os
import re
import urllib.parse
from pathlib import Path


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def detect_repo_type(repo_url: str) -> str:
    """
    Classify a repository URL or path into a provider type string.

    Returns one of: 'local', 'github', 'ado', 'generic_https', 'ssh'
    """
    if not repo_url:
        return "local"

    stripped = repo_url.strip()

    # Absolute local filesystem path
    if stripped.startswith("/") or stripped.startswith("./") or stripped.startswith("~"):
        return "local"
    # Windows-style absolute path (rare inside Docker, but defensive)
    if len(stripped) > 1 and stripped[1] == ":":
        return "local"
    # Existing local directory (no scheme)
    if not ("://" in stripped or stripped.startswith("git@")):
        candidate = Path(stripped).expanduser()
        if candidate.exists():
            return "local"

    # SSH remotes — cannot embed creds; caller must handle via SSH key
    if stripped.startswith("git@") or stripped.startswith("ssh://"):
        return "ssh"

    lower = stripped.lower()
    if "github.com" in lower:
        return "github"
    if "dev.azure.com" in lower or "visualstudio.com" in lower:
        return "ado"
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return "generic_https"

    return "local"


# ---------------------------------------------------------------------------
# Credential embedding
# ---------------------------------------------------------------------------

def embed_credentials(repo_url: str, repo_type: str | None = None) -> str:
    """
    Rewrite an HTTPS remote URL to embed a PAT using the x-access-token format.

    Security properties:
      - x-access-token:<PAT> is the recommended format for GitHub Actions,
        Azure DevOps service connections, and deploy keys. It avoids embedding
        a plaintext username and uses a dedicated token prefix that providers
        recognise for rotation/revocation.
      - PAT values are percent-encoded via urllib.parse.quote(safe='') to
        handle special characters without breaking URL parsing.
      - Any existing userinfo (including ADO org usernames like
        'mentat-automation@dev.azure.com') is stripped before embedding, so
        org-name-prefixed clone URLs received from Azure DevOps are handled
        correctly without triggering an interactive password prompt.
      - SSH remotes are returned unchanged (credentials must be injected via
        SSH agent or mounted key — out of scope for this utility).
      - If the required env var for a provider is absent the original URL is
        returned unchanged; the caller should handle authentication failure.

    Supported providers (extend _resolve_token() to add more):
        github        → GH_TOKEN / GITHUB_TOKEN env var
        ado           → ADO_TOKEN / ADO_PERSONAL_ACCESS_TOKEN env var
        generic_https → no embedding (pass-through)
        local / ssh   → pass-through
    """
    if repo_type is None:
        repo_type = detect_repo_type(repo_url)

    if repo_type in ("local", "ssh", "generic_https"):
        return repo_url

    token = _resolve_token(repo_type)
    if not token:
        return repo_url

    # Always strip any existing userinfo (including bare org usernames like
    # 'mentat-automation@') before embedding the real PAT so we never end up
    # with a URL that has a username but no password, which causes git to
    # prompt interactively and fail inside Docker.
    return _rewrite_url(strip_credentials(repo_url), token)


def strip_credentials(repo_url: str) -> str:
    """
    Remove any embedded userinfo (credentials) from an HTTPS URL.
    Safe to call on clean URLs — returns them unchanged.
    """
    try:
        parsed = urllib.parse.urlparse(repo_url)
        clean = parsed._replace(netloc=parsed.hostname + (f":{parsed.port}" if parsed.port else ""))
        return urllib.parse.urlunparse(clean)
    except Exception:
        return repo_url


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _has_credentials(url: str) -> bool:
    """Return True if the URL already contains a username/password segment."""
    try:
        parsed = urllib.parse.urlparse(url)
        return bool(parsed.username or parsed.password)
    except Exception:
        return False


def _resolve_token(repo_type: str) -> str:
    """Return the PAT for a given provider type from environment variables."""
    if repo_type == "github":
        return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if repo_type == "ado":
        return (
            os.environ.get("ADO_TOKEN")
            or os.environ.get("ADO_PERSONAL_ACCESS_TOKEN")
            or ""
        )
    return ""


def _rewrite_url(repo_url: str, token: str) -> str:
    """
    Insert x-access-token:<encoded_token>@ into the HTTPS URL after the scheme.

    Input:  https://github.com/org/repo.git
    Output: https://x-access-token:<token>@github.com/org/repo.git

    Input:  https://mentat-automation@dev.azure.com/org/project/_git/repo
    Output: https://x-access-token:<token>@dev.azure.com/org/project/_git/repo
            (existing username segment is stripped before re-embedding)
    """
    encoded = urllib.parse.quote(token, safe="")
    # Strip any existing userinfo first
    url = strip_credentials(repo_url)
    # Insert credentials after scheme://
    return re.sub(
        r"^(https?://)",
        rf"\1x-access-token:{encoded}@",
        url,
        count=1,
    )
