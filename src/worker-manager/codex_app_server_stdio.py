#!/usr/bin/env python3
"""
Run a single Codex app-server session over stdio (JSON-RPC / JSONL) to obtain model text.

This uses the same auth/session as the official Codex CLI (~/.codex/auth.json), not
api.openai.com + ChatGPT OAuth bearer. Sync login with:

  ./flume codex-oauth import   # flume <- ~/.codex
  # or: codex login            # fills ~/.codex

Flume's .openai-oauth.json alone is not read by the Codex binary — the subprocess uses Codex's store.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any


def _codex_app_server_argv() -> list[str]:
    """Match ./flume codex-app-server resolution (global codex vs npx)."""
    if os.environ.get("FLUME_CODEX_APP_SERVER_VIA_NPX", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return ["npx", "--yes", "@openai/codex", "app-server"]
    bin_name = (os.environ.get("CODEX_BIN") or "codex").strip() or "codex"
    if os.path.isabs(bin_name) or "/" in bin_name:
        if os.path.isfile(bin_name) and os.access(bin_name, os.X_OK):
            return [bin_name, "app-server"]
    elif shutil.which(bin_name):
        return [bin_name, "app-server"]
    if shutil.which("npx"):
        return ["npx", "--yes", "@openai/codex", "app-server"]
    raise RuntimeError(
        "Codex CLI not found. Install Node.js, then: npm install -g @openai/codex "
        "or ensure npx is on PATH (Flume can run npx --yes @openai/codex app-server)."
    )


def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    """Flatten chat messages into one prompt (system + user/assistant)."""
    parts: list[str] = []
    for m in messages:
        role = (m.get("role") or "user").strip()
        content = m.get("content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        else:
            content = str(content)
        parts.append(f"### {role.upper()}\n{content}")
    return "\n\n".join(parts)


_TIMEOUT_TOKEN = object()
_EOF = object()


def _queue_get(q: queue.Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return _TIMEOUT_TOKEN


def chat_via_codex_app_server(
    messages: list[dict[str, Any]],
    model: str,
    *,
    cwd: str,
    temperature: float = 0.3,
    timeout_sec: float | None = None,
) -> str:
    """
    Spawn `codex app-server`, handshake, thread/start, turn/start, collect agent text.

    approvalPolicy never + readOnly sandbox reduces prompts for a planning-style completion.
    temperature is accepted for API parity but may be ignored by Codex for some models.
    """
    del temperature  # Codex turn/start uses effort/summary; keep signature for callers
    if timeout_sec is None:
        timeout_sec = float(os.environ.get("FLUME_CODEX_APP_SERVER_TIMEOUT", "900"))
    timeout_sec = max(30.0, timeout_sec)

    argv = _codex_app_server_argv()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    line_q: queue.Queue[str | object] = queue.Queue()
    err_lines: list[str] = []

    def _stdout_reader() -> None:
        try:
            for line in proc.stdout:
                s = line.strip()
                if s:
                    line_q.put(s)
        finally:
            line_q.put(_EOF)

    def _stderr_reader() -> None:
        try:
            for line in proc.stderr:
                s = line.rstrip()
                if s:
                    err_lines.append(s)
        except Exception:
            pass

    threading.Thread(target=_stdout_reader, daemon=True).start()
    threading.Thread(target=_stderr_reader, daemon=True).start()

    def send(obj: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _maybe_reply_server_request(ev: dict[str, Any]) -> None:
        if "method" not in ev or "id" not in ev:
            return
        if "result" in ev or "error" in ev:
            return
        mid = str(ev.get("method") or "")
        rid = ev.get("id")
        if rid is None:
            return
        if (
            "Approval" in mid
            or "approval" in mid.lower()
            or mid.startswith("serverRequest")
            or "elicitation" in mid.lower()
        ):
            send({"id": rid, "result": {}})

    def next_parsed(deadline: float) -> dict[str, Any] | None:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw = _queue_get(line_q, min(2.0, remaining))
            if raw is _TIMEOUT_TOKEN:
                continue
            if raw is _EOF:
                return None
            if not isinstance(raw, str):
                continue
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
        return None

    pending_notifications: list[dict[str, Any]] = []

    def wait_rpc(rpc_id: int, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            ev = next_parsed(deadline)
            if ev is None:
                break
            if ev.get("id") == rpc_id:
                if ev.get("error"):
                    raise RuntimeError(f"Codex app-server error: {ev['error']}")
                return ev.get("result") or {}
            _maybe_reply_server_request(ev)
            if (
                "method" in ev
                and "id" not in ev
                and "result" not in ev
                and "error" not in ev
            ):
                pending_notifications.append(ev)
        raise TimeoutError("Timed out waiting for Codex app-server RPC response")

    rpc_id = 0
    end = time.monotonic() + timeout_sec
    try:
        rpc_id += 1
        send(
            {
                "method": "initialize",
                "id": rpc_id,
                "params": {
                    "clientInfo": {"name": "flume", "title": "Flume", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        wait_rpc(rpc_id, end)

        send({"method": "initialized", "params": {}})

        rpc_id += 1
        send(
            {
                "method": "thread/start",
                "id": rpc_id,
                "params": {
                    "model": model,
                    "cwd": cwd,
                    "approvalPolicy": "never",
                    # Match Codex docs (thread/start); turn uses readOnly sandboxPolicy below.
                    "sandbox": "workspaceWrite",
                    "serviceName": "flume_llm_bridge",
                },
            }
        )
        t_res = wait_rpc(rpc_id, end)
        thread_id = (t_res.get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError(f"thread/start missing thread id: {t_res!r}")

        prompt = _messages_to_text(messages)
        rpc_id += 1
        send(
            {
                "method": "turn/start",
                "id": rpc_id,
                "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": cwd,
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "access": {"type": "fullAccess"},
                    },
                    "model": model,
                },
            }
        )
        wait_rpc(rpc_id, end)

        accumulated = ""
        final_agent_text = ""

        def _next_event() -> dict[str, Any] | None:
            if pending_notifications:
                return pending_notifications.pop(0)
            return next_parsed(end)

        while time.monotonic() < end:
            if proc.poll() is not None:
                break
            ev = _next_event()
            if ev is None:
                continue
            _maybe_reply_server_request(ev)
            if "id" in ev and ("result" in ev or "error" in ev):
                continue
            m = ev.get("method")
            params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
            if m == "item/agentMessage/delta":
                delta = ""
                for key in ("delta", "text", "patch"):
                    v = params.get(key)
                    if isinstance(v, str):
                        delta = v
                        break
                accumulated += delta
            elif m == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    final_agent_text = item["text"]
            elif m == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                st = turn.get("status")
                if st == "failed":
                    err = turn.get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"Codex turn failed: {msg}")
                break
        text_out = (final_agent_text or accumulated or "").strip()
        if not text_out:
            err_hint = "\n".join(err_lines[-15:]) if err_lines else ""
            raise RuntimeError(
                "Codex app-server returned no assistant text. "
                "Ensure `codex login` or ~/.codex/auth.json exists (Flume OAuth file alone is not used by Codex). "
                + (f" stderr (last lines):\n{err_hint}" if err_hint else "")
            )
        return text_out
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
