#!/usr/bin/env python3
"""Implementer agent role.

The most complex role — runs a multi-turn tool-calling loop to
implement code changes against a repository checkout.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
from typing import Any, Optional

import httpx

from providers import get_registry
from roles.common import (
    AgentResult,
    current_llm_model,
    implementer_max_iterations,
    json_schema_implementer,
    load_system_prompt,
    preflight_validate_task,
)
from tools.definitions import ELASTRO_QUERY_TOOL, IMPLEMENTER_TOOLS
from tools.dispatch import dispatch_tool_call
from utils.logger import get_logger

logger = get_logger("roles.implementer")


async def run_implementer(
    task: dict[str, Any],
    repo_path: Optional[str] = None,
    on_progress: Optional[Any] = None,
    on_thought: Optional[Any] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> AgentResult:
    """Execute the Implementer agent loop.

    Resolves the LLM provider via ProviderRegistry, handles Codex-specific
    structured output, and runs the standard tool-calling loop for all other
    providers (Gateway).
    """
    system_prompt = load_system_prompt("implementer")
    model = task.get("preferred_model") or current_llm_model()

    def _progress(note: str) -> None:
        if on_progress:
            try:
                on_progress(note)
            except Exception:
                pass

    def _thought(note: str) -> None:
        if on_thought:
            try:
                on_thought(note)
            except Exception:
                pass

    # ── Provider resolution ──────────────────────────────────────────
    registry = get_registry()
    provider = registry.resolve(task)

    # ── Codex path (structured JSON, no tool loop) ───────────────────
    if provider.name == "codex" and repo_path:
        return await _run_codex_path(
            task, provider, model, system_prompt, repo_path,
            client, _progress, _thought,
        )

    # ── Pre-flight: detect phantom tasks before burning LLM tokens ───
    _phantom_result = preflight_validate_task(task, repo_path, _progress)
    if _phantom_result is not None:
        return _phantom_result

    # AST context hint
    system_prompt += (
        "\n\nCRITICAL CONTEXT: An Elastro AST index is available. For code changes, "
        "use `elastro_query_ast` first when searching for symbols. "
        "If the task is documentation-only or adds new files (e.g. README), AST hits "
        "may be sparse — then use `list_directory` on the repo root and `read_file` as "
        "needed. Do NOT use `run_shell` for `ls`/`cat` when `list_directory`/`read_file` "
        "apply; use `grep`/`find` via `run_shell` for search. "
        "You may use `grep` or `find` via `run_shell` for targeted file searches."
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": task,
                    "repo_path": repo_path,
                    "instruction": _build_implementer_instruction(task),
                },
                indent=2,
            ),
        },
    ]

    return await _run_tool_loop(
        task, provider, model, messages, repo_path, client, _progress, _thought,
    )


# ── Private helpers ──────────────────────────────────────────────────────


def _build_implementer_instruction(task: dict[str, Any]) -> str:
    """Build the instruction string for the implementer prompt."""
    instruction = (
        "Complete the task using the provided tools. "
        "Start by reading the task title and objective carefully. "
    )
    if task.get("requires_code"):
        instruction += (
            "This task MUST be treated as a code-edit task (task.requires_code=true): "
            "you MUST write files to the repo and call implementation_complete with a "
            "non-empty commit_message. Do NOT treat it as analysis/context."
        )
    else:
        instruction += (
            "Decide whether this is a code task, analysis task, or context task. "
        )

    instruction += (
        " Use elastro_query_ast when looking for existing code symbols. "
        "For new docs/files or when AST is not relevant, explore with "
        "list_directory/read_file. Then call implementation_complete with a clear summary."
        " If task.agent_log contains [Human guidance] or [Recovery], read those entries "
        "first and align your work with them."
        " After a blocked/retry cycle, prefer validating with tests and fixing failures "
        "before handing off."
    )

    if task.get("merge_conflict"):
        base_branch = task.get("merge_conflict_base_branch") or "develop"
        instruction += (
            " This task is a merge-conflict recovery. Do NOT re-implement the feature. "
            "Check out task.merge_conflict_head_branch, rebase onto origin/"
            + base_branch
            + " (or merge it in), resolve the conflicts in "
            "task.merge_conflict_files_preview using your best judgment of both sides, "
            "run or add tests for the merged result, and push --force-with-lease before "
            "calling implementation_complete. The dashboard will retry the integration "
            "merge once you push."
        )

    return instruction


async def _run_codex_path(
    task: dict[str, Any],
    provider: Any,
    model: str,
    system_prompt: str,
    repo_path: str,
    client: Optional[httpx.AsyncClient],
    _progress: Any,
    _thought: Any,
) -> AgentResult:
    """Codex-specific structured JSON path — no tool loop needed."""
    max_codex_attempts = 3 if task.get("requires_code") else 1
    schema = json_schema_implementer()

    for attempt in range(1, max_codex_attempts + 1):
        _progress(f"Using Codex provider (attempt {attempt}/{max_codex_attempts})…")
        repo_file_hint = (
            "\nEnsure your edits match the requested files."
            if task.get("requires_code")
            else ""
        )
        attempt_hint = ""
        if attempt > 1:
            attempt_hint = (
                f"\n\n[ATTEMPT {attempt}/{max_codex_attempts}]: "
                "The previous attempt did not return valid file edits.\n"
                "- You MUST edit at least one file in this repo before returning.\n"
                "- You MUST include at least one changed path in artifacts.\n"
                "- Do not return a no-op summary.\n"
            )
        messages_codex = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "You are operating inside the repository at the provided cwd. "
                    "Make any needed file edits directly. "
                    "When finished, return ONLY JSON matching the required schema.\n\n"
                    f"TASK JSON:\n{json.dumps(task, indent=2)}\n\n"
                    f"REPO PATH: {repo_path}\n\n"
                    "If task.requires_code is true, you must make real code edits "
                    "before finishing. Set artifacts to the list of changed file paths "
                    "relative to the repo root when possible."
                    + (
                        "\nFor code tasks: artifacts must contain at least one edited file path."
                        if task.get("requires_code")
                        else ""
                    )
                    + repo_file_hint
                    + attempt_hint
                ),
            },
        ]
        try:
            llm_resp = await provider.chat(
                client,
                messages_codex,
                model=model,
                json_schema=schema,
                agent_role="implementer",
                task_id=task.get("id", task.get("_id")),
                task=task,
            )
            response = (
                json.loads(llm_resp.content)
                if isinstance(llm_resp.content, str) and llm_resp.content
                else llm_resp.raw
            )
        except Exception as e:
            err = str(e).strip().replace("\n", " ")
            if err:
                _thought(
                    f"*[Codex]* attempt {attempt}/{max_codex_attempts} failed: {err[:500]}"
                )
            if attempt < max_codex_attempts:
                _progress(f"Codex attempt {attempt}/{max_codex_attempts} failed — retrying.")
                continue
            return AgentResult(
                action="implementer_failed",
                summary="Codex provider failed after retries.",
                artifacts=[],
                metadata={"source": "llm_no_response", "commit_sha": "", "commit_message": ""},
            )
        if response is None:
            response = {
                "summary": "Implementation attempt completed.",
                "commit_message": "",
                "artifacts": [],
            }
        if not isinstance(response, dict):
            response = {
                "summary": str(response).strip() or "Implementation completed.",
                "commit_message": "",
                "artifacts": [],
            }
        if response and isinstance(response, dict):
            if task.get("requires_code"):
                resp_artifacts = [
                    str(x) for x in (response.get("artifacts") or []) if str(x).strip()
                ]
                status = subprocess.run(
                    ["git", "-C", repo_path, "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                )
                has_changes = bool((status.stdout or "").strip())
                if not has_changes or not resp_artifacts:
                    _thought(
                        f"*[Codex]* attempt {attempt}/{max_codex_attempts} produced "
                        f"has_changes={has_changes}, artifacts={len(resp_artifacts)}"
                    )
                    if attempt < max_codex_attempts:
                        _progress(
                            f"Codex returned insufficient edit evidence "
                            f"(attempt {attempt}/{max_codex_attempts}) — retrying."
                        )
                        continue
            return AgentResult(
                action="handoff_to_tester",
                summary=(
                    str(response.get("summary") or "Implementation completed.").strip()
                    or "Implementation completed."
                ),
                artifacts=[
                    str(x) for x in (response.get("artifacts") or []) if str(x).strip()
                ],
                metadata={
                    "source": "llm_agentic",
                    "provider": provider.name,
                    "commit_sha": "",
                    "commit_message": str(response.get("commit_message") or "").strip(),
                },
            )

    _progress("Codex provider returned no usable file edits after retries.")
    return AgentResult(
        action="implementer_failed",
        summary="Codex provider returned no usable file edits after retries.",
        artifacts=[],
        metadata={"source": "llm_no_response", "commit_sha": "", "commit_message": ""},
    )


async def _run_tool_loop(
    task: dict[str, Any],
    provider: Any,
    model: str,
    messages: list[dict],
    repo_path: Optional[str],
    client: Optional[httpx.AsyncClient],
    _progress: Any,
    _thought: Any,
) -> AgentResult:
    """Execute the multi-turn tool-calling loop against the LLM."""
    final_summary = ""
    final_commit_message = ""
    final_artifacts: list[str] = []
    repo_touched = False
    nudge_sent = False
    max_iter = implementer_max_iterations()

    _progress("Agent started — analysing task…")

    for _iteration in range(max_iter):
        _progress(f"Thinking… (step {_iteration + 1}/{max_iter})")

        # ── Write-file nudge ─────────────────────────────────────────
        if (
            task.get("requires_code")
            and not repo_touched
            and not nudge_sent
            and _iteration >= 7
        ):
            nudge_sent = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "SYSTEM NUDGE: You have not written or modified any repository "
                        "files yet. Stop exploring. For this task you MUST call write_file "
                        "(or multi_replace_file_content) with the real deliverable, then "
                        "call implementation_complete with a commit message. "
                        "If the task is documentation (e.g. README.md), write that file "
                        "at the repo root now."
                    ),
                }
            )
            _progress("Nudge: require write_file before completion")

        # ── Provider-agnostic tool-calling ────────────────────────────
        dynamic_tools = IMPLEMENTER_TOOLS.copy()
        dynamic_tools.append(ELASTRO_QUERY_TOOL)

        llm_resp = await provider.chat_with_tools(
            client,
            messages,
            dynamic_tools,
            model=model,
            agent_role="implementer",
            task_id=task.get("id", task.get("_id")),
            task=task,
        )
        raw = llm_resp.raw if llm_resp else None

        if not raw:
            should_abort = _handle_backoff(
                _iteration, max_iter, task, _progress
            )
            if should_abort is not None:
                return should_abort
            continue

        message = raw.get("message", {})
        tool_calls = message.get("tool_calls") or []
        thoughts = message.get("thoughts") or ""
        content = message.get("content") or ""

        if thoughts:
            _thought(thoughts)
        elif content and tool_calls:
            _thought(content)

        # Normalize tool call shape
        norm_calls = []
        for idx, call in enumerate(tool_calls):
            call = dict(call)
            call.setdefault("id", f"call_{idx}")
            call.setdefault("type", "function")
            norm_calls.append(call)

        # Append assistant turn
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
        }
        if norm_calls:
            assistant_msg["tool_calls"] = norm_calls
        messages.append(assistant_msg)

        if not tool_calls:
            text = (message.get("content") or "").strip()
            if task.get("requires_code") and not repo_touched:
                _progress("Model returned text without tools — require file edits before finishing.")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Invalid stop: this task requires repository changes. "
                            "Do not answer with plain text. "
                            "Use write_file or multi_replace_file_content, then "
                            "implementation_complete."
                        ),
                    }
                )
                continue
            final_summary = text or "Implementation completed."
            _progress(f"Done: {final_summary[:120]}")
            break

        done = False
        for call in norm_calls:
            fn_name = call.get("function", {}).get("name", "")
            fn_args = call.get("function", {}).get("arguments", {})
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            call_id = call.get("id", "")

            # ── implementation_complete handling ─────────────────────
            if fn_name == "implementation_complete":
                if task.get("requires_code") and not repo_touched:
                    tool_result = json.dumps(
                        {
                            "status": "error",
                            "message": (
                                "Cannot complete yet: no file was written or modified "
                                "in this session. Call write_file or "
                                "multi_replace_file_content first, then call "
                                "implementation_complete again."
                            ),
                        }
                    )
                    _progress("Blocked implementation_complete — no repo writes yet")
                else:
                    final_summary = fn_args.get("summary", "Implementation completed.")
                    final_commit_message = fn_args.get("commit_message", "")
                    if not final_commit_message:
                        final_commit_message = "Verified task complete, no code changes required."
                    final_artifacts = fn_args.get("artifacts") or []
                    _progress(f"Completing: {final_summary[:120]}")
                    tool_result = "Implementation marked complete."
                    done = True
            else:
                # ── Dispatch to tool executor ────────────────────────
                tool_result, modified = await dispatch_tool_call(
                    fn_name,
                    fn_args,
                    repo_path=repo_path,
                    client=client,
                    progress_fn=_progress if fn_name not in ("implementation_complete",) else None,
                )
                if modified:
                    repo_touched = True

            messages.append(
                {"role": "tool", "content": tool_result, "tool_call_id": call_id}
            )

        if done:
            break

    if final_summary:
        return AgentResult(
            action="handoff_to_tester",
            summary=final_summary,
            artifacts=final_artifacts,
            metadata={
                "source": "llm_agentic",
                "commit_sha": "",
                "commit_message": final_commit_message,
            },
        )

    return AgentResult(
        action="handoff_to_tester",
        summary="Implementation step completed (fallback). Ready for test validation.",
        artifacts=[],
        metadata={"source": "fallback", "commit_sha": "", "commit_message": ""},
    )


def _handle_backoff(
    iteration: int,
    max_iter: int,
    task: dict[str, Any],
    _progress: Any,
) -> Optional[AgentResult]:
    """Handle exponential backoff with context-aware kill-switch polling.

    Returns an AgentResult if the task should abort, or None to continue.
    """
    base_delay = float(os.environ.get("FLUME_BACKOFF_BASE_DELAY", "2.0"))
    max_delay = float(os.environ.get("FLUME_BACKOFF_MAX_DELAY", "30.0"))
    jitter_factor = float(os.environ.get("FLUME_BACKOFF_JITTER_FACTOR", "0.2"))

    delay = min(base_delay * (2**iteration), max_delay)
    jitter = delay * jitter_factor

    logger.warning(
        {
            "message": "LLM returned no response — triggering backoff retry",
            "metric_id": "flume_backoff_events_total",
            "delay_sec": delay,
            "iteration": iteration,
        }
    )

    final_delay = delay + random.uniform(0, jitter)
    _slept = 0.0

    while _slept < final_delay:
        chunk = min(5.0, final_delay - _slept)
        time.sleep(chunk)
        _slept += chunk

        try:
            from worker_handlers import check_kill_switch, KillSwitchAbortError

            task_id = task.get("id", task.get("_id", ""))
            if task_id:
                check_kill_switch(task_id)
        except KillSwitchAbortError:
            _progress("Task blocked mid-backoff — aborting context.")
            return AgentResult(
                action="implementer_failed",
                summary=(
                    "Task halted via Kill Switch (node overload or user intervention) "
                    "during exponential backoff execution."
                ),
                artifacts=[],
                metadata={"source": "kill_switch", "commit_sha": "", "commit_message": ""},
            )
        except Exception:
            pass

    if iteration == max_iter - 1:
        _progress("LLM returned no response after max retries — stopping.")
        return AgentResult(
            action="implementer_failed",
            summary="LLM returned no response after exponential backoff exhaustion.",
            artifacts=[],
            metadata={
                "source": "llm_no_response",
                "commit_sha": "",
                "commit_message": "",
            },
        )
    _progress("LLM returned no response — retrying...")
    return None
