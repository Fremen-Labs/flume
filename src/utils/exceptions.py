"""Flume-specific exception hierarchy.

Provides typed exceptions for git, Elasticsearch, and worker operations
so callers can catch specific failures instead of broad ``Exception``.

Usage::

    from utils.exceptions import GitOperationError

    try:
        rc, out, err = await run_cmd_async("git", "diff", ...)
        if rc != 0:
            raise GitOperationError("diff", stderr=err, returncode=rc)
    except GitOperationError as e:
        logger.warning({"event": "git_diff_failed", "error": str(e)})
"""

from utils.logger import get_logger

logger = get_logger(__name__)


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

