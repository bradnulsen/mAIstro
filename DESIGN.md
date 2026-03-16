# Design

## What mAistro Is

mAistro is a development engine where LLM-powered tasks coordinate through git. The user defines tasks — configurable units of autonomous work — and the platform dispatches them, streams their output, and records what they did. The app itself is the agent; tasks are its limbs.

The user points mAistro at a project directory. From that point, the platform owns the operational loop: triggering tasks, building prompts, invoking the LLM, and letting the LLM commit its own changes. The user's role shifts from writing code to defining what work should happen, when, and under what constraints.

---

## Requirements

### Projects

- The user opens a project directory. The platform initializes it for use: ensures a git repository exists, installs the post-commit hook, manages gitignore entries, creates the `.maistro/` directory (gitignored) with an operational database inside it.
- Opening a project makes it the active context. All dispatch, task, and chat operations target the active project.
- Switching projects closes the current context and opens a new one. A project switch is blocked while any dispatch is active.
- The platform maintains a recent-projects list in a separate app-level store, enabling quick project switching.

### Tasks

- A task is a named, configurable unit of work. It has a short description, detailed instructions (the prompt body), a model, and behavioral properties.
- Task identity is derived from the name (slugified). The name is the human-facing label; the slug is the system-facing key. Once created, the slug is immutable — renaming a task does not change its identity.
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
- **Schedule** — cron expressions evaluated by a background scheduler. Enqueues when the expression fires. Always coalesces globally (repeated fires while a dispatch is pending produce one run, not many). The first evaluation after a schedule is set establishes a baseline without firing — a newly configured schedule does not immediately dispatch.
- **Dependency** — when a task completes successfully, tasks declaring it as an upstream dependency are enqueued. Coalesces with other pending dependency-triggered dispatches for the same task. Timed-out, failed, or cancelled dispatches do not trigger dependents.

Two continuation triggers operate on existing dispatches:

- **Resume** — continues a previous dispatch using the CLI's session resume capability. Never coalesces.
- **Retry** — re-enqueues a failed or timed-out dispatch. Resurrects the original record in-place (resets lifecycle fields and created_at to maintain fair queue ordering).

### Coalescing

- Coalescing prevents redundant pending dispatches. When a new trigger would create a dispatch but a compatible pending dispatch already exists, the trigger is appended to the existing dispatch's trigger list instead of creating a new record.
- `commit` and `dependency` triggers coalesce with other pending dispatches of the same trigger type for the same task.
- `schedule` triggers coalesce globally (any pending dispatch for the same task absorbs the new trigger).
- The `coalesce_dispatches` task property enables global coalescing for all trigger types — the task will never have more than one pending dispatch.
- `manual`, `resume`, and `retry` never coalesce — each represents distinct explicit intent.

### Subscriptions

Subscriptions are glob patterns (supporting `**` recursion) that define a task's **relevant files**. They serve two purposes:

- **Context injection** — every dispatch resolves the task's subscription patterns and lists matching files in the prompt as "files relevant to your task." The task's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering** — when a commit changes files matching a task's subscription patterns, the task is enqueued. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A task with non-empty subscriptions is watch-active. There is no separate toggle.

A task can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

### Dependencies

- Tasks declare upstream dependencies via `depends_on` (a list of task IDs). When an upstream task's dispatch completes successfully, dependent tasks are auto-enqueued.
- Circular dependency chains are permitted. Coalescing and sequential execution absorb redundant triggers — a task that is already pending absorbs the new trigger rather than creating an infinite queue.
- Dependency context includes the upstream task name, dispatch ID, and commit range.

### Approval Gates

- Tasks with `require_approval` enabled produce dispatches with `approval='pending'` status (except manual dispatches, which bypass the gate).
- The worker skips pending-approval dispatches. A user must explicitly approve or reject.
- Rejection marks the dispatch as completed with an error of "rejected."

### Timeout Enforcement

- Each task has a configurable timeout (in seconds, default 900).
- A watchdog cancels the CLI subprocess after the timeout elapses. The dispatch is marked as timed out. Timed-out dispatches do not trigger dependents.
- Cancellation is graceful: the platform signals termination, waits a grace period, then forces termination if the process has not exited.

### Prompt Assembly

- The platform builds two prompts per dispatch: a **system prompt** (execution mode, documentation principles, git workflow, working directory) and a **user prompt** (task identity, instructions, invocation context, task registry, subscribed files, action directive).
- The system prompt establishes headless autonomous behavior: no questions, no clarification requests, commit-based workflow.
- The user prompt layers task-specific instructions with runtime context (why this dispatch was triggered, what other tasks exist, which files are subscribed).
- The task registry — a manifest of all tasks with their names, descriptions, and subscriptions — is included in every dispatch prompt. Agents know what other agents exist.
- Agents commit their own changes. The platform does not auto-commit.

### Chat

- Every dispatch creates a linked chat session for durable output storage. The session holds the agent's streamed text as chat messages and raw events as an audit trail.
- A standalone chat interface provides direct conversation with the LLM in the project context. Its system prompt is built dynamically from live project state — task list, running dispatches, recent queue activity, git history — so the LLM has ambient awareness of what the platform is doing.
- Chat sessions persist across application restarts. Sessions can be resumed through the CLI's session continuation capability.

### Streaming

- Dispatch output streams to the frontend via Server-Sent Events (SSE). Event types: `text`, `tool_use`, `result`, `error`, `session_id`.
- Live subscribers receive events in real time. The stored session provides the same content for later retrieval.

### Git Integration

- The platform installs a post-commit hook in the project directory to notify the backend of new commits, enabling watch triggers. The hook is asynchronous and fails silently — hook failures do not affect git operations.
- The platform reads git state (log, diff, head hash, changed files). Only agents commit changes through MCP tools.
- Dispatch diffs are tracked via `start_commit` and `result_commit` for before/after comparison.

### Platform-Mediated Tool Access

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call is observable, auditable, and policy-governed. The server is context-aware: it reads the dispatching task's configuration and presents only relevant tools. Different tasks get different tool surfaces based on their properties and subscriptions. Agents retain native CLI tool access alongside MCP tools — mediated tools are structured alternatives, not restrictions. For detailed design, see architecture/tool-mediation.md.

### External MCP Servers

- External tool servers can be registered (name, command, args, env) and enabled/disabled. These extend the capabilities available to dispatched agents.

### User Interface

The product presents six views and a persistent chat surface:

- **Queue** — the operational center. Shows pending, active, and completed dispatches. Provides controls for processing, cancelling, approving/rejecting, resuming, and retrying. Selecting a dispatch shows its streamed output.
- **Feed** — git history enriched with dispatch metadata. Shows what changed and which dispatches produced those changes.
- **Tasks** — task configuration: create, edit, reorder, delete. Properties are organized by concern (definition, triggers). Inline dispatch for immediate execution.
- **Files** — a project file browser. The user searches for files by glob pattern and reads their contents. Markdown files render as formatted documents. Code files render with syntax highlighting for readability. This view provides direct, read-only access to project content without leaving the application.
- **Settings** — platform configuration: queue processing mode, default model, default timeout, MCP server management.
- **Chat** — a persistent, resizable tray providing interactive conversation with the LLM in the project context.

A status bar surfaces running dispatch indicators, providing ambient awareness of system activity without requiring the user to be on the Queue view.

### Contextual Help (Tooltips)

Configuration fields that involve syntax rules, non-obvious behavior, or domain-specific concepts provide hover tooltips. The tooltip appears on a help indicator adjacent to the field label — not on the input itself — so it does not interfere with interaction.

Tooltips explain *rules and behavior*, not just labels. They answer: "what do I type here?" and "what will this do?"

Required tooltip surfaces:

- **Subscriptions (glob patterns)** — syntax: `*` matches files in one directory, `**` matches recursively across directories. One pattern per line. Dual purpose: patterns determine which commits trigger the task *and* which files are included as context in the dispatch prompt.
- **Schedule (cron expression)** — five-field format: `minute hour day-of-month month day-of-week`. Ranges (`1-5`), lists (`0,15,30`), steps (`*/10`), and wildcards (`*`). Examples: `*/30 * * * *` (every 30 min), `0 9 * * 1-5` (weekdays at 9am). First evaluation after setting a schedule establishes a baseline — does not fire immediately.
- **Allowed Tools** — comma-separated tool names that the agent can use (e.g. `Read`, `Edit`, `Bash`, `Write`). When set, the agent is restricted to only these tools plus any MCP tools. When empty, the agent gets the default tool set.
- **Require Approval** — when enabled, automated triggers (commit-watch, schedule, dependency) produce dispatches that wait for manual approval before executing. Manual dispatches bypass this gate.
- **Coalesce Dispatches** — when enabled, the task will never have more than one pending dispatch. Any new trigger merges into the existing pending dispatch instead of creating a new queue entry. Useful for tasks that should catch up in one run rather than queuing redundant work.
- **Dependencies** — the task auto-dispatches when *any* selected upstream task completes successfully. Circular chains are allowed — coalescing prevents runaway queuing. Timed-out, failed, or cancelled dispatches do not trigger dependents.
- **Timeout** — maximum execution time in seconds. When reached, the platform gracefully terminates the agent, then force-kills if it does not exit. Timed-out dispatches do not trigger downstream dependencies. Set to 0 for no limit.
- **Auto-dispatch (Settings)** — when enabled, the background worker automatically pulls and executes pending dispatches. When disabled, dispatches remain pending until the user manually triggers processing from the Queue view.
- **MCP Servers (Settings)** — external tool servers that extend agent capabilities. Registered servers are available to dispatched agents as additional tools alongside the platform's built-in tool set. Command and args specify how to launch the server process.
- **Model** — the LLM model for this task. Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency tasks.

---

## Constraints

### Operational Integrity

- **Queue-first invariant**: every dispatch passes through the queue before execution. All code paths — manual, watch, schedule, dependency — enqueue first, then execute.
- **Sequential execution**: exactly one dispatch runs at a time. The worker holds a lock during processing.
- **Project isolation**: each project has its own SQLite database. The app-level database holds only the recent-projects list.
- **Git is content source-of-truth**: all project content lives in git. The SQLite database holds only operational state (task configs, queue records, chat sessions).
- **Single active project**: the platform operates on one project at a time. The active project is global state that all operations reference.

### Data Integrity

- **Task identity is immutable**: a task's slug ID, once derived from its initial name, stays constant. All references (dispatches, properties, dependencies) use the slug. Renaming changes only the display label.
- **Dispatch lifecycle is monotonic**: a dispatch progresses from created → started → completed. Retry creates a new cycle by resetting lifecycle fields on the same record, preserving dispatch identity.
- **Trigger context is immutable at enqueue time**: each trigger entry's context string is built when the trigger fires. This preserves the causal record — the prompt reflects what was true when the trigger occurred.
- **Task deletion cascades**: removing a task removes all associated data (properties, dispatches, sessions). This prevents orphaned records.
- **Running state is derived**: whether a dispatch is active is computed from lifecycle timestamps (started_at IS NOT NULL AND completed_at IS NULL), not persisted as a separate status field.

### Accountability

- **Tool mediation is observable**: every tool call that flows through the internal MCP server is logged as a structured event. The platform can reconstruct exactly what an agent did, not just what it produced.
- **Context-aware tool surfaces**: the set of tools available to an agent is determined by the task's configuration, not by the agent's own choices. The platform controls what actions are possible.

### Safety

- **Active dispatch locks project state**: while a dispatch is running, the platform keeps the project directory unchanged. This prevents state corruption from changing the working directory mid-execution.
- **Stale sweep on startup**: any dispatch marked as in-flight when the process starts is marked interrupted. This eliminates zombie dispatches.
- **Approval gates apply to all automated triggers**: manual dispatch (explicit human intent) bypasses the approval check; all other trigger types require `require_approval` approval.
- **Timeouts are enforced**: every dispatch has a configurable timeout. The watchdog runs unconditionally. Tasks have bounded execution time.
- **Dependency cycles coalesce gracefully**: circular dependency chains produce redundant triggers that coalescing absorbs, preventing unbounded dispatch growth.
- **Post-commit hook is non-blocking**: the hook runs asynchronously and fails silently. Hook failures never prevent or delay git operations.
