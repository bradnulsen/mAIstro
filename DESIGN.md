# Design

## What mAistro Is

mAistro is a development engine where LLM-powered agents coordinate through git. The user defines goals — configurable units of autonomous work — and the platform dispatches them as tasks, streams their output, and records what they did. The app itself is the agent; goals define what work exists, tasks are specific executions of that work.

The user points mAistro at a project directory. From that point, the platform owns the operational loop: triggering tasks, building prompts, invoking the LLM, and letting the LLM commit its own changes. The user's role shifts from writing code to defining what work should happen, when, and under what constraints.

### Naming

Two concepts, precisely defined:

- **Goal** — a named, persistent configuration of autonomous work. The user creates, edits, and deletes goals. Goals define *what* work exists: instructions, model, subscriptions, dependencies, schedule. Goals are the nouns of the system.
- **Task** — a scoped, contextualized execution of a goal. When a goal is dispatched (by any trigger), it produces a task in the dispatch queue. Tasks have lifecycle (pending, queued, active, completed, failed), trigger context, and linked output. Tasks are the verbs of the system.

A goal is a template. A task is an instance. One goal produces many tasks over time.

---

## Requirements

### Projects

- The user opens a project directory. The platform initializes it for use: ensures a git repository exists, installs the post-commit hook, manages gitignore entries, creates the `.maistro/` directory (gitignored) with an operational database inside it.
- Opening a project makes it the active context. All dispatch, goal, and chat operations target the active project.
- Switching projects closes the current context and opens a new one. A project switch is blocked while any task is active.
- The platform maintains a recent-projects list in a separate app-level store, enabling quick project switching.

### Goals

- A goal is a named, configurable unit of work. It has a short description, detailed instructions (the prompt body), a model, and behavioral properties.
- Goal identity is derived from the name (slugified). The name is the human-facing label; the slug is the system-facing key. Once created, the slug is immutable — renaming a goal does not change its identity.
- Each goal has a **color** — a visual identifier that distinguishes it across every surface where goals appear. Color is assigned randomly on creation from a curated palette of distinguishable hues. The user can change the color at any time via a simple color picker (a hue wheel or equivalent minimal control). The color is cosmetic metadata — it carries no behavioral semantics. It exists purely to let the user visually parse goal-related elements at a glance: queue items, task cards, status indicators, and anywhere else the goal's identity appears.
- Goal deletion cascades: removing a goal removes its properties, dispatch history, and chat sessions.

### Goal Properties

- Properties follow an entity-attribute-value pattern: a registry of property definitions (with types and defaults) and per-goal overrides.
- Property types: `string`, `json`, `integer`, `boolean`. The platform casts stored strings to the declared type on read.
- Core properties: `description`, `instructions`, `model`, `subscriptions`, `depends_on`, `schedule`, `timeout`, `max_turns`, `require_approval`, `coalesce_dispatches`, `allowed_tools`, `allowed_internal_tools`, `allowed_dispatch_targets`, `mcp_servers`, `sort_order`, `color`.

### Dispatch

- Every dispatch — regardless of trigger — creates a task in the dispatch queue before execution. The queue is the single entry point to the execution engine.
- The background worker pulls from the queue and processes tasks sequentially (one at a time). The worker only pulls tasks in the **queued** state — pending tasks are invisible to the worker.
- **Two-stage queue**: tasks progress through two pre-execution states before the worker picks them up:
  - **Pending** — the staging area. Newly created tasks land here by default. The user reviews, coalesces, and curates pending tasks before promoting them to queued. Pending tasks are *not* eligible for execution.
  - **Queued** — the execution runway. Tasks here are committed to run. The worker pulls the highest-priority queued task when ready. The user reorders queued tasks to control execution sequence.
- **Auto-queueing** — a global setting that controls the routing of newly created tasks. When enabled, new tasks skip pending and go directly to the queued state. When disabled, new tasks always enter pending. This replaces the former auto-dispatch concept. The distinction: auto-queueing controls *where tasks land on creation*, not whether the worker runs. The worker always runs — it simply has nothing to do when no queued tasks exist.
- The user can override auto-queueing for any individual task by dragging it back from queued to pending. The routing decision happens only at initial trigger time — once a task exists, the user has full manual control over its state.
- Each task records its full lifecycle: creation, queueing, start, and completion timestamps plus a terminal status. A task that has started but not completed is "active."
- Each task captures **execution metadata** when the agent finishes: stop reason (why the agent stopped — natural completion, max turns reached, or error), turns consumed, duration, and cost. This metadata is surfaced to the user alongside the task's outcome. It is the difference between "the agent finished" and "the agent finished in 3 turns at $0.02" — the latter lets the operator assess efficiency, detect anomalies, and tune goal configuration.
- On startup, the worker sweeps any tasks that were active when the process died and marks them as interrupted.

#### Terminal States

A task reaches a terminal state when execution ends (or is prevented). Seven distinct terminal states exist:

- **Completed** — the agent finished its work naturally. This is the success state. Only completed tasks trigger downstream dependencies.
- **Exhausted** — the agent consumed all available turns without finishing. The platform's per-task turn limit was reached and the CLI stopped the agent. This is a resource-limit stop, not a natural stop — the agent was cut off, not done. Partial commits may exist. Exhausted tasks do not trigger downstream dependencies. The user response is to investigate (what was the agent trying to do?), potentially increase the turn limit, and resume or re-dispatch.
- **Failed** — the task started but the agent encountered an unrecoverable error. The error context carries the diagnostic message.
- **Timed out** — the watchdog terminated the agent after exceeding the configured timeout. Partial commits may exist between `start_commit` and `result_commit`.
- **Cancelled** — the user explicitly stopped the task while it was running.
- **Interrupted** — the platform process died while the task was active. Detected and marked on startup by the stale sweep.
- **Rejected** — the user rejected a task awaiting approval. The task never executed.

The distinction matters: **completed is success; everything else is a form of non-success.** Each non-success state implies a different user response — retry a failure, resume a timeout, increase turns and re-dispatch after exhaustion, investigate after an interruption — so the UI must make the distinction immediately visible without requiring the user to inspect error details.

The difference between **exhausted** and **timed out** is the resource that ran out: turns vs. time. Both produce partial work. Both are non-success. But they have different operational responses: exhaustion suggests the task needs more turns (or the instructions are too open-ended), while timeout suggests the task needs more time (or the agent is stuck). The distinction prevents misdiagnosis.

### Task Ordering

- The queued column maintains a sort order that determines execution priority. The worker pulls the highest-priority queued task (lowest order position). The user controls execution order by reordering queued tasks via drag-and-drop.
- The pending column does not maintain a meaningful sort order — it is a staging area for curation, not a priority queue. Tasks in pending are displayed by creation time.
- Newly created tasks are appended to the end of their target column (pending by default, or queued if auto-queueing is enabled).
- Tasks transferred from pending to queued are appended to the end of the queued column. The user then reorders them to set priority.

### Triggers

Five trigger types cause tasks to be enqueued:

- **Manual** — the user explicitly dispatches a goal. Always creates a new task. Never coalesces. Bypasses approval gates.
- **Commit (watch)** — a git post-commit hook notifies the platform. Goals whose subscription glob patterns match changed files are enqueued as tasks. Coalesces with other pending commit-triggered tasks for the same goal.
- **Schedule** — cron expressions evaluated by a background scheduler. Enqueues a task when the expression fires. Always coalesces globally (repeated fires while a task is pending produce one run, not many). The first evaluation after a schedule is set establishes a baseline without firing — a newly configured schedule does not immediately dispatch.
- **Dependency** — when a task completes successfully, goals declaring its goal as an upstream dependency are enqueued. Coalesces with other pending dependency-triggered tasks for the same goal. Only successful completion triggers dependents — exhausted, failed, timed-out, cancelled, interrupted, and rejected tasks do not.
- **Agent** — another agent's task programmatically dispatches a goal via the `dispatch_task` MCP tool, with a message explaining why. The trigger context carries the dispatching task's identity and message. Coalesces with other pending agent-triggered tasks for the same goal. Agent dispatch is subject to goal-level dispatch control — a goal property governs which other goals an agent can dispatch.

Three continuation triggers operate on existing tasks:

- **Resume** — continues a previous task using the CLI's session resume capability. Creates a new task and coalesces the original under it (inverted — the new task is the root, the original becomes subordinate). The CLI resumes the original session.
- **Reply** — creates a follow-up task targeting a resolved task with additional user context. Like resume, the original task is coalesced under the new one. Unlike resume, reply starts a fresh session — it does not resume the original CLI session.
- **Retry** — re-enqueues a failed or timed-out task. Resurrects the original record in-place (resets lifecycle fields and created_at to maintain fair queue ordering).

Resume and reply use **inverted coalescing**: the new task becomes the root and the original becomes a subordinate. In chains (A → reply B → reply C), the latest task is always the root and all predecessors are flat subordinates. This preserves full provenance while keeping the newest intent as the dispatchable unit.

### Coalescing

Coalescing prevents redundant pre-execution tasks. Two mechanisms exist: **automatic coalescing** (at enqueue time, driven by trigger rules) and **manual coalescing** (user-initiated, from the queue).

#### Automatic Coalescing

- When a new trigger would create a task but a compatible pre-execution task (pending or queued) already exists, the trigger is appended to the existing task's trigger list instead of creating a new record. Coalescing checks both columns — a new trigger coalesces into whichever matching task exists, regardless of whether it is pending or queued.
- `commit`, `dependency`, and `agent` triggers coalesce with other pre-execution tasks of the same trigger type for the same goal.
- `schedule` triggers coalesce globally (any pre-execution task for the same goal absorbs the new trigger).
- The `coalesce_dispatches` goal property enables global coalescing for all trigger types — the goal will never have more than one pre-execution task.
- `manual` and `retry` never coalesce — each represents distinct explicit intent.
- `resume` and `reply` do not participate in automatic coalescing (they always create a new task), but they establish an inverted coalesce relationship with the original task — the original becomes subordinate to the new task.

#### Manual Queue Composition

The user can merge and split tasks in the pending column directly from the Dispatch view. This gives explicit control over the grouping that automatic coalescing performs implicitly. Composition is a pending-column operation — it belongs to the curation stage, not the execution runway.

**Merge** — the user drags a pending task directly onto another pending task for the same goal. The two tasks combine into a single task. All trigger entries from both tasks are collected into the surviving task's `triggers` array. The older task (by `created_at`) survives; the dragged task is removed from the queue. The surviving task retains its position.

- Only pending tasks can be merged (not queued, not started, not completed, not pending-approval). Merge is a curation operation that belongs to the staging area.
- Only tasks belonging to the same goal can be merged. Merging across goals would produce a task with ambiguous identity — one task cannot represent two goals. The UI enforces this by suppressing the merge affordance when the dragged task and the drop target belong to different goals.
- The merge operation is the manual equivalent of what automatic coalescing does at enqueue time: multiple reasons to run become one run that addresses all of them.
- Trigger context is preserved verbatim. Each trigger entry retains the context string it was created with — merge does not rewrite history.

**Drag Interaction Model** — drag operations are specialized by column, reflecting the distinct purpose of each stage:

- **Pending column (coalescing)** — the pending column is for grouping and curating work before it runs. Drag operations in pending support **merge** (drop onto a same-goal task to coalesce) and **transfer** (drop into the queued column to promote). Reordering within pending is not meaningful — pending is a staging area, not a priority queue. The order tasks leave pending is determined by when the user transfers them to queued.
- **Queued column (sorting)** — the queued column is the execution runway where order determines priority. Drag operations in queued support **reorder** (drop between tasks to change execution priority) and **transfer** (drop into the pending column to demote). Merge is not available in queued — coalescing decisions are made during staging, not after commitment to run.
- **Transfer** (between columns) — drop into the other column. Moves the task from pending to queued or from queued to pending. The transferred task is appended to the end of the target column. Visual feedback: the target column highlights as a drop zone.

**Each column has one primary drag operation plus transfer.** Pending owns coalescing (merge); queued owns sorting (reorder). This separation reflects the workflow: curate and group work in pending, then commit and prioritize in queued. Cross-column drag is exclusively a transfer — it changes state without merging or reordering within the target column.

**Split** — the user takes a pending task that has multiple trigger entries and breaks it into individual tasks, one per trigger. The original task keeps its first trigger entry and position; new tasks are created for each remaining trigger and appended to the end of the pending column.

- Only pending tasks with more than one trigger entry can be split. Split is a coalescing operation and coalescing belongs to the pending column.
- Each resulting task is an independent queue entry with its own lifecycle.
- Split reverses a previous merge or automatic coalescing. The user can inspect the accumulated triggers on a task and decide they should run separately.
- New tasks created by split inherit the goal's current approval gate setting. If `require_approval` is enabled, split-off tasks enter pending-approval state.

**Why same-goal only**: a task's identity is bound to exactly one goal. The task record carries a `task_id` (the goal slug), and prompt assembly, subscriptions, tool configuration, and commit authorship all derive from that single goal. Merging tasks across goals would require either a compound identity (one task, two goals — breaks prompt assembly, tool surfaces, authorship) or a synthetic super-goal (implicit, unmanageable). Neither is coherent. The constraint preserves the foundational invariant: one task, one goal, one execution context.

### Subscriptions

Subscriptions are glob patterns (supporting `**` recursion) that define a goal's **relevant files**. They serve two purposes:

- **Context injection** — every task resolves the goal's subscription patterns and lists matching files in the prompt as "files relevant to your task." The goal's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering** — when a commit changes files matching a goal's subscription patterns, the goal is enqueued as a task. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A goal with non-empty subscriptions is watch-active. There is no separate toggle.

A goal can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

### Dependencies

- Goals declare upstream dependencies via `depends_on` (a list of goal IDs). When an upstream goal's task completes successfully, dependent goals are auto-enqueued.
- Circular dependency chains are permitted. Coalescing and sequential execution absorb redundant triggers — a goal that already has a pending task absorbs the new trigger rather than creating an infinite queue.
- Dependency context includes the upstream goal name, task ID, and commit range.

### Approval Gates

- Goals with `require_approval` enabled produce tasks with `approval='pending'` status (except manual dispatches, which bypass the gate).
- The worker skips pending-approval tasks. A user must explicitly approve or reject.
- Rejection marks the task as completed with an error of "rejected."

### Timeout Enforcement

- Each goal has a configurable timeout (in seconds, default 900).
- A watchdog cancels the CLI subprocess after the timeout elapses. The task is marked as timed out. Timed-out tasks do not trigger dependents.
- Cancellation is graceful: the platform signals termination, waits a grace period, then forces termination if the process has not exited.

### Turn Limit

- Each goal has a configurable maximum number of agent turns (`max_turns`). A turn is one cycle of the agent receiving context, reasoning, and producing output (text or tool calls).
- When the agent reaches the turn limit, the CLI stops the session. The platform marks the task as exhausted. Exhausted tasks do not trigger downstream dependencies.
- The turn limit is a safety bound, not a target. Most tasks finish well within the limit. When a task hits the limit, it typically means the instructions are too broad, the agent is stuck in a loop, or the work genuinely requires more interaction than anticipated.
- The user sees turns consumed alongside the outcome — "completed in 5/50 turns" vs. "exhausted at 50/50 turns" — so the limit's effect is always visible, not just when it triggers.

### Prompt Assembly

- The platform builds two prompts per task: a **system prompt** (execution mode, documentation principles, git workflow, working directory) and a **user prompt** (goal identity, instructions, invocation context, goal registry, subscribed files, action directive).
- The system prompt establishes headless autonomous behavior: no questions, no clarification requests, commit-based workflow.
- The user prompt layers goal-specific instructions with runtime context (why this task was triggered, what other goals exist, which files are subscribed).
- The goal registry — a manifest of all goals with their names, descriptions, and subscriptions — is included in every task prompt. Agents know what other agents exist.
- Agents commit their own changes. The platform does not auto-commit.

### Chat

- Every task creates a linked chat session for durable output storage. The session holds the agent's streamed text as chat messages and raw events as an audit trail.
- A standalone chat interface provides direct conversation with the LLM in the project context. Its system prompt is built dynamically from live project state — goal list, running tasks, recent queue activity, git history — so the LLM has ambient awareness of what the platform is doing.
- Chat sessions persist across application restarts. Sessions can be resumed through the CLI's session continuation capability.

### Task Session Interrogation

Completed tasks produce outcome summaries and diffs, but the user needs to ask follow-up questions: "Why did you change this file?" "What alternatives did you consider?" This requires conversational access to the agent's session — the same context, the same reasoning chain.

- **Session resume from Resolved** — resolved tasks in the Dispatch view's Resolved column provide a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact via the CLI's `--resume` capability.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. No `Edit`, `Write`, `Bash` (or Bash restricted to read-only mode), no `git_commit`. The agent can read files, search code, and reason about its prior work, but cannot modify the project. Interrogation does not produce side effects.
- **Work flows through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.
- **UI integration** — the session chat appears in the same chat tray used for standalone conversation, but with a visual indicator that this is a task session (goal color, task reference) and that it is read-only.

### Notifications

The platform must inform the operator when events need attention — without requiring them to be watching. As task volume grows through automated triggers, scheduled goals, and agent-initiated dispatches, the operator is increasingly absent during execution. The dashboard answers "how are my agents doing?" when the operator looks; notifications answer "something needs you" when the operator isn't looking.

- **In-app notification feed** — a persistent notification surface (badge indicator with dropdown or panel) showing recent events that warrant operator attention. Events include: task failures, timeouts, approval requests pending, and optionally task completions. The feed is ordered by recency. Read/unread state distinguishes new events from acknowledged ones. The badge count reflects unread notifications — the operator sees at a glance whether anything needs attention without opening the feed.
- **Desktop notifications** — browser Notification API for events that need attention when the operator is away from the tab. The operator opts in via a standard browser permission prompt. The platform does not assume permission — it degrades gracefully when denied. Critical events (failures, timeouts, approval requests) are the default desktop notification triggers. Desktop notifications are a supplement to the in-app feed, not a replacement — every desktop notification has a corresponding in-app entry.
- **Notification rules** — configurable per-goal or globally. The operator controls which events produce notifications: all completions, only failures, only timeouts, only approval requests, or never. Defaults to failures, timeouts, and approval requests — these are the events that most commonly require operator response. The operator tunes signal-to-noise as task volume grows. Rules are additive: a per-goal rule overrides the global default for that goal.
- **Webhook integration** — an outbound webhook that fires on configurable events. The operator registers a URL (and optional secret for signature verification). The platform sends a structured JSON payload describing the event: what happened, which goal, which task, when. This is the escape hatch for external systems — Slack, Discord, email, or any HTTP endpoint. One generic mechanism rather than N specific integrations. The webhook fires independently of in-app and desktop notifications — it is a separate channel, not gated by notification rules.

**What notifications are not:**

- Notifications are not a messaging system. They carry structured event data, not free-text messages. The operator cannot reply to or compose notifications.
- Notifications do not replace the dashboard or dispatch view. They are nudges that tell the operator *when* to look, not *what* to look at. Investigation happens on existing surfaces.
- Notifications do not create or modify tasks. They are read-only signals. The operator responds to a notification by navigating to the appropriate view and taking action there.

### Streaming

- Task output streams to the frontend via Server-Sent Events (SSE). Event types: `text`, `thinking`, `tool_use`, `result`, `error`, `session_id`.
- **Thinking content** — when the agent produces reasoning or thinking blocks, these are streamed as `thinking` events. The user sees the agent's reasoning process alongside its actions. Thinking content is part of the agent's output — dropping it silently degrades the user's ability to understand what the agent is doing and why.
- Live subscribers receive events in real time. The stored session provides the same content for later retrieval.
- **Incremental persistence** — chat events are persisted incrementally during streaming, not accumulated in memory and flushed once at the end. A crash or failure mid-stream must not discard all events captured up to that point. The stored session is the authoritative record of what the agent did; its durability cannot depend on clean termination.

### Dispatch Outcomes

When a task completes, the platform must make the result understandable without requiring the user to read the full streamed output.

- **Outcome summary** — the platform derives a short summary from the commits produced during the task's execution window (between `start_commit` and `result_commit`). The summary captures what the agent actually did — commit messages and change statistics — not a restatement of the instructions. Displayed inline in the Dispatch view so users can scan completed tasks at a glance.
- **Execution metadata** — each resolved task displays its execution metadata: stop reason, turns consumed (relative to the limit), duration, and cost. This is visible alongside the outcome summary — the user sees not just what the agent did but how much it cost to do it. Turns consumed relative to the limit is especially important: "3/50 turns" is healthy; "50/50 turns (exhausted)" is a signal to investigate.
- **Outcome in dependency context** — when a completed task triggers downstream dependents, the outcome summary is included in the trigger context passed to the dependent task's prompt. This gives downstream agents concrete information about what their upstream actually produced, not just that it completed.

The outcome summary is derived, not authored. The platform computes it from git artifacts that already exist. The user does not write summaries; the agent does not produce them explicitly. The platform reads what happened and describes it.

### Git Integration

- The platform installs a post-commit hook in the project directory to notify the backend of new commits, enabling watch triggers. The hook is asynchronous and fails silently — hook failures do not affect git operations.
- The platform reads git state (log, diff, head hash, changed files). Only agents commit changes through MCP tools.
- Task diffs are tracked via `start_commit` and `result_commit` for before/after comparison.

### Platform-Mediated Tool Access

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call flows through platform code, making actions observable, auditable, and policy-governed.

- The server is context-aware: it reads the dispatching goal's configuration and presents only relevant tools. Different goals get different tool surfaces based on their properties and subscriptions.
- **Git operations as structured tools** — `git_commit`, `git_diff`, `git_log`, `git_status` with enforced conventions (commit authorship, message format). These replace unmediated shell-based git access.
- **Git branch operations** — `git_branch_create` (create a new branch from a specified base, with enforced naming conventions such as `<goal-id>/<description>`), `git_branch_switch` (switch the working directory to a named branch, with the platform tracking which branch a task operates on for audit purposes), and `git_branch_merge` (merge a source branch into the current branch, surfacing merge conflicts as structured tool output rather than silent failures).
- **Read-only project context tools** — file listing, file reading, and goal information retrieval, scoped by the goal's subscriptions and configuration.
- **Inter-agent coordination tools** — `dispatch_task` (enqueue a task for another goal with a message, creating an `agent` trigger attributed to the dispatching task) and `get_queue_status` (read-only view of queue state — what's pending, running, and backed up). These give agents situational awareness and imperative coordination beyond the declarative trigger system.
- **Tool invocation logging** — every MCP tool call is recorded as a structured event in the task session, creating an audit trail richer than NDJSON stream parsing. Branch operations are logged identically — the platform can reconstruct which branches a task created, switched to, and merged.
- Agents retain access to native CLI tools (including Bash) alongside MCP tools. MCP tools are structured alternatives, not an exclusive replacement.

### Tool Configuration

Each goal configures what tools its agent can access:

- **Allowed Tools** (`allowed_tools`) — the CLI tool names the agent can use (e.g. `Read`, `Edit`, `Bash`, `Write`). When set, the agent is restricted to exactly these tools plus any tools from connected MCP servers. When empty, the agent gets the default tool set.
- **Allowed Internal Tools** (`allowed_internal_tools`) — selects which internal MCP tools the goal's agent can access. When set, only the specified internal tools are presented. When empty, all internal tools are available (backward compatible). A read-only goal might be restricted to `list_files`, `read_file`, `git_log`, `git_diff`. A writer goal gets the full set including `git_commit` and branch operations.
- **MCP Servers** (`mcp_servers`) — selects which registered external MCP servers are available to this goal's agent. Only servers listed here (and enabled globally) are connected during dispatch. The platform's internal MCP server is always connected.

Tool configuration is **opt-in only**. The user defines what a goal *can* do — one concept, one property. There is no complementary "disallowed" property. The platform computes the inverse internally: tools not in the allowed set are removed from the agent's environment entirely. The agent never sees them, never reasons about them, never attempts to work around restrictions.

This is not a stylistic choice — it follows from headless execution. A headless agent cannot ask for permissions, cannot negotiate tool access, cannot be told "you're not allowed to use X" in a meaningful way. The only coherent model is to present exactly the tools the agent can use and nothing else. The restriction surface is the platform's responsibility, not the agent's.

The three tool configuration properties — `allowed_tools`, `allowed_internal_tools`, and `mcp_servers` — compose into the agent's complete tool surface at dispatch time. CLI tools are selected by `allowed_tools`, internal MCP tools are selected by `allowed_internal_tools`, and external MCP tools come from the servers selected by `mcp_servers`. Each dimension is independently configurable, and the default for each is "everything available."

### Tool Discoverability

The platform must make the available tool inventory visible and selectable. A user configuring a goal's tools should never need to guess names, remember spelling, or consult external documentation.

Three tool sources exist, each requiring discovery:

- **Built-in CLI tools** — the default tools provided by the Claude CLI (e.g. `Read`, `Edit`, `Write`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `Agent`, `NotebookEdit`). The platform queries the CLI for its available tool list and presents them. These are the tools that `allowed_tools` selects from.
- **Internal MCP tools** — the tools hosted by the platform's own MCP server (`git_commit`, `git_diff`, `git_log`, `git_status`, `git_branch_create`, `git_branch_switch`, `git_branch_merge`, `list_files`, `read_file`, `list_tasks`, `dispatch_task`, `get_queue_status`). The platform knows these directly — it defines them. The internal server is always connected, but individual internal tools are subject to per-goal selection via `allowed_internal_tools`. When no selection is made, all internal tools are available.
- **External MCP server tools** — tools provided by registered external servers. When a server is registered and enabled, the platform connects to it and discovers its tool list. These tools become visible in the per-goal configuration surface alongside built-in tools.

The configuration surface for `allowed_tools` presents the full inventory of available tools as a selectable list — checkboxes, multi-select, or equivalent. The user selects from available options.

This is a discoverability requirement, not a UI prescription. The essential behavior: the user sees what is available and selects what they want.

### External MCP Servers

External MCP servers extend the tool surface available to agents beyond the platform's built-in and internal tools. They are the platform's extensibility mechanism — every third-party integration, every domain-specific tool, every custom capability flows through this surface. The platform manages their full lifecycle: registration, validation, connection, discovery, per-goal assignment, and dispatch-time verification.

**Registration** — external servers are registered at the platform level (MCP Servers view). Each registration specifies a server name, the command to launch it, command arguments, and environment variables. A registered server can be enabled or disabled globally — disabled servers are not available to any goal regardless of per-goal configuration.

**Registration Validation** — the platform validates server configuration at registration time, not at dispatch time. The command must be a valid executable (exists on PATH or is a valid absolute path). Arguments must parse correctly as a structured list. Environment variables must be well-formed key-value pairs. Invalid registrations are rejected with specific error messages explaining what is wrong. The user fixes problems when they create them, not when a task fails minutes later with an opaque error.

**Environment Variables** — external MCP servers frequently require environment variables (API keys, configuration paths, service URLs). The registration surface provides explicit key-value environment variable management. Env vars are stored as part of the server configuration and passed to the server process at launch. This keeps server configuration self-contained within the platform — the operator does not need to set env vars outside the platform for servers to function.

**Connection and Discovery** — when an external server is registered and enabled, the platform can connect to it and discover its tool inventory. The discovered tools are what the user sees when configuring per-goal server assignments. If a server cannot be reached or fails to report its tools, the platform surfaces this state clearly — the user knows which servers are healthy and which are not.

**Per-Goal Assignment** — a goal's `mcp_servers` property controls which registered external servers are connected during that goal's task execution. The configuration surface presents registered servers as selectable options (not free-text). Only servers that are both registered and globally enabled appear as options. The platform's internal MCP server is always connected and is not subject to per-goal selection. The per-goal configuration surface shows each server's current health status (healthy, unreachable, disabled) so the user sees problems before dispatching.

**Pre-Dispatch Health Check** — before a task dispatches, the platform probes all external MCP servers assigned to the task's goal. If any server is unreachable, the task fails immediately with a specific error naming the server and the failure reason (command not found, timeout, handshake failure, disabled). The operator sees exactly what broke and can fix it before retrying. This is a dispatch-time gate, not a background monitor — the check happens at the moment of execution.

**Error Attribution** — when an MCP server fails during task execution, the error is surfaced with the server name and failure mode, not as a generic CLI error. The task detail view shows which external MCP servers were included in the dispatch configuration, so when a task fails the operator can immediately correlate the failure with a specific server.

**Stale Reference Integrity** — when a server is deleted, the platform removes it from all goals' `mcp_servers` lists. A goal configured with a server that no longer exists never silently loses tools — the reference is cleaned up at the source. When a server is disabled, goals referencing it see a visible warning that tools from this server will not be available at dispatch time. Disabled servers are never included in dispatch configuration.

**Tool Surface Composition** — during dispatch, the agent's available tools are the union of: (1) CLI tools selected via `allowed_tools` (or the full default set if empty), (2) internal MCP server tools (always present), and (3) tools from external MCP servers enabled for the goal. The user can see this composed tool surface when configuring a goal — what the agent will actually have access to.

### User Interface

The product presents a persistent command bar, eight views, and a persistent chat surface.

#### Command Bar

The command bar is a persistent operational control surface pinned to the top of every view. It provides the primary interaction point for dispatch and queue management — the controls the user reaches for most often, accessible without navigating to any specific view.

The command bar contains:

- **Goal indicators** — one colored indicator per goal, using the goal's identity color. Each indicator shows the goal's current operational state: idle, has pending tasks, has queued tasks, or has an active (running) task. The indicators provide at-a-glance awareness of which goals have work in the pipeline.

  **Clicking a goal indicator opens a dispatch popout** — a lightweight panel anchored to the indicator that lets the user dispatch a manual task for that goal. The popout provides a context field for the dispatch message and a dispatch action. This is the primary manual dispatch surface — the user triggers work from anywhere in the application without navigating to the Dispatch or Goals view. The popout closes after dispatch or on click-away.

  The dispatch popout replaces inline dispatch on the Goals view. Manual dispatch is an operational action — it belongs on the persistent operational surface, not on the configuration view where it competes with goal properties for attention.

- **Auto-queue toggle** — controls whether newly created tasks skip pending and go directly to queued. This is the same global setting previously located in Settings, elevated to the command bar because it directly governs how every trigger routes into the queue. The toggle provides immediate visual feedback of the current state (auto-queueing on/off).

- **Queue all** — a batch action that transfers all pending tasks to the queued state. This is the batch equivalent of dragging each pending task individually into the Active column. The action is only available when pending tasks exist.

- **Shelve all** — a batch action that transfers all queued tasks back to the pending state. This pulls everything off the execution runway back into the staging area for reconsideration. The action is only available when queued (non-active) tasks exist. It does not affect the currently running task — active execution is cancelled, not shelved.

The command bar's design rationale: the user's primary interaction with mAistro is through dispatches. A summary of goal state plus core dispatch controls pinned to the top aligns the interface with this reality. The user should never need to navigate away from their current view to dispatch a goal, check queue routing, or batch-manage queue state.

#### Views

- **Dispatch** — the operational center. A single three-column kanban that makes the entire task lifecycle visible at once:
  - **Upcoming** (left column) — the staging area. Newly created tasks land here by default. The user reviews, coalesces (merge and split), and curates tasks before promoting them. Pending tasks are not eligible for execution. Tasks are displayed by creation time. This column answers: "what work is waiting for my attention?"
  - **Active** (center column) — the execution pipeline. Contains queued tasks awaiting their turn and the currently running task. The running task (if any) appears at the top of the column, visually distinct from queued tasks below it. The user reorders queued tasks to control execution priority. This column answers: "what is running and what runs next?"
  - **Resolved** (right column) — all terminal states. Every task that has finished — completed, exhausted, failed, timed out, cancelled, interrupted, rejected — lands here. Each card carries a status badge identifying its terminal state. Completed (success) cards display the outcome summary and commit range. Non-success cards surface the error context inline — the user sees *why* it didn't succeed at a glance. Ordered by completion time (most recent first). This column answers: "what happened?"

  Tasks flow left to right through their lifecycle: Upcoming → Active → Resolved. The user drags tasks between Upcoming and Active to promote (pending → queued) or demote (queued → pending). Provides controls for cancelling active tasks, approving/rejecting tasks awaiting approval, and resuming/retrying/replying to resolved tasks.

  **Reply** — the user can reply to a resolved task with additional context or follow-up instructions. Reply creates a new task (via the `reply` trigger) and coalesces the original under it. The reply action requires a text input — the user must provide the follow-up message that becomes the new task's context. This is distinct from resume (which continues the same session without new input) and from session interrogation (which is read-only and does not create a task).

  **Detail drawer** — selecting any task opens a drawer that slides up from the bottom of the view. For active tasks, the drawer shows live streamed output (text, tool use, thinking indicators). For resolved tasks, it shows the stored session output, outcome summary, diff, and execution metadata (stop reason, turns consumed relative to the limit, duration, cost). For upcoming tasks, it shows trigger context and task metadata. For any task that has dispatched or will dispatch with external MCP servers, the drawer shows which servers were included in the dispatch configuration — so when a task fails, the operator can immediately see whether the failure correlates with a server issue. The drawer is resizable — the user controls how much vertical space it occupies. Closing the drawer returns full space to the columns. The drawer keeps the column layout visible above it, preserving spatial context while the user inspects a specific task.
- **Feed** — git history enriched with task metadata. Shows what changed and which tasks produced those changes.
- **Goals** — the goal configuration surface. Goal configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Properties are organized by concern (definition, triggers). Manual dispatch is not on this view — it is on the command bar, where operational actions belong. The Goals view is purely for defining *what* goals are, not for triggering them.
- **Files** — a project file browser. The user searches for files by glob pattern and reads their contents. Markdown files render as formatted documents. Code files render with syntax highlighting for readability. This view provides direct, read-only access to project content without leaving the application.
- **MCP Servers** — tool server management as a dedicated surface. See MCP Servers View below.
- **Dashboard** — aggregated operational visibility. Answers "how are my agents doing?" without requiring the user to inspect individual tasks. Shows goal health, task timing, and coordination patterns across configurable time windows. Read-only — no actions, no state changes. See Activity Dashboard below.
- **Settings** — platform configuration: default model, default timeout.
- **Chat** — a persistent, resizable tray providing interactive conversation with the LLM in the project context.

A notification indicator (badge with unread count) provides ambient awareness of events that need attention. The indicator is persistent — visible from any view, not tied to a specific navigation item. Activating the indicator reveals a notification feed (dropdown or panel) without navigating away from the current view. Notifications are a cross-cutting concern, not a destination.

### Contextual Help (Tooltips)

Configuration fields that involve syntax rules, non-obvious behavior, or domain-specific concepts provide hover tooltips. The tooltip appears on a help indicator adjacent to the field label — not on the input itself — so it does not interfere with interaction.

Tooltips explain *rules and behavior*, not just labels. They answer: "what do I type here?" and "what will this do?"

Required tooltip surfaces:

- **Subscriptions (glob patterns)** — syntax: `*` matches files in one directory, `**` matches recursively across directories. One pattern per line. Dual purpose: patterns determine which commits trigger the goal *and* which files are included as context in the task prompt.
- **Schedule (cron expression)** — five-field format: `minute hour day-of-month month day-of-week`. Ranges (`1-5`), lists (`0,15,30`), steps (`*/10`), and wildcards (`*`). Examples: `*/30 * * * *` (every 30 min), `0 9 * * 1-5` (weekdays at 9am). First evaluation after setting a schedule establishes a baseline — does not fire immediately.
- **Allowed Tools** — select which CLI tools the agent can use. The platform presents the full inventory of available tools; the user selects from this list. When any tools are selected, the agent sees only those tools plus tools from connected MCP servers. When none are selected, the agent gets the full default tool set. Tools not selected are removed from the agent's environment entirely — the agent has no awareness they exist.
- **Allowed Internal Tools** — select which internal MCP tools the agent can access. When any are selected, only those internal tools are presented. When none are selected, all internal tools are available. Use this to create read-only goals (restrict to `list_files`, `read_file`, `git_log`, `git_diff`) or to grant branch management tools (`git_branch_create`, `git_branch_switch`, `git_branch_merge`) only to orchestrator goals.
- **Allowed Dispatch Targets** — select which goals this agent can programmatically dispatch via the `dispatch_task` tool. When none are selected, the agent cannot dispatch other goals. This prevents unconstrained cross-agent triggering.
- **MCP Servers (per-goal)** — select which registered external MCP servers this goal's agent can connect to. Only checked servers are available during dispatch. The platform's internal server (git operations, file access) is always connected. Each server in the selection list shows its current health status (healthy, unreachable, disabled) — the user sees problems before dispatching, not after. Register servers in the MCP Servers view first, then enable them here per-goal.
- **Require Approval** — when enabled, automated triggers (commit-watch, schedule, dependency) produce tasks that wait for manual approval before executing. Manual dispatches bypass this gate.
- **Coalesce Dispatches** — when enabled, the goal will never have more than one pending task. Any new trigger merges into the existing pending task instead of creating a new queue entry. Useful for goals that should catch up in one run rather than queuing redundant work.
- **Dependencies** — the goal auto-dispatches when *any* selected upstream goal completes successfully. Circular chains are allowed — coalescing prevents runaway queuing. Timed-out, failed, or cancelled tasks do not trigger dependents.
- **Timeout** — maximum execution time in seconds. When reached, the platform gracefully terminates the agent, then force-kills if it does not exit. Timed-out tasks do not trigger downstream dependencies. Set to 0 for no limit.
- **Max Turns** — maximum number of agent turns before the session is stopped. A turn is one cycle of reasoning and output. Most tasks complete in well under the limit. When reached, the task is marked as exhausted (not completed) — the agent was cut off, not done. Exhausted tasks do not trigger downstream dependencies. Increase the limit if a goal consistently needs more interaction, or tighten the instructions if the agent is doing unnecessary work.
- **Auto-queueing (Command Bar)** — when enabled, newly created tasks skip the pending column and go directly to queued, where the worker will pick them up. When disabled, all new tasks enter the pending column and must be manually transferred to queued before they can execute. The worker always runs — auto-queueing only controls the initial routing of new tasks. The user can override any individual task by dragging it between columns after creation.
- **Model** — the LLM model for this goal. Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency goals.

### MCP Servers View

MCP server configuration is a first-class surface with its own rail item. It is not buried in Settings. The reason: MCP servers are how users extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view must be simple enough that a user who has never configured an MCP server can succeed on the first attempt.

**Design principles:**

- **Progressive disclosure.** The empty state is not a blank page — it explains what MCP servers are, why you might add one, and how to do it. Configuration complexity is revealed only as the user engages. A user with zero servers sees guidance. A user with three servers sees status and management.
- **Guided registration.** Adding a server requires a name and a command. Arguments and environment variables are optional but first-class. The form makes each field obvious — labeled inputs with placeholder examples that show real, working patterns (e.g., command: `npx`, args: `@modelcontextprotocol/server-filesystem`, `/path/to/dir`). Arguments are a structured list — each argument is a discrete entry, not a space-separated text field. This eliminates the class of errors where quoting or spaces produce wrong arguments. Environment variables are key-value pairs with add/remove controls. The form validates before submission: the command must be a resolvable executable, args and env vars must parse correctly. Validation errors explain what went wrong in plain language.
- **Immediate feedback.** After adding a server, the platform tests connectivity automatically and shows the result — healthy with a tool count, or an error with a plain-language explanation. The user never wonders "did it work?" The test action is also available on demand for any registered server.
- **Health at a glance.** Each server shows its current state: enabled/disabled, healthy/unreachable, and the tools it provides. The user can scan the list and immediately understand which servers are working.
- **Safe defaults.** New servers are enabled by default — the most common intent when adding a server is to use it. Disabling is a deliberate choice the user makes later if needed.

**What the view shows:**

- The list of registered servers, each displaying: name, command, arguments, environment variable count, enabled state, health status, and discovered tools (when healthy).
- A registration form for adding new servers.
- Per-server actions: enable/disable toggle, test connection, edit configuration, remove. Editing allows changing the command, arguments, and environment variables after registration.
- When a server is unhealthy, the error is shown inline — not hidden behind a click. Error messages should help the user fix the problem: "command not found" means the command isn't installed or isn't on PATH; "connection refused" means the server started but isn't responding correctly; "handshake failed" means the process started but didn't respond with a valid MCP protocol exchange.
- When a server is deleted, a confirmation shows which goals currently reference it. After deletion, the server is removed from all goals' `mcp_servers` lists — no stale references remain.

**What the view does not do:**

- No per-goal assignment. That stays on the goal configuration surface where it belongs — the MCP Servers view manages the global registry; goals select from it.

**Relationship to goal configuration:** The MCP Servers view is where servers are registered and managed. The goal configuration surface (Goals view) is where servers are assigned to specific goals via the `mcp_servers` property. The per-goal tooltip directs users to the MCP Servers view when they need to register new servers. This separation keeps each surface focused: one place to manage servers, another to assign them.

### Activity Dashboard

The Dispatch view shows individual tasks — what's running, what happened. As task volume grows through automated triggers, scheduled goals, and agent-initiated dispatches, the user needs aggregated visibility: patterns across tasks, not just the tasks themselves. The Activity Dashboard is this surface.

**What the dashboard answers:**

- "How are my agents doing?" — per-goal success and failure rates over time.
- "What needs attention?" — recurring failures, timeout patterns, goals that consistently underperform.
- "When did things run?" — temporal patterns in task execution: scheduling conflicts, long-running outliers, idle gaps.
- "How do agents coordinate?" — which goals dispatch other goals, how deep chains go, where coordination breaks down.

**The dashboard is read-only.** It does not create, modify, or dispatch anything. It aggregates existing data from the `tasks` table and MCP tool call logs. No schema changes, no new data collection — the platform already records everything the dashboard needs. The dashboard is a lens on data that exists.

#### Goal Health Summary

Each goal shows a health summary across a selectable time window (today, 7 days, 30 days):

- **Task counts** — total tasks completed, failed, timed out, cancelled, interrupted, rejected. The breakdown by terminal state is essential — a goal with 10 failures and 10 timeouts has two different problems.
- **Success rate** — completed tasks as a proportion of all terminal tasks. This is the single number that captures goal health. A goal running at 70% success needs investigation; one at 95% is healthy.
- **Trend indicator** — whether the success rate is improving, stable, or degrading compared to the previous equivalent window. The user needs to know not just current health but direction.

Goals with low success rates or degrading trends are visually prominent — the dashboard surfaces what needs attention without the user hunting for it. The ordering and emphasis are health-driven, not alphabetical.

#### Timeline

A temporal view of task execution: when tasks ran and how long they took.

- **Horizontal bars** per task, positioned by start time and sized by duration. Color-coded by goal. The user sees scheduling density, idle gaps, and outliers at a glance.
- **No chart library.** The timeline is rendered with basic HTML/CSS — positioned elements, not SVG or canvas. This keeps the implementation minimal and the rendering predictable.
- **Configurable window** — the same time windows as the health summary (today, 7d, 30d). The timeline and health summary share a time selector so the user sees consistent data.
- **Long-running outliers** are visually distinct. A task that took 10x the goal's median duration stands out without the user calculating.

The timeline reveals patterns that individual task inspection cannot: bunching (too many tasks in a window), gaps (periods of no activity when activity was expected), and overlap awareness (even though execution is sequential, queued-at times reveal demand patterns).

#### Agent Dispatch Chains

When agents dispatch other agents, the resulting chains are a new dimension of system behavior that is invisible in the Dispatch view's flat task list.

- **Dispatch graph** — for each agent-initiated task, show the chain: which task dispatched it, which task dispatched that one, back to the original trigger. This is a tree, not a cycle — each task has at most one originating task.
- **Chain depth** — how many levels deep agent-initiated dispatches go. A chain of depth 1 (agent dispatches one task) is normal coordination. Depth 3+ may indicate emergent behavior worth inspecting.
- **Goal-to-goal patterns** — which goals dispatch which other goals, aggregated over time. This reveals the coordination topology: "Architect always dispatches Engineer," "Engineer never dispatches anything." The user understands agent relationships without reading individual task histories.

This surface makes the `agent` trigger type legible. Without it, agent coordination is an invisible graph embedded in trigger metadata.

#### Tool Usage Patterns

Each goal's agent uses tools differently. The audit trail (MCP tool call logs) already records every tool invocation. The dashboard surfaces patterns:

- **Per-goal tool frequency** — which internal MCP tools each goal uses most. A goal that calls `git_commit` 20 times per task works differently than one that calls it once. A goal that never uses `read_file` despite having file subscriptions may have misconfigured instructions.
- **Tool errors** — tool calls that return errors, aggregated by goal and tool. Persistent tool errors indicate a configuration or instruction problem.

Tool patterns are secondary to health and timing — they support investigation, not triage. The user notices a goal has low success rates (health summary), checks when it runs (timeline), then looks at what it does (tool patterns) to diagnose the problem.

#### Design Principles

- **Aggregation, not raw data.** The dashboard never shows individual task records — that's what the Dispatch view does. Every element is a summary, a count, a rate, or a pattern derived from multiple tasks.
- **Time-windowed.** All data is scoped to a configurable time window. The dashboard shows the recent picture, not all-time history. The time selector is global to the view — health summary, timeline, and dispatch chains all respond to the same window.
- **Health-driven emphasis.** Goals that need attention are visually prominent. A healthy system fades into the background; problems surface. This is the opposite of a status board that treats everything equally.
- **Derived from existing data.** The dashboard reads from `tasks` (lifecycle timestamps, trigger metadata, goal associations) and MCP tool call session events (tool names, results). It introduces no new data collection, no new tables, no new event types. If the data doesn't already exist, the dashboard doesn't show it.
- **No actions.** The dashboard is purely informational. The user cannot dispatch, cancel, retry, or configure from the dashboard. Actions belong on the surfaces designed for them (Dispatch, Goals). The dashboard informs decisions; other views execute them.

---

## Constraints

### Operational Integrity

- **Queue-first invariant**: every dispatch passes through the queue as a task before execution. All code paths — manual, watch, schedule, dependency — enqueue first, then execute. Tasks enter as either pending or queued (determined by auto-queueing setting) but always exist as queue records before execution.
- **Two-stage progression**: tasks must be in the queued state before the worker will pick them up. Pending tasks are invisible to the worker. This ensures the user always has an opportunity to review and curate work before it executes (unless auto-queueing is deliberately enabled).
- **Sequential execution**: exactly one task runs at a time. The worker holds a lock during processing.
- **Project isolation**: each project has its own SQLite database. The app-level database holds only the recent-projects list.
- **Git is content source-of-truth**: all project content lives in git. The SQLite database holds only operational state (goal configs, task records, chat sessions).
- **Single active project**: the platform operates on one project at a time. The active project is global state that all operations reference.
- **Dashboard is read-only**: the Activity Dashboard performs only read queries on existing data. It introduces no new tables, no new event types, and no write operations. All aggregations derive from `tasks` lifecycle columns and MCP tool call session events that already exist.

### Data Integrity

- **Goal identity is immutable**: a goal's slug ID, once derived from its initial name, stays constant. All references (tasks, properties, dependencies) use the slug. Renaming changes only the display label.
- **Task lifecycle is monotonic**: a task progresses from created → queued → started → terminal. Terminal states are: completed (success), exhausted (turn limit), failed, timed out, cancelled, interrupted, or rejected. A task may skip pending (via auto-queueing) or move back from queued to pending (via manual transfer), but once started, progression is forward-only. Retry creates a new cycle by resetting lifecycle fields on the same record, preserving task identity.
- **Trigger context is immutable at enqueue time**: each trigger entry's context string is built when the trigger fires. This preserves the causal record — the prompt reflects what was true when the trigger occurred.
- **Goal deletion cascades**: removing a goal removes all associated data (properties, tasks, sessions). This prevents orphaned records.
- **Task status is authoritative**: each task has a well-defined status that progresses through a validated state machine. Status transitions are enforced — invalid transitions (e.g., pending directly to completed, or any transition out of a terminal state) are rejected. The status is the single source of truth for where a task is in its lifecycle. Lifecycle timestamps record *when* transitions happened; the status records *where the task is now*.
- **Outcome summaries are derived from git**: the summary is computed from commits between `start_commit` and `result_commit`. It reflects what the repository records, not what the agent claims. A task that produces no commits has no summary.
- **Goal color is non-null**: every goal has a color from creation. The platform assigns a random color from a curated palette when a goal is created. The palette is chosen for visual distinguishability — high saturation, evenly distributed hues, readable against both light and dark backgrounds. The user can override the color at any time. Color has no behavioral effect — it is purely visual metadata.
- **Merge preserves trigger history**: merging pending tasks concatenates their trigger arrays. No trigger entry is lost or rewritten. The surviving task's triggers are the union of all source tasks' triggers, ordered by original creation time.
- **Split produces valid tasks**: each task created by split carries exactly one trigger entry from the original. The original task retains its first trigger and identity; new tasks get fresh IDs and are appended to the pending column.
- **Coalescing belongs to Upcoming**: merge and split operate exclusively on pending tasks in the Upcoming column. This is the curation stage where work is grouped and decomposed. Once a task is promoted to Active, its composition is fixed — the Active column is for prioritization and execution, not restructuring.
- **Sorting belongs to Active**: reordering operates exclusively on queued tasks in the Active column. Queue position determines execution priority. The Upcoming column displays tasks by creation time — it has no user-controlled sort order.
- **Cross-column drag is transfer only**: dragging between Upcoming and Active changes state (pending ↔ queued) without merging or reordering within the target column. Transfer and composition/sorting are distinct user intentions that must not be conflated in a single gesture.
- **Same-goal constraint on merge**: only tasks belonging to the same goal can be merged. A task's identity is bound to one goal; cross-goal merging would violate prompt assembly, tool configuration, and commit authorship invariants. The UI enforces this structurally — the merge affordance does not appear when tasks belong to different goals, so the invalid operation is never offered.

### External MCP Integrity

- **Registration validates eagerly**: an external MCP server registration is rejected if the command is not a resolvable executable or if arguments and environment variables do not parse correctly. Invalid configuration never reaches the database — errors surface at the moment the user submits the form, not when a task dispatches minutes or hours later.
- **Dispatch gates on server health**: before a task begins execution, all external MCP servers assigned to its goal are probed. If any server is unreachable, the task fails with a specific error naming the server and the failure reason. A task never runs with a silently missing tool surface.
- **Disabled servers are never dispatched**: a disabled server is excluded from dispatch configuration unconditionally. The `enabled` flag is authoritative — there is no default-to-enabled fallback for missing or ambiguous state. Goals referencing a disabled server see a visible warning on their configuration surface.
- **Server deletion cascades to goal references**: deleting a server removes it from every goal's `mcp_servers` list. No goal silently loses tools because it references a server that no longer exists — the reference is cleaned up atomically with the deletion.
- **MCP errors are attributed to their source**: when an MCP server failure occurs during task execution, the error identifies the server by name and describes the failure mode. Generic CLI errors that originate from MCP server failures are enriched with server-specific context before being surfaced to the user.
- **Config assembly validates before dispatch**: the assembled MCP configuration — the composite of all servers assigned to a goal — is validated as well-formed JSON before being written to disk and passed to the CLI. Malformed arguments, invalid environment variable structures, or any composition error is caught at assembly time with a specific error naming the problematic server, not propagated as an opaque CLI failure. This is defense-in-depth: registration validates individual server configs; assembly validates the composed whole.
- **Server configuration is self-contained**: environment variables required by a server are stored as part of the server's registration, not as external system state. The platform passes them to the server process at launch. A server registration contains everything needed to launch and connect to the server.

### Accountability

- **Tool mediation is observable**: every tool call that flows through the internal MCP server is logged as a structured event. The platform can reconstruct exactly what an agent did, not just what it produced.
- **Context-aware tool surfaces**: the set of tools available to an agent is determined by the goal's configuration, not by the agent's own choices. The platform controls what actions are possible. This applies to all three tool dimensions: CLI tools (`allowed_tools`), internal MCP tools (`allowed_internal_tools`), and external MCP servers (`mcp_servers`).
- **Tool restrictions are invisible to agents**: an agent only sees tools it is allowed to use. Tools outside the allowed set are removed from the agent's environment — not mentioned, not instructed against, not present. A headless agent has no mechanism to negotiate access; presenting tools it cannot use would only produce failed attempts or prompt-level workarounds. The platform owns the restriction surface; the agent owns only its allowed capabilities.
- **Tool inventory is discoverable**: the platform presents the complete set of available tools — built-in CLI tools, internal MCP tools, and external MCP server tools — so the user configures from known options rather than guessing. Tool configuration surfaces select from what exists; they do not accept arbitrary text that may not correspond to real tools.
- **Dispatch attribution**: tasks created by agents via `dispatch_task` are tagged with the originating task, creating a provenance chain visible in history. The user can trace any agent-dispatched task back to the task that requested it.
- **Branch operations are auditable**: all git branch tool calls (create, switch, merge) are logged in the task session like any other MCP tool call. The platform can reconstruct which branches a task created, operated on, and merged.

### Safety

- **Active task locks project state**: while a task is running, the platform keeps the project directory unchanged. This prevents state corruption from changing the working directory mid-execution.
- **Stale sweep on startup**: any task marked as in-flight when the process starts is marked interrupted. This eliminates zombie tasks.
- **Approval gates apply to all automated triggers**: manual dispatch (explicit human intent) bypasses the approval check; all other trigger types — including `agent` triggers — respect `require_approval`.
- **Timeouts are enforced**: every task has a configurable timeout (inherited from its goal). The watchdog runs unconditionally. Goals have bounded execution time.
- **Dependency cycles coalesce gracefully**: circular dependency chains produce redundant triggers that coalescing absorbs, preventing unbounded task growth.
- **Post-commit hook is non-blocking**: the hook runs asynchronously and fails silently. Hook failures never prevent or delay git operations.
- **Session interrogation is read-only**: resumed task sessions for interrogation strip all write tools. The agent can read and reason but cannot modify the project. This preserves the queue-first invariant — all modifications flow through the task queue.
- **Agent dispatch is governed**: an agent can only dispatch tasks for goals listed in its `allowed_dispatch_targets`. No self-dispatch. A depth limit on agent-initiated dispatch chains prevents runaway cascades. Coalescing absorbs redundant agent-triggered enqueues.
- **Branch operations are explicit**: branch creation, switching, and merging are structured tool calls — not unmediated shell commands. Merge conflicts surface as structured output, not silent failures. Naming conventions on branch creation prevent namespace collisions between goals.
- **Notifications are read-only signals**: the notification system observes task lifecycle events but never creates, modifies, or dispatches tasks. Notifications inform the operator; actions happen on the surfaces designed for them (Dispatch, Goals). Webhook payloads carry event data but do not accept inbound commands.
- **Notification defaults are conservative**: the platform defaults to notifying on failures, timeouts, and approval requests — events that typically require operator response. The operator opts into higher-volume notifications (all completions) deliberately. Desktop notifications require explicit browser permission.
