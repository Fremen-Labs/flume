"""
Unit tests for core/tasks.py — Pure function layer.

Tests the priority ranking logic and default branch resolution
without requiring running infrastructure.
"""
import tempfile
import subprocess
import pytest

from core.tasks import priority_rank, resolve_default_branch
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# priority_rank — Task Priority Ordering
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriorityRank:
    """Validates priority ranking for task queue ordering."""

    @pytest.mark.parametrize("priority,expected_rank", [
        ("urgent", 0),
        ("high", 1),
        ("medium", 2),
        ("normal", 3),
        ("low", 4),
    ])
    def test_known_priorities(self, priority, expected_rank):
        assert priority_rank(priority) == expected_rank

    def test_case_insensitive(self):
        """Priority should be case-insensitive."""
        assert priority_rank("URGENT") == 0
        assert priority_rank("High") == 1
        assert priority_rank("LOW") == 4

    def test_unknown_priority(self):
        """Unknown priority strings should sort last (rank 99)."""
        assert priority_rank("critical") == 99
        assert priority_rank("blocker") == 99
        assert priority_rank("asdfgh") == 99

    def test_none_priority(self):
        """None should be handled gracefully."""
        assert priority_rank(None) == 99

    def test_empty_string(self):
        assert priority_rank("") == 99

    def test_ordering_is_correct(self):
        """Verify the complete sort order: urgent < high < medium < normal < low < unknown."""
        priorities = ["low", "urgent", "medium", "high", "normal", "unknown"]
        sorted_priorities = sorted(priorities, key=priority_rank)
        assert sorted_priorities == ["urgent", "high", "medium", "normal", "low", "unknown"]


# ═══════════════════════════════════════════════════════════════════════════════
# resolve_default_branch — Git Default Branch Resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveDefaultBranch:
    """Validates default branch resolution with real git repositories."""

    @pytest.fixture
    def temp_git_repo(self):
        """Create a temporary git repository for testing."""
        tmp = tempfile.mkdtemp(prefix="flume-test-branch-")
        repo_path = Path(tmp)
        subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
        # Create an initial commit
        readme = repo_path / "README.md"
        readme.write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp, check=True, capture_output=True)
        yield repo_path
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_override_takes_precedence(self, temp_git_repo):
        """Explicit override should always be returned."""
        result = resolve_default_branch(temp_git_repo, override="develop")
        assert result == "develop"

    def test_override_none_falls_through(self, temp_git_repo):
        """None override should trigger git-based resolution."""
        result = resolve_default_branch(temp_git_repo, override=None)
        # Should return whatever the current branch is (likely 'main' or 'master')
        assert isinstance(result, str)
        assert len(result) > 0

    def test_nonexistent_path_returns_main(self):
        """Non-existent repo path should fallback to 'main'."""
        result = resolve_default_branch(Path("/nonexistent/path/12345"))
        assert result == "main"

    def test_override_empty_string_falls_through(self, temp_git_repo):
        """Empty string override should NOT be used (falsy)."""
        result = resolve_default_branch(temp_git_repo, override="")
        # Empty string is falsy, so it should fall through to git resolution
        assert result != ""
