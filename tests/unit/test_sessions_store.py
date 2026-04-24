"""
Unit tests for core/sessions_store.py — Pure function layer.

Tests the ISO timestamp utilities used across session management
without requiring a running Elasticsearch instance.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta

# Path setup and module mocking handled by conftest.py
from core.sessions_store import _utcnow_iso, _iso_elapsed_seconds


# ═══════════════════════════════════════════════════════════════════════════════
# _utcnow_iso — ISO 8601 Timestamp Generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestUtcnowIso:
    """Validates UTC ISO 8601 timestamp format compliance."""

    def test_returns_string(self):
        result = _utcnow_iso()
        assert isinstance(result, str)

    def test_ends_with_z_suffix(self):
        """Flume convention: UTC timestamps end with 'Z', not '+00:00'."""
        result = _utcnow_iso()
        assert result.endswith('Z'), f"Expected Z suffix, got: {result}"

    def test_no_plus_00_00_offset(self):
        """Must not contain the Python default '+00:00' offset."""
        result = _utcnow_iso()
        assert '+00:00' not in result

    def test_parseable_as_iso(self):
        """The output must be parseable back into a datetime."""
        result = _utcnow_iso()
        parsed = datetime.fromisoformat(result.replace('Z', '+00:00'))
        assert parsed.tzinfo is not None

    def test_within_reasonable_time(self):
        """Result should be within 2 seconds of actual UTC now."""
        result = _utcnow_iso()
        parsed = datetime.fromisoformat(result.replace('Z', '+00:00'))
        delta = abs((datetime.now(timezone.utc) - parsed).total_seconds())
        assert delta < 2.0, f"Timestamp drift: {delta}s"


# ═══════════════════════════════════════════════════════════════════════════════
# _iso_elapsed_seconds — Elapsed Time Calculator
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsoElapsedSeconds:
    """Validates elapsed-time calculation from an ISO timestamp to now."""

    def test_none_input_returns_none(self):
        assert _iso_elapsed_seconds(None) is None

    def test_empty_string_returns_none(self):
        assert _iso_elapsed_seconds("") is None

    def test_invalid_iso_returns_none(self):
        assert _iso_elapsed_seconds("not-a-date") is None

    def test_valid_z_suffix(self):
        """A recent timestamp should produce a small positive elapsed value."""
        started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace('+00:00', 'Z')
        result = _iso_elapsed_seconds(started)
        assert result is not None
        assert 3.0 < result < 10.0, f"Expected ~5s elapsed, got {result}"

    def test_valid_offset_suffix(self):
        """Timestamps with +00:00 offset should also parse correctly."""
        started = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        result = _iso_elapsed_seconds(started)
        assert result is not None
        assert 8.0 < result < 15.0

    def test_result_is_rounded(self):
        """Result should be rounded to 3 decimal places."""
        started = _utcnow_iso()
        result = _iso_elapsed_seconds(started)
        assert result is not None
        # Check that it's rounded (at most 3 decimal digits)
        str_result = str(result)
        if '.' in str_result:
            decimals = len(str_result.split('.')[1])
            assert decimals <= 3
