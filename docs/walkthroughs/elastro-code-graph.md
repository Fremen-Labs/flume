# Elastro Graph RAG Integration

The traditional RAG architecture attempts to chunk large Python and Go repositories based on tokens. This inevitably slices variables directly in half and results in catastrophic "hallucination boundaries" when an LLM tries to assemble missing lines of an orchestrator file.

Flume completely abolishes standard Vector RAG across code in favor of **Structural Graph RAG**, powered entirely by the native [Elastro Library](https://github.com/Fremen-Labs/elastro).

## 1. Elastro Code Mapping Architecture

Elastro natively indexes mathematical **AST (Abstract Syntax Trees)**, not raw strings.

Instead of inserting strings into Elasticsearch blindly, the Elastro Daemon analyzes your local workspace, mapping down explicit:
- Variables & Definitions
- Function Parameters
- Return Values
- `Downstream Callers` Graph Nodes
- `Upstream Triggers` Graph Nodes

This generates an identical reflection of your runtime execution boundaries perfectly encoded as Elastic Documents.

## 2. Bootstrapping Your Execution Graph

The Flume AI Workers seamlessly coordinate with Elastro. However, to execute the mapping, you must instruct Elastro to build the cluster representation natively using your Local OS.

**Step 1:** Verify the Flume Matrix is booted natively on your Host.
```bash
flume start
# Verify the elastro indexer is alive: http://localhost:9200
```

**Step 2:** Execute an Elastro RAG mapping against the root repository.
```bash
elastro doc index fremen_codebase_rag --dir ./src/
```

> [!NOTE]
> The Elastro indexer automatically queries local standard libraries (`ruff`, `golangci-lint`) checking node parity. When parsing multi-gigabyte files, expect initial mappings to demand substantial memory mapping.

## 3. Dynamic RAG Execution

Inside the Flume AI container, Agent Workers (e.g., `Implementer`, `Project Manager`) are strictly bound by the `elastro_query_ast` function tool payload. 

When your `Implementer` agent is instructed to refactor the `.env` generation system, it will autonomously invoke:
```json
{
  "name": "elastro_query_ast",
  "arguments": {
    "query": "fetch_llm_config",
    "target_path": "/workspace"
  }
}
```

The system responds natively with all contextual implementations, removing context blindness and significantly isolating bugs.
