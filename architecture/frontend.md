# Frontend

The frontend is a single-page React application that provides the operator interface for project management, job configuration, dispatch monitoring, and interactive chat.

**Stack**: React 19, Vite 6, no TypeScript, no state management library.

## Application Shell

`App.jsx` provides the outer layout:

- **Rail navigation**: vertical icon bar on the left — Dispatch, Activity, Jobs, Files, MCP Servers, Settings, Governor
- **Command bar**: persistent operational control surface pinned to the top of every view, containing job indicators, dispatch popouts, auto-queue toggle, and batch queue actions (see below)
- **Main area**: renders the active view

The app polls job status every 5 seconds to keep the command bar current. There is no global chat tray — the standalone chat surface was removed when the Governor replaced reactive Q&A with proactive analysis. Task-scoped output is exposed only inside the Dispatch view's detail drawer.

## Command Bar

The command bar is the primary interaction point for dispatch and queue management — accessible from every view without navigation.

**Job indicators** — one colored indicator per job, using the job's identity color. Each shows operational state: idle, has pending tasks, has queued tasks, or has an active task. Clicking an indicator opens a **dispatch popout** — a lightweight panel anchored to the indicator with a context field and dispatch action for immediate manual dispatch. The popout closes after dispatch or on click-away.

**Auto-queue toggle** — the global setting controlling whether new tasks skip pending and go directly to queued. Elevated from Settings because it directly governs how every trigger routes into the queue.

**Queue all / Shelve all** — batch actions for bulk queue management. Queue all transfers all pending tasks to queued. Shelve all transfers all queued (non-active) tasks back to pending. Each action is only available when applicable tasks exist. Shelve all does not affect the currently running task.

## Views

### Dispatch (`Queue.jsx`)
The primary operational view. A single three-column kanban that makes the entire task lifecycle visible at once:

- **Upcoming** (left column) — the staging area. Newly created tasks land here by default. The user reviews, coalesces (merge and split), and curates tasks before promoting them. Pending tasks are not eligible for execution. Tasks are displayed by creation time. This column answers: "what work is waiting for my attention?"
- **Active** (center column) — the execution pipeline. Contains queued tasks awaiting their turn and the currently running task. The running task (if any) appears at the top of the column, visually distinct from queued tasks below it. The user reorders queued tasks to control execution priority. This column answers: "what is running and what runs next?"
- **Resolved** (right column) — all terminal states. Every task that has finished — completed, failed, timed out, cancelled, interrupted, rejected — lands here. Each card carries a status badge identifying its terminal state. Completed (success) cards display the outcome summary and commit range. Non-success cards surface the error context inline. Ordered by completion time (most recent first). This column answers: "what happened?"

Tasks flow left to right through their lifecycle: Upcoming → Active → Resolved. The user drags tasks between Upcoming and Active to promote (pending → queued) or demote (queued → pending). Provides controls for cancelling active tasks, approving/rejecting tasks awaiting approval, and resuming/retrying/replying to resolved tasks.

**Reply** — the user can reply to a resolved task with follow-up context or instructions. Reply creates a new task (via the `reply` trigger) and coalesces the original under it via inverted coalescing (see [Trigger System — Inverted Coalescing](trigger-system.md#inverted-coalescing-reply-and-resume)). The action requires a text input — the user provides the follow-up message that becomes the new task's context. This is distinct from resume (which continues the same CLI session without new input) and from session interrogation (which is read-only and does not create a task).

**Detail drawer** — selecting any task opens a drawer that slides up from the bottom of the view. For active tasks, the drawer shows live streamed output (text, tool use, thinking indicators). For resolved tasks, it shows the stored session output, outcome summary, diff, and execution metadata (stop reason, turns consumed relative to the limit, duration, cost). For upcoming tasks, it shows trigger context and task metadata. For any task that has dispatched or will dispatch with external MCP servers, the drawer shows which servers were included in the dispatch configuration — so when a task fails, the operator can immediately see whether the failure correlates with a server issue. The drawer is resizable — the user controls how much vertical space it occupies. Closing the drawer returns full space to the columns. The drawer keeps the column layout visible above it, preserving spatial context while the user inspects a specific task.

**Drag Interaction Model** — drag operations are specialized by column, reflecting the distinct purpose of each stage:

- **Upcoming column (coalescing)** — drag operations in Upcoming support **merge** (drop onto a same-job task to coalesce) and **transfer** (drop into the Active column to promote). Reordering within Upcoming is not meaningful — it is a staging area, not a priority queue.
- **Active column (sorting)** — drag operations in Active support **reorder** (drop between tasks to change execution priority) and **transfer** (drop into the Upcoming column to demote). Merge is not available in Active — coalescing decisions are made during staging, not after commitment to run.
- **Transfer** (between Upcoming and Active) — drop into the other column. Moves the task from pending to queued or from queued to pending. The transferred task is appended to the end of the target column. Visual feedback: the target column highlights as a drop zone.

Each column has one primary drag operation plus transfer. Upcoming owns coalescing (merge); Active owns sorting (reorder). Cross-column drag is exclusively a transfer — it changes state without merging or reordering within the target column. The Resolved column does not participate in drag operations.

### Jobs (`Tasks.jsx`)
The job configuration surface. Job configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Each job expands to show all configurable properties: instructions, model, subscriptions, schedule, dependencies, timeout, approval, tools, and MCP servers. Manual dispatch is not on this view — it lives on the command bar, where operational actions belong. The Jobs view is purely for defining what jobs are, not for triggering them.

A dedicated **Learnings** tab carries the job's discrete rules and notes that compose into the prompt as a sibling section after the description. Each row exposes an enabled toggle, a click-to-edit body, a `human` / `agent` source badge, drag-to-reorder, and delete. Operator writes are committed immediately (independent of the EAV save bar) and always record `source='human'`; agent self-modification flows through MCP tools and is gated by the `allow_learning_self_modification` toggle on the same tab.

### Files
A project file browser providing read-only access to project content. The user searches for files by glob pattern and views their contents inline. Markdown files render as formatted documents; code files render with syntax highlighting. This view enables direct inspection of project files without leaving the application or switching to an external editor.

### MCP Servers
A dedicated surface for managing external tool servers. MCP servers extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view manages the global server registry: registration, validation, health monitoring, enable/disable, editing, and removal. Per-job server assignment remains on the Jobs configuration surface. See [Tool Mediation — External MCP Servers](tool-mediation.md#external-mcp-servers) for lifecycle details.

The registration form collects: server name, command, arguments (structured list — each argument is a discrete entry, not space-separated text), and environment variables (key-value pairs with add/remove controls). The form validates eagerly: the command must be a resolvable executable, args and env vars must parse correctly. Invalid registrations are rejected with specific error messages.

Each registered server displays: name, command, arguments, environment variable count, enabled state, health status, and discovered tools (when healthy). Per-server actions: enable/disable toggle, test connection, edit configuration (command, args, env vars), and remove. Deletion shows a confirmation listing all jobs that reference the server — after deletion, the server is removed from all jobs' `mcp_servers` lists automatically.

Error messages are inline and actionable: "command not found" means the executable is missing or not on PATH; "connection refused" means the server started but isn't responding; "handshake failed" means the process started but didn't complete the MCP protocol exchange.

### Activity (`Dashboard.jsx`)

Aggregated operational visibility — a read-only surface that answers "how are my agents doing?" and "what have they actually done?" without inspecting individual tasks. The view absorbed the standalone Feed surface in the dashboard reorientation: there is no separate `Feed.jsx`, no separate rail entry. The single Activity surface combines per-job aggregates (top half) with a concrete commit history (bottom half).

**Time window selector** — a global control (today, 7 days, 30 days) that scopes all sections to the same window.

**Three top-half panels:**

- **Job Health Summary** — per-job task counts by terminal state (completed, failed, timed out, cancelled, interrupted, rejected), success rate (completed / total terminal), and trend indicator (current window vs. previous equivalent window). Terminal state breakdown uses the `status` column directly. Jobs are ordered by health — low success rates and degrading trends are visually prominent.

- **Job Impact** — per-job commit-derived metrics: commits in window, lines added, lines removed, files touched, plus the per-job execution summary (completed / total runs, non-success count, turns consumed, total cost). Authorship is parsed from git log by author name (`user.name` == job name); commits attributed to non-job authors, including jobs since deleted, fold into an "Operator" pseudo-row. This is the panel that answers "what did this job actually produce?" — task-outcome counts alone don't.

- **Timeline** — horizontal bars per task positioned by `started_at` and sized by duration, color-coded by job. Rendered with positioned HTML/CSS — no chart library. Reveals scheduling density, idle gaps, and duration outliers.

**Bottom half — Commit History:**

A scrollable list of commits in the window, with author/job attribution, message, file count, and ±line counts. Clicking a commit opens a diff drawer (the same one the standalone Feed used to provide). This is the concrete companion to Job Impact's aggregates — the user sees per-job sums up top, then drills into specific commits to inspect what was actually changed.

**What the dashboard no longer does:**

- Earlier iterations had Agent Dispatch Chains and Tool Usage Patterns panels. Both were dropped: dispatch-chain reconstruction relied on fragile `trigger_detail` parsing on a sample, and tool usage joined through `chat_events` in a way that returned zeros after the chat-surface removals. Removing them is structural — there is no plan to reintroduce these analytics in their previous shape; if a future need surfaces, it gets its own proposal.

**Data access pattern**: the dashboard introduces a new query surface over existing tables but requires no schema changes. The key reads are:
- `tasks` joined to `task_executions` (filtered on `coalesced_id IS NULL` so coalesce groups count as one outcome), grouped by `job_id` with terminal state classification, filtered by time range — for Job Health and the execution summary in Job Impact
- `git log --numstat` parsed into `{author, files, insertions, deletions}` over the window — for the commit-derived half of Job Impact and for the Commit History panel
- `task_events` pairs of `activated` and terminal events for timeline positioning, with duration computed from event timestamps

The reorientation lives in `db_dashboard.dashboard_job_impact()` (commit-attribution and execution-cost aggregation) and `dashboard_routes` (response shape `{window_days, health, timeline, job_impact}`). The shipped reorientation is documented in [proposals/archive/dashboard-commits-reorientation.md](proposals/archive/dashboard-commits-reorientation.md).

### Settings (`Settings.jsx`)
Platform configuration: default model, default timeout. The auto-queueing toggle previously here has been elevated to the command bar for immediate access.

### Governor (`Governor.jsx`)

Today: a findings feed surface — pending suggestions and unread observations in a single scrollable list, with per-item approve/decline (suggestions) and read/dismiss (observations) buttons, plus a manual trigger button and a run history sidebar. See [Governor](governor.md) for the as-built specification.

DESIGN.md has reset this surface to a thread-based, two-pane correspondence layout. The full target shape — thread list left, selected thread right, compose box, no Approve/Decline buttons (operator response is prose), proposals as structured cards under Governor messages, closed threads collapsed into a disclosure, and a collapsed-by-default debug drawer for operator-internal state — is specified in [proposals/governor-threads.md](proposals/governor-threads.md). The frontend surface will be rewritten when that proposal is engineered.

## Contextual Help

Configuration fields that involve syntax rules, non-obvious behavior, or domain concepts surface hover tooltips. The tooltip attaches to a help indicator adjacent to the field label — not on the input itself — preserving normal interaction.

Tooltips explain rules and behavior, not just labels. They answer "what do I type here?" and "what will this do?" Required surfaces include: subscription glob syntax, cron expression format, allowed tools, approval gates, coalescing, dependencies, timeout, auto-queueing, MCP servers, and model selection.

This is a frontend-only concern — tooltip content is static, derived from the domain rules documented in DESIGN.md. No backend involvement.

## API Client

`api.js` exports typed functions for every backend endpoint. Two transport patterns:

- **`fetchJSON`**: standard request/response for CRUD operations. Handles error extraction and JSON parsing.
- **`fetchSSE`**: streaming transport for dispatch and chat output. Returns `{ abort, done }` — `abort()` cancels the connection, `done` is a Promise that resolves when the stream ends. Parses SSE event/data pairs and delivers them via callback.

All API calls go to the same origin — Vite's dev server proxies `/api` to the backend on port 8420.

## Project Opener

The landing screen when no project is loaded. Provides:
- Manual path input
- OS-native directory picker (backend spawns PowerShell/osascript/zenity)
- Recent projects list from the app-level database

## State Management

No state library. React `useState` and `useEffect` throughout. The app component holds project state and job list; views manage their own local state. Job list refresh is centralized in the app and passed down.

## Relationship to Other Systems

- Communicates exclusively through the HTTP API and SSE streams defined by the backend routers
- [Streaming and Sessions](streaming-and-sessions.md) defines the SSE protocol the frontend consumes
- [Dispatch Engine](dispatch-engine.md) exposes the queue and control endpoints
- [Job Configuration](job-configuration.md) exposes the CRUD endpoints
