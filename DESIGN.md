# Design

## What mAistro Is

mAistro is a development engine where LLM-powered tasks coordinate through git. The user defines tasks — configurable units of autonomous work — and the platform dispatches them, streams their output, and records what they did. The app itself is the agent; tasks are its limbs.

The user points mAistro at a project directory. From that point, the platform owns the operational loop: triggering tasks, building prompts, invoking the LLM, and letting the LLM commit its own changes. The user's role shifts from writing code to defining what work should happen, when, and under what constraints.

---

## Requirements

### Projects

- The user opens a project directory. The platform initializes operational state inside it (`.maistro/` directory, gitignored). All project content lives in git; operational state lives in SQLite.
- Switching projects closes the current context and opens a new one. A project switch is blocked while any dispatch is active.
- The platform maintains a recent-projects list in a separate app-level store.

### Tasks

- A task is a named, configurable unit of work. It has a short description, detailed instructions (the prompt body), a model, and behavioral properties.
- Task identity is derived from the name (slugified). The name is the human-facing label; the slug is the system-facing key.
- Tasks are ordered. The user controls sort order explicitly.
- Task deletion cascades: removing a task removes its properties, dispatch history, and chat sessions.

### Task Properties

- Properties follow an entity-attribute-value pattern: a registry of property definitions (with types and defaults) and per-task overrides.
- Property types: `string`, `json`, `integer`, `boolean`. The platform casts stored strings to the declared type on read.
- Core properties: `description`, `instructions`, `model`, `subscriptions`, `depends_on`, `schedule`, `timeout`, `require_approval`, `coalesce_dispatches`, `base_tools`, `disallowed_tools`, `mcp_servers`, `sort_order`.

### Dispatch

- Every dispatch — regardless of trigger — enters a queue before execution. The queue is the single entry point to the execution engine.
- The background worker pulls from the queue and processes dispatches sequentially (one at a time).
- The queue operates in two modes: **auto-processing** (worker continuously pulls and executes) and **manual** (user explicitly triggers processing). A global setting controls which mode is active.
- Each dispatch records its full lifecycle: `created_at`, `started_at`, `completed_at`, `error`. A dispatch that has started but not completed is "active." A dispatch with an error is "failed."
- On startup, the worker sweeps any dispatches that were active when the process died and marks them as interrupted.

### Triggers

Four trigger types cause dispatches to be enqueued:

- **Manual** — the user explicitly dispatches a task. Always creates a new queue entry. Never coalesces. Bypasses approval gates.
- **Commit (watch)** — a git post-commit hook notifies the platform. Tasks whose subscription glob patterns match changed files are enqueued. Coalesces with other pending commit-triggered dispatches for the same task.
- **Schedule** — cron expressions evaluated by a background scheduler. Enqueues when the expression fires. Always coalesces globally (repeated fires while a dispatch is pending produce one run, not many).
- **Dependency** — when a task completes successfully, tasks declaring it as an upstream dependency are enqueued. Coalesces with other pending dependency-triggered dispatches for the same task. Timed-out or failed dispatches do not trigger dependents.

Two continuation triggers operate on existing dispatches:

- **Resume** — continues a previous dispatch using the CLI's session resume capability. Never coalesces.
- **Retry** — re-enqueues a failed or timed-out dispatch. Resurrects the original record in-place.

### Coalescing

- Coalescing prevents redundant pending dispatches. When a new trigger would create a dispatch but a compatible pending dispatch already exists, the trigger is appended to the existing dispatch's trigger list instead of creating a new record.
- `commit` and `dependency` triggers coalesce with other pending dispatches of the same trigger type for the same task.
- `schedule` triggers coalesce globally (any pending dispatch for the same task absorbs the new trigger).
- The `coalesce_dispatches` task property enables global coalescing for all trigger types — the task will never have more than one pending dispatch.
- `manual`, `resume`, and `retry` never coalesce — each represents distinct explicit intent.

### Subscriptions

- Subscriptions are glob patterns (supporting `**` recursion) that serve two purposes: **trigger matching** (which commits activate the task) and **context injection** (resolved files are listed in the dispatch prompt for the agent to read).
- A task with non-empty subscriptions is watch-active. There is no separate toggle.

### Dependencies

- Tasks declare upstream dependencies via `depends_on` (a list of task IDs). When an upstream task's dispatch completes successfully, dependent tasks are auto-enqueued.
- Circular dependencies are prevented at configuration time.
- Dependency context includes the upstream task name, dispatch ID, and commit range.

### Approval Gates

- Tasks with `require_approval` enabled produce dispatches with `approval='pending'` status (except manual dispatches, which bypass the gate).
- The worker skips pending-approval dispatches. A user must explicitly approve or reject.
- Rejection marks the dispatch as completed with an error of "rejected."

### Timeout Enforcement

- Each task has a configurable timeout (in seconds, default 900).
- A watchdog cancels the CLI subprocess after the timeout elapses. The dispatch is marked as timed out. Timed-out dispatches do not trigger dependents.

### Prompt Assembly

- The platform builds two prompts per dispatch: a **system prompt** (execution mode, documentation principles, git workflow, working directory) and a **user prompt** (task identity, instructions, invocation context, task registry, subscribed files, action directive).
- The system prompt establishes headless autonomous behavior: no questions, no clarification requests, commit-based workflow.
- The user prompt layers task-specific instructions with runtime context (why this dispatch was triggered, what other tasks exist, which files are subscribed).
- Agents commit their own changes. The platform does not auto-commit.

### Chat Sessions

- Every dispatch creates a linked chat session for durable output storage. The session holds the agent's streamed text as chat messages and raw NDJSON events as an audit trail.
- A standalone chat interface (not dispatch-linked) allows direct conversation with the LLM in the project context.
- Chat sessions persist across application restarts.

### Streaming

- Dispatch output streams to the frontend via Server-Sent Events (SSE). Event types: `text`, `tool_use`, `result`, `error`, `session_id`.
- Live subscribers receive events in real time. The stored session provides the same content for later retrieval.

### Git Integration

- The platform installs a post-commit hook in the project directory. This hook notifies the backend of new commits, enabling watch triggers.
- The platform reads git state (log, diff, head hash, changed files) but does not write to git directly — only agents commit.
- Dispatch diffs are tracked via `start_commit` and `result_commit`, enabling before/after comparison.

### MCP Servers

- External tool servers can be registered (name, command, args, env) and enabled/disabled. These extend the capabilities available to dispatched agents.

---

## Constraints

### Operational Integrity

- **Queue-first invariant**: no dispatch may execute without passing through the queue. All code paths — manual, watch, schedule, dependency — enqueue first, execute second.
- **Sequential execution**: exactly one dispatch runs at a time. The worker holds a lock during processing. No concurrent dispatch execution.
- **Project isolation**: each project has its own SQLite database. No cross-project state leakage. The app-level database holds only the recent-projects list.
- **Git as content truth**: all project content lives in git. The SQLite database holds only operational state (task configs, queue records, chat sessions). If the database is deleted, project content is unaffected.

### Data Integrity

- **Dispatch lifecycle is monotonic**: a dispatch progresses from created → started → completed. Once completed, a dispatch record is never restarted (retry creates a new cycle by resetting lifecycle fields on the same record).
- **Trigger context is immutable at the enqueue site**: each trigger entry's context string is built when the trigger fires, not when the dispatch executes. This preserves the causal record.
- **Cascading deletes**: task deletion removes all associated data (properties, dispatches, sessions). No orphaned records.

### Safety

- **Active dispatch blocks project switch**: the platform refuses to open a different project while a dispatch is running. This prevents state corruption from changing the working directory mid-execution.
- **Stale sweep on startup**: any dispatch marked as in-flight when the process starts is marked interrupted. No zombie dispatches.
- **Approval gates are non-bypassable for automated triggers**: only manual dispatch (explicit human intent) skips the approval check. All other trigger types respect `require_approval`.
- **Timeout enforcement is mandatory**: every dispatch has a timeout. The watchdog runs unconditionally. A task cannot run forever.
- **Dependency cycles are prevented**: the system validates `depends_on` at configuration time to prevent circular dependency chains.
