# Flume GUI: Onboarding & Visual Dispatch

The Flume ecosystem features a high-performance **React 18 + Vite** dashboard served securely via the local execution engine on port `8765`. 

Because our backend manages the underlying Docker graph and Elastic clusters natively, your workflow entirely abstracts away the command line. This guide walks you through registering a new codebase and successfully deploying an Autonomous Agent onto it.

---

## 1. Accessing the Matrix
Ensure you have executed the cold boot from your host machine natively:
```bash
flume start
```

Navigate your browser to exactly:
`http://localhost:8765`

*Flume does not impose a local account sign-up. The installation securely trusts your `localhost` execution origin!*

## 2. Onboarding a New Project

The Flume UI manages distinct "Projects", each bound to an explicitly declared repository.

1. **Click "New Project"** in the top-left repository switcher.
2. **Name Your Project**: Assign a highly recognizable name (e.g., *Frontend Registration Flow*). This determines how Flume uniquely isolates the Elastic indexing variables.
3. **Declare the Path (`repoUrl`)**: Flume requires the **absolute path** to your code natively, or a relative path mapped strictly inside the bound `./workspace` block.
    > *For Example*: `/workspace/fremenlabs/speed-read-trainer`
4. **Save**: Upon hitting save, Flume will silently spawn background tasks invoking the **Elastro RAG Grapher** natively. You won't see a loading bar, but the system is indexing your `import/export` AST dependencies natively behind the scenes!

---

## 3. Generating the Work Plan (The Intake Phase)

Once your repository is correctly indexed, your dashboard unlocks the **Plan New Work** interface.

Rather than hand-writing tickets, Flume delegates this mapping to your LLM configuration.

1. Select **"Plan New Work"**.
2. **The Prompt**: In open-text, describe explicitly what you want the overarching architecture to construct. 
    > *Example*: "Construct a new dark-mode compatible Hero Section utilizing Framer Motion for scroll reveals, and connect the Email input to our active Postgres API."
3. **Draft Plan**: The PM Agent will natively dissect your request into a rigid JSON structure broken into **Epics &rarr; Features &rarr; Stories &rarr; Tasks**. 
4. **Approve**: If the mathematical breakdown is satisfactory, click Commit to securely seal the pipeline into the local Elasticsearch tracker.

---

## 4. Triggering Autonomous Execution

Committing a plan populates the "Kanban Board" interface on the Dashboard.

By default, newly approved tasks are in the `approved` block safely waiting for human clearance. 
1. Open the created Task card.
2. Change the status from `Approved` to `In Progress`.

**This natively awakes the Swarm Matrix.** Without any further clicks, Flume will organically:
- Create a pristine isolated `git worktree` natively against your repository.
- Assign an explicit `Implementer` Agent role.
- Execute changes, run Zero-LLM Meta-Critic evaluation, and formulate a Pull Request back into your root branch!
