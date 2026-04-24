"""
Unit tests for core/projects_store.py — Project Configuration Defaults.

Tests the gitflow default backfill logic and project data structure
invariants without requiring a running Elasticsearch instance.
"""
import pytest

from core.projects_store import _ensure_gitflow_defaults, PROJECTS_INDEX


# ═══════════════════════════════════════════════════════════════════════════════
# _ensure_gitflow_defaults — Gitflow Configuration Backfill
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureGitflowDefaults:
    """Validates the gitflow config backfill for project entries."""

    REQUIRED_KEYS = [
        'autoPrOnApprove',
        'defaultBranch',
        'integrationBranch',
        'releaseBranch',
        'autoMergeIntegrationPr',
        'ensureIntegrationBranch',
    ]

    def test_missing_gitflow_key_creates_defaults(self):
        """Entry with no 'gitflow' key should get full default config."""
        entry = {"id": "proj-abc", "name": "Test"}
        result = _ensure_gitflow_defaults(entry)
        assert 'gitflow' in result
        for key in self.REQUIRED_KEYS:
            assert key in result['gitflow'], f"Missing gitflow key: {key}"

    def test_partial_gitflow_backfills_missing(self):
        """Entry with partial gitflow should get missing keys backfilled."""
        entry = {
            "id": "proj-abc",
            "gitflow": {"autoPrOnApprove": False}
        }
        result = _ensure_gitflow_defaults(entry)
        assert result['gitflow']['autoPrOnApprove'] is False  # Preserved
        assert result['gitflow']['integrationBranch'] == 'develop'  # Backfilled
        assert result['gitflow']['releaseBranch'] == 'main'  # Backfilled

    def test_complete_gitflow_untouched(self):
        """Entry with all gitflow keys should not be modified."""
        gitflow = {
            'autoPrOnApprove': False,
            'defaultBranch': 'develop',
            'integrationBranch': 'staging',
            'releaseBranch': 'production',
            'autoMergeIntegrationPr': False,
            'ensureIntegrationBranch': False,
        }
        entry = {"id": "proj-abc", "gitflow": dict(gitflow)}
        result = _ensure_gitflow_defaults(entry)
        assert result['gitflow'] == gitflow

    def test_default_values(self):
        """Verify specific default values when gitflow is missing."""
        entry = {"id": "proj-abc"}
        result = _ensure_gitflow_defaults(entry)
        gf = result['gitflow']
        assert gf['autoPrOnApprove'] is True
        assert gf['defaultBranch'] is None
        assert gf['integrationBranch'] == 'develop'
        assert gf['releaseBranch'] == 'main'
        assert gf['autoMergeIntegrationPr'] is True
        assert gf['ensureIntegrationBranch'] is True

    def test_returns_same_dict_reference(self):
        """Function should mutate and return the same dict, not a copy."""
        entry = {"id": "proj-abc"}
        result = _ensure_gitflow_defaults(entry)
        assert result is entry


# ═══════════════════════════════════════════════════════════════════════════════
# PROJECTS_INDEX — Index Name Constant
# ═══════════════════════════════════════════════════════════════════════════════

class TestProjectsIndexConstant:
    """Validates the Elasticsearch index name constant."""

    def test_index_name_is_string(self):
        assert isinstance(PROJECTS_INDEX, str)

    def test_index_name_value(self):
        assert PROJECTS_INDEX == "flume-projects"
