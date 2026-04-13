# Inception Skills Integration

The Flume Gateway relies on rigorous, stateless execution loops. When an LLM evaluates complex metadata pipelines, the inherent latency and token bloat inherently degrade network efficiency. To resolve this, Flume V3 introduces **Inception Skills**—an infrastructure upgrade enabling deterministic Golang handlers mapped natively to the Elasticsearch AST Construct.

Instead of generating runtime prompts for every worker request, Inception Skills decouple logic into immutable, pre-compiled Go execution maps.

---

## 1. The Compilation Phase

At the core of the Inception protocol is the Flume Meta-Critic compiler construct. Any skill initialized with the metadata flag `inception: full` triggers the native Go Code Generator directly overriding traditional LLM-dispatch bindings.

### Execution Protocol
The dashboard UI explicitly provides a **⚙ Compile & Reload** mechanic under the *Skills* tab, executing the matrix generation sequence via the orchestration shell:
```bash
flume skills compile --all
```

**The Generator Pipeline:**
1. Isolates the payload defined in the `.skill.md` definition securely.
2. Injects a centralized `skillslog.WithContext()` structure explicitly to bind output logs strictly to the unified Gateway telemetry nodes.
3. Automatically maps the declarative `ContextReqs` into a native HTTP client querying your locally attached `$ES_URL`, achieving immediate Contextual RAG matching directly inside the Go runtime—bypassing raw LLM inference architectures completely.

## 2. Core Registration Bounds (The Global Uplink)

Once generated, the files are localized natively in the `src/gateway/skills/generated/` pipeline. 

To bridge these natively compiled maps to the `SkillRegistry`, each handler auto-generates a Go `init()` function invoking the `RegisterCompiledHandler` hook. When the Gateway binary executes `flume start`, the unified system dynamically ingests these static arrays locally. 

If an `InceptionFull` skill lacks a compiled map at runtime, the gateway inherently falls back to a structural runtime generation pattern. However, to guarantee peak operations, recompiling and subsequently restarting the Go node natively (`./flume restart`) securely locks the pipeline into memory.

---

## 3. The Dashboard UI Manifest

Managing the compilation matrix natively via CLI is robust, but the unified Flume Dashboard natively surfaces this topology dynamically.

- **The Registry Port**: The UI binds to `GET /api/skills`, an internal API proxy directly tracing `http://localhost:8090/skills` exposing real-time skill compilation telemetry.
- **The Visual Interface**: Navigate to the **Inception Skills** tab in the dashboard. The pane maps out structural parameters (Ensemble sizes, Creativity weights) matching the underlying Markdown state perfectly against what the Gateway execution node expects dynamically. 

By grounding skill processing inside native Golang mappings natively bound to Elasticsearch RAG, Flume guarantees mathematical execution precision with strictly zero inference bleed.
