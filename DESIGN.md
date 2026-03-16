# Design

## What mAistro Is

mAistro is a development engine where LLM-powered agents coordinate through git. The user defines jobs — configurable units of autonomous work — and the platform dispatches them as tasks, streams their output, and records what they did. The app itself is the agent; jobs define what work exists, tasks are specific executions of that work.

The user points mAistro at a project directory. From that point, the platform owns the operational loop: triggering tasks, building prompts, invoking the LLM, and letting the LLM commit its own changes. The user's role shifts from writing code to defining what work should happen, when, and under what constraints.

### Naming

Two concepts, precisely defined:

- **Job** — a named, persistent configuration of autonomous work. The user creates, edits, and deletes jobs. Jobs define *what* work exists: instructions, model, subscriptions, dependencies, schedule. Jobs are the nouns of the system.
- **Task** — a scoped, contextualized execution of a job. When a job is dispatched (by any trigger), it produces a task in the dispatch queue. Tasks have lifecycle (pending, active, completed, failed), trigger context, and linked output. Tasks are the verbs of the system.

A job is a template. A task is an instance. One job produces many tasks over time.

---

## Requirements

### Projects

- The user opens a project directory. The platform initializes it for use: ensures a git repository exists, installs the post-commit hook, manages gitignore entries, creates the `.maistro/` directory (gitignored) with an operational database inside it.
- Opening a project makes it the active context. All dispatch, job, and chat operations target the active project.
- Switching projects closes the current context and opens a new one. A project switch is blocked while any task is active.
- The platform maintains a recent-projects list in a separate app-level store, enabling quick project switching.

### Jobs

- A job is a named, configurable unit of work. It has a short description, detailed instructions (the prompt body), a model, and behavioral properties.
- Job identity is derived from the name (slugified). The name is the human-facing label; the slug is the system-facing key. Once created, the slug is immutable — renaming a job does not change its identity.
- Jobs are ordered. The user controls sort order by dragging jobs into position.
- Job deletion cascades: removing a job removes its properties, dispatch history, and chat sessions.

### Job Properties

- Properties follow an entity-attribute-value pattern: a registry of property definitions (with types and defaults) and per-job overrides.
- Property types: `string`, `json`, `integer`, `boolean`. The platform casts stored strings to the declared type on read.
- Core properties: `description`, `instructions`, `model`, `subscriptions`, `depends_on`, `schedule`, `timeout`, `require_approval`, `coalesce_dispatches`, `allowed_tools`, `mcp_servers`, `sort_order`.

### Dispatch

- Every dispatch — regardless of trigger — creates a task in the dispatch queue before execution. The queue is the single entry point to the execution engine.
- The background worker pulls from the queue and processes tasks sequentially (one at a time).
- The queue operates in two modes: **auto-processing** (worker continuously pulls and executes pending tasks in order) and **paused** (tasks accumulate as pending; the user reorders them, then toggles auto-processing when ready). A global setting controls which mode is active.
- Each task records its full lifecycle: `created_at`, `started_at`, `completed_at`, `error`. A task that has started but not completed is "active." A task with an error is "failed."
- On startup, the worker sweeps any tasks that were active when the process died and marks them as interrupted.

### Task Ordering in the Queue

- Pending tasks in the dispatch queue have an explicit execution order. In auto-processing mode, the worker pulls the highest-priority pending task (lowest order position).
- The user controls task execution order by dragging pending tasks into position within the Dispatch view. This determines which task runs next when the current one completes.
- Reordering applies only to pending tasks. Active and completed tasks are not reorderable.
- Newly enqueued tasks are appended to the end of the pending queue by default.

### Triggers

Four trigger types cause tasks to be enqueued:

- **Manual** — the user explicitly dispatches a job. Always creates a new task. Never coalesces. Bypasses approval gates.
- **Commit (watch)** — a git post-commit hook notifies the platform. Jobs whose subscription glob patterns match changed files are enqueued as tasks. Coalesces with other pending commit-triggered tasks for the same job.
- **Schedule** — cron expressions evaluated by a background scheduler. Enqueues a task when the expression fires. Always coalesces globally (repeated fires while a task is pending produce one run, not many). The first evaluation after a schedule is set establishes a baseline without firing — a newly configured schedule does not immediately dispatch.
- **Dependency** — when a task completes successfully, jobs declaring its job as an upstream dependency are enqueued. Coalesces with other pending dependency-triggered tasks for the same job. Timed-out, failed, or cancelled tasks do not trigger dependents.

Two continuation triggers operate on existing tasks:

- **Resume** — continues a previous task using the CLI's session resume capability. Never coalesces.
- **Retry** — re-enqueues a failed or timed-out task. Resurrects the original record in-place (resets lifecycle fields and created_at to maintain fair queue ordering).

### Coalescing

Coalescing prevents redundant pending tasks. Two mechanisms exist: **automatic coalescing** (at enqueue time, driven by trigger rules) and **manual coalescing** (user-initiated, from the queue).

#### Automatic Coalescing

- When a new trigger would create a task but a compatible pending task already exists, the trigger is appended to the existing task's trigger list instead of creating a new record.
- `commit` and `dependency` triggers coalesce with other pending tasks of the same trigger type for the same job.
- `schedule` triggers coalesce globally (any pending task for the same job absorbs the new trigger).
- The `coalesce_dispatches` job property enables global coalescing for all trigger types — the job will never have more than one pending task.
- `manual`, `resume`, and `retry` never coalesce — each represents distinct explicit intent.

#### Manual Queue Composition

The user can merge and split pending tasks directly from the Dispatch view. This gives explicit control over the grouping that automatic coalescing performs implicitly.

**Merge** — the user drags a pending task directly onto another pending task for the same job. The two tasks combine into a single task. All trigger entries from both tasks are collected into the surviving task's `triggers` array. The older task (by `created_at`) survives; the dragged task is removed from the queue. The surviving task retains its queue position.

- Only pending tasks can be merged (not started, not completed, not pending-approval).
- Only tasks belonging to the same job can be merged. Merging across jobs would produce a task with ambiguous identity — one task cannot represent two jobs. The UI enforces this by suppressing the merge affordance when the dragged task and the drop target belong to different jobs.
- The merge operation is the manual equivalent of what automatic coalescing does at enqueue time: multiple reasons to run become one run that addresses all of them.
- Trigger context is preserved verbatim. Each trigger entry retains the context string it was created with — merge does not rewrite history.

**Drag Interaction Model** — reorder and merge share a single drag gesture, disambiguated by drop position:

- **Reorder zone** — the upper and lower edges of each task row (the gaps between tasks). Dropping here inserts the dragged task at that position. Visual feedback: an insertion line between tasks.
- **Merge zone** — the central area of a task row. Dropping here merges the dragged task into the drop target. Visual feedback: the target task highlights with a merge indicator. The merge zone activates only when the drop target is a pending task belonging to the same job as the dragged task. When the same-job condition is not met, the central zone behaves as a reorder zone — no merge affordance is shown.
- The tolerance split (how much of the row is merge zone vs. reorder zone) is a UI tuning parameter, not a design constant. The essential contract: the user's spatial intent — "place between" vs. "place onto" — determines whether the operation is reorder or merge.

**Split** — the user takes a pending task that has multiple trigger entries and breaks it into individual tasks, one per trigger. The original task keeps its first trigger entry and queue position; new tasks are created for each remaining trigger and appended to the end of the pending queue.

- Only pending tasks with more than one trigger entry can be split.
- Each resulting task is an independent queue entry with its own lifecycle.
- Split reverses a previous merge or automatic coalescing. The user can inspect the accumulated triggers on a task and decide they should run separately.
- New tasks created by split inherit the job's current approval gate setting. If `require_approval` is enabled, split-off tasks enter pending-approval state.

**Why same-job only**: a task's identity is bound to exactly one job. The task record carries a `task_id` (the job slug), and prompt assembly, subscriptions, tool configuration, and commit authorship all derive from that single job. Merging tasks across jobs would require either a compound identity (one task, two jobs — breaks prompt assembly, tool surfaces, authorship) or a synthetic super-job (implicit, unmanageable). Neither is coherent. The constraint preserves the foundational invariant: one task, one job, one execution context.

### Subscriptions

Subscriptions are glob patterns (supporting `**` recursion) that define a job's **relevant files**. They serve two purposes:

- **Context injection** — every task resolves the job's subscription patterns and lists matching files in the prompt as "files relevant to your task." The job's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering** — when a commit changes files matching a job's subscription patterns, the job is enqueued as a task. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A job with non-empty subscriptions is watch-active. There is no separate toggle.

A job can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

### Dependencies

- Jobs declare upstream dependencies via `depends_on` (a list of job IDs). When an upstream job's task completes successfully, dependent jobs are auto-enqueued.
- Circular dependency chains are permitted. Coalescing and sequential execution absorb redundant triggers — a job that already has a pending task absorbs the new trigger rather than creating an infinite queue.
- Dependency context includes the upstream job name, task ID, and commit range.

### Approval Gates

- Jobs with `require_approval` enabled produce tasks with `approval='pending'` status (except manual dispatches, which bypass the gate).
- The worker skips pending-approval tasks. A user must explicitly approve or reject.
- Rejection marks the task as completed with an error of "rejected."

### Timeout Enforcement

- Each job has a configurable timeout (in seconds, default 900).
- A watchdog cancels the CLI subprocess after the timeout elapses. The task is marked as timed out. Timed-out tasks do not trigger dependents.
- Cancellation is graceful: the platform signals termination, waits a grace period, then forces termination if the process has not exited.

### Prompt Assembly

- The platform builds two prompts per task: a **system prompt** (execution mode, documentation principles, git workflow, working directory) and a **user prompt** (job identity, instructions, invocation context, job registry, subscribed files, action directive).
- The system prompt establishes headless autonomous behavior: no questions, no clarification requests, commit-based workflow.
- The user prompt layers job-specific instructions with runtime context (why this task was triggered, what other jobs exist, which files are subscribed).
- The job registry — a manifest of all jobs with their names, descriptions, and subscriptions — is included in every task prompt. Agents know what other agents exist.
- Agents commit their own changes. The platform does not auto-commit.

### Chat

- Every task creates a linked chat session for durable output storage. The session holds the agent's streamed text as chat messages and raw events as an audit trail.
- A standalone chat interface provides direct conversation with the LLM in the project context. Its system prompt is built dynamically from live project state — job list, running tasks, recent queue activity, git history — so the LLM has ambient awareness of what the platform is doing.
- Chat sessions persist across application restarts. Sessions can be resumed through the CLI's session continuation capability.

### Streaming

- Task output streams to the frontend via Server-Sent Events (SSE). Event types: `text`, `tool_use`, `result`, `error`, `session_id`.
- Live subscribers receive events in real time. The stored session provides the same content for later retrieval.

### Dispatch Outcomes

When a task completes, the platform must make the result understandable without requiring the user to read the full streamed output.

- **Outcome summary** — the platform derives a short summary from the commits produced during the task's execution window (between `start_commit` and `result_commit`). The summary captures what the agent actually did — commit messages and change statistics — not a restatement of the instructions. Displayed inline in the Dispatch view so users can scan completed tasks at a glance.
- **Task rating** — the user can rate a completed task (positive or negative). This is a simple binary signal stored on the task record. Ratings create a feedback dataset that can later be correlated with job instructions, model choices, and trigger patterns. No rating is required — unrated tasks are the default.
- **Outcome in dependency context** — when a completed task triggers downstream dependents, the outcome summary is included in the trigger context passed to the dependent task's prompt. This gives downstream agents concrete information about what their upstream actually produced, not just that it completed.

The outcome summary is derived, not authored. The platform computes it from git artifacts that already exist. The user does not write summaries; the agent does not produce them explicitly. The platform reads what happened and describes it.

### Git Integration

- The platform installs a post-commit hook in the project directory to notify the backend of new commits, enabling watch triggers. The hook is asynchronous and fails silently — hook failures do not affect git operations.
- The platform reads git state (log, diff, head hash, changed files). Only agents commit changes through MCP tools.
- Task diffs are tracked via `start_commit` and `result_commit` for before/after comparison.

### Platform-Mediated Tool Access

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call flows through platform code, making actions observable, auditable, and policy-governed.

- The server is context-aware: it reads the dispatching job's configuration and presents only relevant tools. Different jobs get different tool surfaces based on their properties and subscriptions.
- **Git operations as structured tools** — `git_commit`, `git_diff`, `git_log`, `git_status` with enforced conventions (commit authorship, message format). These replace unmediated shell-based git access.
- **Read-only project context tools** — file listing, file reading, and job information retrieval, scoped by the job's subscriptions and configuration.
- **Tool invocation logging** — every MCP tool call is recorded as a structured event in the task session, creating an audit trail richer than NDJSON stream parsing.
- Agents retain access to native CLI tools (including Bash) alongside MCP tools. MCP tools are structured alternatives, not an exclusive replacement.

### Tool Configuration

Each job configures what tools its agent can access:

- **Allowed Tools** (`allowed_tools`) — the CLI tool names the agent can use (e.g. `Read`, `Edit`, `Bash`, `Write`). When set, the agent is restricted to exactly these tools plus any tools from connected MCP servers. When empty, the agent gets the default tool set.
- **MCP Servers** (`mcp_servers`) — selects which registered external MCP servers are available to this job's agent. Only servers listed here (and enabled globally) are connected during dispatch. The platform's internal MCP server is always connected.

Tool configuration is **opt-in only**. The user defines what a job *can* do — one concept, one property. There is no complementary "disallowed" property. The platform computes the inverse internally: tools not in the allowed set are removed from the agent's environment entirely. The agent never sees them, never reasons about them, never attempts to work around restrictions.

This is not a stylistic choice — it follows from headless execution. A headless agent cannot ask for permissions, cannot negotiate tool access, cannot be told "you're not allowed to use X" in a meaningful way. The only coherent model is to present exactly the tools the agent can use and nothing else. The restriction surface is the platform's responsibility, not the agent's.

### Tool Discoverability

The platform must make the available tool inventory visible and selectable. A user configuring a job's tools should never need to guess names, remember spelling, or consult external documentation.

Three tool sources exist, each requiring discovery:

- **Built-in CLI tools** — the default tools provided by the Claude CLI (e.g. `Read`, `Edit`, `Write`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `Agent`, `NotebookEdit`). The platform queries the CLI for its available tool list and presents them. These are the tools that `allowed_tools` selects from.
- **Internal MCP tools** — the tools hosted by the platform's own MCP server (`git_commit`, `git_diff`, `git_log`, `git_status`, `list_files`, `read_file`, `list_tasks`). The platform knows these directly — it defines them. They are always available and not subject to per-job selection (the internal server is always connected).
- **External MCP server tools** — tools provided by registered external servers. When a server is registered and enabled, the platform connects to it and discovers its tool list. These tools become visible in the per-job configuration surface alongside built-in tools.

The configuration surface for `allowed_tools` presents the full inventory of available tools as a selectable list — checkboxes, multi-select, or equivalent. The user selects from available options.

This is a discoverability requirement, not a UI prescription. The essential behavior: the user sees what is available and selects what they want.

### External MCP Servers

External MCP servers extend the tool surface available to agents beyond the platform's built-in and internal tools. The platform manages their full lifecycle: registration, connection, discovery, per-job assignment.

**Registration** — external servers are registered at the platform level (MCP Servers view). Each registration specifies a server name, the command to launch it, and command arguments. A registered server can be enabled or disabled globally — disabled servers are not available to any job regardless of per-job configuration.

**Connection and Discovery** — when an external server is registered and enabled, the platform can connect to it and discover its tool inventory. The discovered tools are what the user sees when configuring per-job server assignments. If a server cannot be reached or fails to report its tools, the platform surfaces this state clearly — the user knows which servers are healthy and which are not.

**Per-Job Assignment** — a job's `mcp_servers` property controls which registered external servers are connected during that job's task execution. The configuration surface presents registered servers as selectable options (not free-text). Only servers that are both registered and globally enabled appear as options. The platform's internal MCP server is always connected and is not subject to per-job selection.

**Tool Surface Composition** — during dispatch, the agent's available tools are the union of: (1) CLI tools selected via `allowed_tools` (or the full default set if empty), (2) internal MCP server tools (always present), and (3) tools from external MCP servers enabled for the job. The user can see this composed tool surface when configuring a job — what the agent will actually have access to.

### User Interface

The product presents seven views and a persistent chat surface:

- **Dispatch** — the operational center. Shows pending, active, and completed tasks with outcome summaries for completed work. Provides controls for cancelling, approving/rejecting, resuming, retrying, and rating completed tasks. Dragging pending tasks serves dual purpose — reorder or merge — distinguished by drop position tolerance. When the user drags a task mostly between two other tasks (near the gap), the system shows a reorder indicator and inserts the task at that position. When the user drags a task directly onto another task (within the task's central zone), the system shows a merge indicator and combines the two tasks. Merge is only available when both tasks belong to the same job — dragging onto a task from a different job shows no merge affordance and falls back to reorder behavior. Split breaks a multi-trigger pending task into individual tasks. Selecting a task shows its streamed output.
- **Feed** — git history enriched with task metadata. Shows what changed and which tasks produced those changes.
- **Jobs** — the primary configuration and dispatch surface. Job configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Properties are organized by concern (definition, triggers). Inline dispatch for immediate execution — the most direct way to trigger work.
- **Files** — a project file browser. The user searches for files by glob pattern and reads their contents. Markdown files render as formatted documents. Code files render with syntax highlighting for readability. This view provides direct, read-only access to project content without leaving the application.
- **MCP Servers** — tool server management as a dedicated surface. See MCP Servers View below.
- **Settings** — platform configuration: queue processing mode, default model, default timeout.
- **Chat** — a persistent, resizable tray providing interactive conversation with the LLM in the project context.

A status bar surfaces running task indicators, providing ambient awareness of system activity without requiring the user to be on the Dispatch view.

### Contextual Help (Tooltips)

Configuration fields that involve syntax rules, non-obvious behavior, or domain-specific concepts provide hover tooltips. The tooltip appears on a help indicator adjacent to the field label — not on the input itself — so it does not interfere with interaction.

Tooltips explain *rules and behavior*, not just labels. They answer: "what do I type here?" and "what will this do?"

Required tooltip surfaces:

- **Subscriptions (glob patterns)** — syntax: `*` matches files in one directory, `**` matches recursively across directories. One pattern per line. Dual purpose: patterns determine which commits trigger the job *and* which files are included as context in the task prompt.
- **Schedule (cron expression)** — five-field format: `minute hour day-of-month month day-of-week`. Ranges (`1-5`), lists (`0,15,30`), steps (`*/10`), and wildcards (`*`). Examples: `*/30 * * * *` (every 30 min), `0 9 * * 1-5` (weekdays at 9am). First evaluation after setting a schedule establishes a baseline — does not fire immediately.
- **Allowed Tools** — select which CLI tools the agent can use. The platform presents the full inventory of available tools; the user selects from this list. When any tools are selected, the agent sees only those tools plus tools from connected MCP servers. When none are selected, the agent gets the full default tool set. Tools not selected are removed from the agent's environment entirely — the agent has no awareness they exist.
- **MCP Servers (per-job)** — select which registered external MCP servers this job's agent can connect to. Only checked servers are available during dispatch. The platform's internal server (git operations, file access) is always connected. Register servers in the MCP Servers view first, then enable them here per-job.
- **Require Approval** — when enabled, automated triggers (commit-watch, schedule, dependency) produce tasks that wait for manual approval before executing. Manual dispatches bypass this gate.
- **Coalesce Dispatches** — when enabled, the job will never have more than one pending task. Any new trigger merges into the existing pending task instead of creating a new queue entry. Useful for jobs that should catch up in one run rather than queuing redundant work.
- **Dependencies** — the job auto-dispatches when *any* selected upstream job completes successfully. Circular chains are allowed — coalescing prevents runaway queuing. Timed-out, failed, or cancelled tasks do not trigger dependents.
- **Timeout** — maximum execution time in seconds. When reached, the platform gracefully terminates the agent, then force-kills if it does not exit. Timed-out tasks do not trigger downstream dependencies. Set to 0 for no limit.
- **Auto-dispatch (Settings)** — when enabled, the background worker automatically pulls and executes pending tasks in order. When disabled (paused), tasks accumulate as pending. The user reorders them via drag-and-drop in the Dispatch view, then toggles auto-processing when ready.
- **Model** — the LLM model for this job. Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency jobs.

### MCP Servers View

MCP server configuration is a first-class surface with its own rail item. It is not buried in Settings. The reason: MCP servers are how users extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view must be simple enough that a user who has never configured an MCP server can succeed on the first attempt.

**Design principles:**

- **Progressive disclosure.** The empty state is not a blank page — it explains what MCP servers are, why you might add one, and how to do it. Configuration complexity is revealed only as the user engages. A user with zero servers sees guidance. A user with three servers sees status and management.
- **Guided registration.** Adding a server requires three pieces of information: a name, a command, and arguments. The form makes this obvious — labeled fields with placeholder examples that show real, working patterns (e.g., `npx @modelcontextprotocol/server-filesystem /path/to/dir`). No unlabeled inputs, no ambiguous fields. The form validates before submission and explains what went wrong in plain language.
- **Immediate feedback.** After adding a server, the platform tests connectivity automatically and shows the result — healthy with a tool count, or an error with a plain-language explanation. The user never wonders "did it work?" The test action is also available on demand for any registered server.
- **Health at a glance.** Each server shows its current state: enabled/disabled, healthy/unreachable, and the tools it provides. The user can scan the list and immediately understand which servers are working.
- **Safe defaults.** New servers are enabled by default — the most common intent when adding a server is to use it. Disabling is a deliberate choice the user makes later if needed.

**What the view shows:**

- The list of registered servers, each displaying: name, command, enabled state, health status, and discovered tools (when healthy).
- A registration form for adding new servers.
- Per-server actions: enable/disable toggle, test connection, remove.
- When a server is unhealthy, the error is shown inline — not hidden behind a click. Error messages should help the user fix the problem: "command not found" means the command isn't installed or isn't on PATH; "connection refused" means the server started but isn't responding correctly.

**What the view does not do:**

- No environment variable editing in the initial version. Keep the configuration surface minimal — name, command, args. Environment variables can be added later if users need them.
- No per-job assignment. That stays on the job configuration surface where it belongs — the MCP Servers view manages the global registry; jobs select from it.

**Relationship to job configuration:** The MCP Servers view is where servers are registered and managed. The job configuration surface (Jobs view) is where servers are assigned to specific jobs via the `mcp_servers` property. The per-job tooltip directs users to the MCP Servers view when they need to register new servers. This separation keeps each surface focused: one place to manage servers, another to assign them.

---

## Constraints

### Operational Integrity

- **Queue-first invariant**: every dispatch passes through the queue as a task before execution. All code paths — manual, watch, schedule, dependency — enqueue first, then execute.
- **Sequential execution**: exactly one task runs at a time. The worker holds a lock during processing.
- **Project isolation**: each project has its own SQLite database. The app-level database holds only the recent-projects list.
- **Git is content source-of-truth**: all project content lives in git. The SQLite database holds only operational state (job configs, task records, chat sessions).
- **Single active project**: the platform operates on one project at a time. The active project is global state that all operations reference.

### Data Integrity

- **Job identity is immutable**: a job's slug ID, once derived from its initial name, stays constant. All references (tasks, properties, dependencies) use the slug. Renaming changes only the display label.
- **Task lifecycle is monotonic**: a task progresses from created → started → completed. Retry creates a new cycle by resetting lifecycle fields on the same record, preserving task identity.
- **Trigger context is immutable at enqueue time**: each trigger entry's context string is built when the trigger fires. This preserves the causal record — the prompt reflects what was true when the trigger occurred.
- **Job deletion cascades**: removing a job removes all associated data (properties, tasks, sessions). This prevents orphaned records.
- **Running state is derived**: whether a task is active is computed from lifecycle timestamps (started_at IS NOT NULL AND completed_at IS NULL), not persisted as a separate status field.
- **Outcome summaries are derived from git**: the summary is computed from commits between `start_commit` and `result_commit`. It reflects what the repository records, not what the agent claims. A task that produces no commits has no summary.
- **Ratings are optional and user-initiated**: a task's rating defaults to null (unrated). The user explicitly sets it. Ratings are never inferred or auto-assigned.
- **Merge preserves trigger history**: merging pending tasks concatenates their trigger arrays. No trigger entry is lost or rewritten. The surviving task's triggers are the union of all source tasks' triggers, ordered by original creation time.
- **Split produces valid tasks**: each task created by split carries exactly one trigger entry from the original. The original task retains its first trigger and identity; new tasks get fresh IDs and are appended to the pending queue.
- **Composition operates on pending tasks only**: merge and split apply exclusively to pending tasks that have not started execution. Active, completed, and pending-approval tasks are ineligible. This preserves lifecycle monotonicity — once a task starts, its trigger set is fixed.
- **Same-job constraint on merge**: only tasks belonging to the same job can be merged. A task's identity is bound to one job; cross-job merging would violate prompt assembly, tool configuration, and commit authorship invariants. The UI enforces this structurally — the merge affordance does not appear when tasks belong to different jobs, so the invalid operation is never offered.

### Accountability

- **Tool mediation is observable**: every tool call that flows through the internal MCP server is logged as a structured event. The platform can reconstruct exactly what an agent did, not just what it produced.
- **Context-aware tool surfaces**: the set of tools available to an agent is determined by the job's configuration, not by the agent's own choices. The platform controls what actions are possible.
- **Tool restrictions are invisible to agents**: an agent only sees tools it is allowed to use. Tools outside the allowed set are removed from the agent's environment — not mentioned, not instructed against, not present. A headless agent has no mechanism to negotiate access; presenting tools it cannot use would only produce failed attempts or prompt-level workarounds. The platform owns the restriction surface; the agent owns only its allowed capabilities.
- **Tool inventory is discoverable**: the platform presents the complete set of available tools — built-in CLI tools, internal MCP tools, and external MCP server tools — so the user configures from known options rather than guessing. Tool configuration surfaces select from what exists; they do not accept arbitrary text that may not correspond to real tools.

### Safety

- **Active task locks project state**: while a task is running, the platform keeps the project directory unchanged. This prevents state corruption from changing the working directory mid-execution.
- **Stale sweep on startup**: any task marked as in-flight when the process starts is marked interrupted. This eliminates zombie tasks.
- **Approval gates apply to all automated triggers**: manual dispatch (explicit human intent) bypasses the approval check; all other trigger types respect `require_approval`.
- **Timeouts are enforced**: every task has a configurable timeout (inherited from its job). The watchdog runs unconditionally. Jobs have bounded execution time.
- **Dependency cycles coalesce gracefully**: circular dependency chains produce redundant triggers that coalescing absorbs, preventing unbounded task growth.
- **Post-commit hook is non-blocking**: the hook runs asynchronously and fails silently. Hook failures never prevent or delay git operations.
