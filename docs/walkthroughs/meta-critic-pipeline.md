# The Meta-Critic Quality Enforcement Pipeline

The Flume V3 execution engine operates a highly deterministic, parallel code-review environment known as the **Meta-Critic**.

Rather than relying entirely on hallucination-prone Large Language Models to read diff strings, Flume intercepts proposed changes natively via its Go runtime to enforce mathematically precise code quality.

## 1. Zero-LLM Fast Pass Initialization

Before a developer's code is queried against any AI model, the Meta-Critic automatically runs a Zero-LLM Linter boundary natively.

- **For Python (`.py`)**: It synchronously invokes `ruff` checking for structural decay or deprecated modules.
- **For Go (`.go`)**: It invokes `golangci-lint` to assess concurrency integrity and syntax limits.

If these commands fail, the Flume pipeline terminates instantly—dumping explicit linter telemetry back to the GitHub PR. This guarantees we don't bleed GPU-compute or tokens on code that physically doesn't compile!

## 2. Elastro RAG Graph Engine

For successful structural compilations, the Meta-Critic performs deep RAG indexing entirely locally.

Using the `elastro` CLI, it analyzes the `git diff`, extracts explicit modified function names, and fires a GraphQL-esque dependency index lookup against your local `fremen_codebase_rag` Elastic index.

The Meta-Critic proxies this response to append a *"Blast Radius"* matrix to the prompt. Your AI agent now understands explicitly that a change inside `fetch_keys()` violently breaks five other modules downstream!

## 3. The Stateless Garbage Collection Loop

The Meta-Critic utilizes Elasticsearch to hash your diff strings deterministically. If you re-run an identical PR without changes, it short-circuits evaluation natively and regurgitates the last cached critique!

To prevent your Elastic Cluster from bloating unbounded with stale pipeline hashes, Flume operates a mathematically precise **Garbage Collection (GC)** sequence perfectly aligned to your cryptographically secure Git Ledger.

- **The Hook**: When executed with `{"mode": "garbage_collection"}` payload, the Go runtime fires an offline subprocess: `git log origin/main --since=14.days`.
- **The Execution**: It structurally reads every squash/merge footprint (`Merge pull request #148`), dynamically deduplicates the target PR numeric identifiers, and fires a bulk stateless `_delete_by_query` against the local ES nodes.
- **The Result**: Total Ephemeral Cleanups. Zero inbound open web-hook ports. Absolute kubernetes-rigor state management.
