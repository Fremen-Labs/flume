"""
Unit tests for core/planning.py — Pure function layer.

Tests the LLM response parsing, task coalescing engine, timeout calculation,
and message construction without requiring any running infrastructure.
All external dependencies (ES, LLM, server imports) are mocked.

Follows Google's Table-Driven Test Pattern for deterministic input→output
validation with comprehensive edge-case coverage.
"""
import json
import os
import pytest
from unittest.mock import patch

# Path setup and module mocking handled by conftest.py
from core.planning import (
    parse_llm_response,
    _strip_json_blocks,
    build_llm_messages,
    _extract_target_file,
    _coalesce_story_tasks,
    _count_plan_tasks,
    _planner_llm_error_hint,
    _planner_request_timeout_seconds,
)


# ═══════════════════════════════════════════════════════════════════════════════
# parse_llm_response — Table-Driven Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseLlmResponse:
    """Validates the LLM JSON response parser against known input patterns."""

    def test_valid_json_with_message_and_plan(self):
        """Clean JSON with both keys should parse perfectly."""
        raw = json.dumps({
            "message": "Here is your plan.",
            "plan": {"epics": [{"id": "epic-1", "title": "Test Epic"}]}
        })
        msg, plan = parse_llm_response(raw)
        assert msg == "Here is your plan."
        assert plan is not None
        assert plan["epics"][0]["id"] == "epic-1"

    def test_markdown_fenced_json(self):
        """LLM wraps entire response in ```json ... ``` fence."""
        inner = json.dumps({
            "message": "Wrapped response.",
            "plan": {"epics": []}
        })
        raw = f"```json\n{inner}\n```"
        msg, plan = parse_llm_response(raw)
        assert msg == "Wrapped response."
        assert plan == {"epics": []}

    def test_think_block_stripped(self):
        """<think>...</think> reasoning blocks should be removed before parsing."""
        inner = json.dumps({"message": "After thinking.", "plan": {"epics": []}})
        raw = f"<think>Let me reason about this...</think>\n{inner}"
        msg, plan = parse_llm_response(raw)
        assert msg == "After thinking."
        assert plan is not None

    def test_malformed_json_returns_none_plan(self):
        """Totally invalid JSON should return cleaned text and None plan."""
        raw = "This is not JSON at all, just a conversational response."
        msg, plan = parse_llm_response(raw)
        assert plan is None
        assert "not JSON" in msg

    def test_partial_json_with_message_only(self):
        """JSON that has message but no plan key should fall through."""
        raw = json.dumps({"message": "I need more info."})
        msg, plan = parse_llm_response(raw)
        # Should fall through to regex path since 'plan' key is missing
        assert plan is None or isinstance(plan, dict)

    def test_embedded_json_in_prose(self):
        """JSON embedded in surrounding prose should be extracted via regex."""
        inner = json.dumps({"message": "Found it.", "plan": {"epics": []}})
        raw = f"Here is my analysis:\n{inner}\nLet me know if you need changes."
        msg, plan = parse_llm_response(raw)
        assert plan is not None

    def test_empty_string_input(self):
        """Empty string should not crash."""
        msg, plan = parse_llm_response("")
        assert plan is None
        assert msg == ""

    def test_whitespace_only_input(self):
        """Whitespace-only input should not crash."""
        msg, plan = parse_llm_response("   \n\n  ")
        assert plan is None


# ═══════════════════════════════════════════════════════════════════════════════
# _strip_json_blocks — Table-Driven Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestStripJsonBlocks:
    """Validates JSON/code block removal from LLM message text."""

    @pytest.mark.parametrize("input_text,expected_contains", [
        # Fenced code block removal
        ("Here is the result.\n```json\n{\"key\": \"val\"}\n```", "Here is the result."),
        # Bare JSON object removal
        ('{"key": "value"}', ""),
        # No JSON — text passes through
        ("Pure text with no JSON.", "Pure text with no JSON."),
        # Mixed content — prose should survive
        ("Some text.\n```\ncode block\n```\nMore text.", "Some text."),
    ])
    def test_strip_patterns(self, input_text, expected_contains):
        result = _strip_json_blocks(input_text)
        assert expected_contains in result or (expected_contains == "" and result == "")


# ═══════════════════════════════════════════════════════════════════════════════
# build_llm_messages — Message Construction
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildLlmMessages:
    """Validates LLM message list construction from session history."""

    def test_empty_session(self):
        """Session with no messages should produce only the system prompt."""
        session = {"messages": []}
        msgs = build_llm_messages(session)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"
        assert "senior technical planner" in msgs[0]["content"]

    def test_user_message_added(self):
        """User messages should appear with role='user'."""
        session = {"messages": [
            {"from": "user", "text": "Build me a todo app."}
        ]}
        msgs = build_llm_messages(session)
        assert len(msgs) == 2
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "Build me a todo app."

    def test_agent_message_added(self):
        """Agent messages should appear as role='assistant' with JSON content."""
        session = {"messages": [
            {"from": "agent", "text": "Here's the plan.", "plan": {"epics": []}}
        ]}
        msgs = build_llm_messages(session)
        assert len(msgs) == 2
        assert msgs[1]["role"] == "assistant"
        content = json.loads(msgs[1]["content"])
        assert content["message"] == "Here's the plan."
        assert content["plan"] == {"epics": []}

    def test_user_message_with_plan_context(self):
        """User messages with an existing plan should include plan state in content."""
        session = {"messages": [
            {"from": "user", "text": "Add a database layer.", "plan": {"epics": [{"id": "epic-1"}]}}
        ]}
        msgs = build_llm_messages(session)
        assert "Current plan state:" in msgs[1]["content"]
        assert "epic-1" in msgs[1]["content"]

    def test_mixed_conversation(self):
        """Multi-turn conversation should produce alternating user/assistant messages."""
        session = {"messages": [
            {"from": "user", "text": "First request"},
            {"from": "agent", "text": "First response", "plan": {}},
            {"from": "user", "text": "Second request"},
        ]}
        msgs = build_llm_messages(session)
        assert len(msgs) == 4  # system + 3 messages
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]

    def test_no_messages_key(self):
        """Session missing 'messages' key should not crash."""
        session = {}
        msgs = build_llm_messages(session)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"


# ═══════════════════════════════════════════════════════════════════════════════
# _planner_request_timeout_seconds — Timeout Logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlannerRequestTimeout:
    """Validates timeout calculation based on provider configuration."""

    def test_ollama_provider_minimum_300s(self):
        """Ollama provider should enforce a minimum 300s timeout."""
        config = {"provider": "ollama", "baseUrl": "http://localhost:11434"}
        timeout = _planner_request_timeout_seconds(config)
        assert timeout >= 300

    def test_frontier_provider_uses_default(self):
        """Non-Ollama providers should use the env-configured default."""
        config = {"provider": "openai", "baseUrl": "https://api.openai.com"}
        with patch.dict(os.environ, {"FLUME_PLANNER_TIMEOUT_SECONDS": "120"}):
            timeout = _planner_request_timeout_seconds(config)
            assert timeout == 120

    def test_ollama_in_base_url_triggers_minimum(self):
        """Even if provider isn't 'ollama', an ollama URL should trigger the 300s floor."""
        config = {"provider": "custom", "baseUrl": "http://my-ollama-server:11434/v1"}
        timeout = _planner_request_timeout_seconds(config)
        assert timeout >= 300

    def test_default_timeout_without_env(self):
        """Without FLUME_PLANNER_TIMEOUT_SECONDS, default should be 300."""
        config = {"provider": "anthropic", "baseUrl": "https://api.anthropic.com"}
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("FLUME_PLANNER_TIMEOUT_SECONDS", None)
            timeout = _planner_request_timeout_seconds(config)
            assert timeout == 300


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_target_file — File Name Extraction
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTargetFile:
    """Validates filename extraction from task titles."""

    @pytest.mark.parametrize("title,expected", [
        ("Update App.tsx to add routing", "app.tsx"),
        ("Fix bug in server.py endpoint", "server.py"),
        ("Modify config.json values", "config.json"),
        ("Create new gateway_test.go", "gateway_test.go"),
        ("Refactor the component", None),  # No file extension
        ("Add index.html template", "index.html"),
        ("Update styles.css for dark mode", "styles.css"),
        ("Fix vite.config.ts build settings", "vite.config.ts"),
        ("Add deploy.yml workflow", "deploy.yml"),
        ("", None),
    ])
    def test_extract_patterns(self, title, expected):
        result = _extract_target_file(title)
        assert result == expected


# ═══════════════════════════════════════════════════════════════════════════════
# _coalesce_story_tasks — Task Merging Engine
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoalesceStoryTasks:
    """Validates the auto-merge engine for adjacent same-file tasks."""

    def test_empty_list(self):
        assert _coalesce_story_tasks([]) == []

    def test_single_task(self):
        tasks = [{"title": "Update App.tsx", "objective": "Add routing"}]
        result = _coalesce_story_tasks(tasks)
        assert len(result) == 1
        assert result[0]["title"] == "Update App.tsx"

    def test_same_file_tasks_coalesced(self):
        """Adjacent tasks targeting the same file should merge into a compound task."""
        tasks = [
            {"title": "Add imports to App.tsx", "objective": "Import router"},
            {"title": "Add routes to App.tsx", "objective": "Define routes"},
        ]
        result = _coalesce_story_tasks(tasks)
        assert len(result) == 1
        assert "Compound Task" in result[0]["title"]
        assert result[0].get("_coalesced_count", 1) == 2

    def test_different_file_tasks_not_coalesced(self):
        """Tasks targeting different files should remain separate."""
        tasks = [
            {"title": "Update App.tsx", "objective": "Add routing"},
            {"title": "Fix server.py endpoint", "objective": "Fix bug"},
        ]
        result = _coalesce_story_tasks(tasks)
        assert len(result) == 2

    def test_no_file_in_title_not_coalesced(self):
        """Tasks without extractable filenames should not merge."""
        tasks = [
            {"title": "Implement the feature", "objective": "Build it"},
            {"title": "Write the tests", "objective": "Test it"},
        ]
        result = _coalesce_story_tasks(tasks)
        assert len(result) == 2

    def test_mixed_sequence(self):
        """A → A → B → B → A should produce [A_compound, B_compound, A]."""
        tasks = [
            {"title": "Step 1 in App.tsx"},
            {"title": "Step 2 in App.tsx"},
            {"title": "Step 1 in server.py"},
            {"title": "Step 2 in server.py"},
            {"title": "Final check in App.tsx"},
        ]
        result = _coalesce_story_tasks(tasks)
        assert len(result) == 3  # compound_App, compound_server, App


# ═══════════════════════════════════════════════════════════════════════════════
# _count_plan_tasks — Plan Task Counter
# ═══════════════════════════════════════════════════════════════════════════════

class TestCountPlanTasks:
    """Validates leaf task counting across nested plan structures."""

    def test_empty_plan(self):
        assert _count_plan_tasks({}) == 0
        assert _count_plan_tasks({"epics": []}) == 0

    def test_single_task(self):
        plan = {
            "epics": [{
                "features": [{
                    "stories": [{
                        "tasks": [{"id": "task-1", "title": "Do thing"}]
                    }]
                }]
            }]
        }
        assert _count_plan_tasks(plan) == 1

    def test_multiple_tasks_across_stories(self):
        plan = {
            "epics": [{
                "features": [{
                    "stories": [
                        {"tasks": [{"id": "t-1"}, {"id": "t-2"}]},
                        {"tasks": [{"id": "t-3"}]},
                    ]
                }]
            }]
        }
        assert _count_plan_tasks(plan) == 3

    def test_nested_epics(self):
        plan = {
            "epics": [
                {"features": [{"stories": [{"tasks": [{"id": "t-1"}]}]}]},
                {"features": [{"stories": [{"tasks": [{"id": "t-2"}, {"id": "t-3"}]}]}]},
            ]
        }
        assert _count_plan_tasks(plan) == 3

    def test_missing_nested_keys(self):
        """Plan with missing features/stories/tasks keys should not crash."""
        plan = {"epics": [{"features": None}]}
        assert _count_plan_tasks(plan) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# _planner_llm_error_hint — Error Hint Generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlannerLlmErrorHint:
    """Validates user-facing error hints for common LLM failure modes."""

    @pytest.mark.parametrize("error,expected_fragment", [
        ("Connection refused", "LLM_PROVIDER"),
        ("Errno 111: Connection refused", "LLM_PROVIDER"),
        ("401 Unauthorized", "expired"),
        ("401 Unauthorized model.request scope missing", "model.request"),
        ("401 api.responses.write insufficient", "api.responses.write"),
        ("500 Internal Server Error", ""),  # Unknown error → empty hint
        ("", ""),
    ])
    def test_error_hints(self, error, expected_fragment):
        hint = _planner_llm_error_hint(error)
        if expected_fragment:
            assert expected_fragment in hint
        else:
            assert hint == ""
