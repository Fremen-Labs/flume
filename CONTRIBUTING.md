<div align="center">

# `:: FLUME ENGINEERING FRONTIER ::`
## CONTRIBUTING TOPOLOGIES

</div>

Welcome, operator. The Flume architecture relies on precision memory management, zero-dependency executions, and ruthless architectural purity. To contribute to the Flume ecosystem, you must adhere strictly to these operational boundaries.

### 1. The Git Worktree Mandate
We operate exclusively inside isolated **Git Worktrees**. Never mutate the `main` execution branch. 
```bash
git worktree add ../flume-feat-nexus -b feat/nexus-core
```

### 2. High-Performance Execution (The Go CLI)
The Flume orchestrator is governed by the compiled `flume` binary (`cmd/flume`). 
*   **Aesthetics:** CLI outputs must embrace the `charmbracelet/lipgloss` bounds formatting data natively using Neo-Cyberpunk layouts. No messy `fmt.Println` stack traces.
*   **Concurrency:** Heavy internal CLI execution MUST route through `sync.WaitGroup` goroutines. Sequential lag is strictly outlawed.

### 3. The Backend Matrices
The Flume system runs via FastAPI Python architecture cleanly executing via Uvicorn. Ensure absolute execution structures properly parsing Pydantic abstractions directly inside `src/`. Do not pollute runtime arrays with trailing standard outputs.

### 4. Submitting the PR (The Upload)
1. Ensure your telemetry array compiles strictly (`go build -o flume cmd/flume/main.go`).
2. Run `flume doctor` verifying 0 ecosystem regressions locally.
3. Submit explicit GitHub Pull Requests routing the data natively.

*End of Transmission.*
