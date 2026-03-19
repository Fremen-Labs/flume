# Dispatcher Model Map

## Default mapping

- `epic` / `feature` / `story` creation -> `gpt-codex`
- `task` decomposition -> `gpt-codex`
- implementation task -> `qwen3`
- bug fix task -> `qwen3`
- formal code review -> `codex-code-review`
- review summary / backlog decision -> `gpt-codex`

## Immediate next implementation

When creating or claiming items, dispatcher logic should also set:
- `preferred_model`

So a queue item is not only assigned by role, but also by model intent.
