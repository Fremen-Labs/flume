"""Flume-specific exception hierarchy.

Provides typed exceptions for git, Elasticsearch, and worker operations
so callers can catch specific failures instead of broad ``Exception``.

Usage::

    from utils.exceptions import GitOperationError, SAFE_EXCEPTIONS

    try:
        rc, out, err = await run_cmd_async("git", "diff", ...)
        if rc != 0:
            raise GitOperationError("diff", stderr=err, returncode=rc)
    except GitOperationError as e:
        logger.warning({"event": "git_diff_failed", "error": str(e)})
"""

import urllib.error

from utils.logger import get_logger

logger = get_logger(__name__)

# ── Centralised safe-exception tuple ─────────────────────────────────────────
#
# Previously copy-pasted 139 times across the dashboard as:
#   except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError)
#
# This constant ADDS ``urllib.error.HTTPError`` (a subclass of URLError) so
# that HTTP 401 / 403 / 500 responses from Elasticsearch or the Gateway are
# caught instead of propagating as unhandled exceptions.
#
# Import and use as:
#   from utils.exceptions import SAFE_EXCEPTIONS
#   except SAFE_EXCEPTIONS as e: ...
SAFE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ValueError,
    KeyError,
    TypeError,
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
)


class FlumeError(Exception):
    """Base for all Flume-specific exceptions."""


class GitOperationError(FlumeError):
    """A git subprocess command failed (clone, checkout, diff, etc.)."""

    def __init__(self, operation: str, stderr: str = "", returncode: int = -1):
        self.operation = operation
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"git {operation} failed (rc={returncode}): {stderr[:300]}")


class ElasticsearchQueryError(FlumeError):
    """An Elasticsearch query or index operation failed."""


class WorkerHeartbeatError(FlumeError):
    """Worker heartbeat timestamp parsing or staleness detection failed."""


