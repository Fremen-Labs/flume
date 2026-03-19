# Spawn Plan

## Current phase

Use queue state + role/model metadata now.

## Next phase

Hook claims to actual worker sessions:
- intake worker -> Codex session
- pm/dispatcher worker -> Codex session
- implementer worker -> Qwen3 session
- tester worker -> Qwen3 session
- reviewer worker -> Codex Code Review session
- memory updater worker -> Codex session

## Why not fully automatic yet?

We need the dispatcher to:
- create correct work items
- assign preferred models
- claim safely
- avoid duplicate workers

before spawning autonomous workers in loops.
