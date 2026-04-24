"""
Unit tests for core planning module — Prompt rendering and utilities.

NOTE: The original `truncate_message` import from `utils.formatter` does not
exist in the codebase. This file has been refactored to test real functions.
See test_planning.py for comprehensive planning module unit tests.
"""
import pytest

from core.planning import PLANNER_SYSTEM_PROMPT


@pytest.mark.unit
def test_system_prompt_contains_json_instruction():
    """The system prompt must instruct the LLM to output valid JSON."""
    assert "valid JSON" in PLANNER_SYSTEM_PROMPT

@pytest.mark.unit
def test_system_prompt_contains_complexity_guidance():
    """The system prompt must include complexity-proportional planning rules."""
    assert "COMPLEXITY-PROPORTIONAL" in PLANNER_SYSTEM_PROMPT

@pytest.mark.unit
def test_system_prompt_contains_plan_structure():
    """The system prompt must define the epics/features/stories/tasks hierarchy."""
    assert "epics" in PLANNER_SYSTEM_PROMPT
    assert "features" in PLANNER_SYSTEM_PROMPT
    assert "stories" in PLANNER_SYSTEM_PROMPT
    assert "tasks" in PLANNER_SYSTEM_PROMPT

@pytest.mark.unit
def test_system_prompt_prohibits_over_decomposition():
    """The prompt should warn against creating too many tasks for trivial changes."""
    assert "NEVER" in PLANNER_SYSTEM_PROMPT
    assert "over-decompose" in PLANNER_SYSTEM_PROMPT.lower() or "TRIVIAL" in PLANNER_SYSTEM_PROMPT
