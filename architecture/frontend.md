# Frontend

The frontend is a single-page React application that provides the operator interface for project management, job configuration, dispatch monitoring, and interactive chat.

**Stack**: React 19, Vite 6, no TypeScript, no state management library.

## Application Shell

`App.jsx` provides the outer layout:

- **Rail navigation**: vertical icon bar on the left — Dispatch, Activity (Feed), Jobs, Files, MCP Servers, Dashboard, Settings
- **Status bar**: horizontal strip showing all jobs with running indicators (pulsing dot for active tasks)
- **Main area**: renders the active view
- **Chat tray**: a resizable side panel (drag-to-resize, click-to-toggle) housing the interactive chat

The app polls job status every 5 seconds to keep the status bar current.

## Views

### Dispatch (`Queue.jsx`)
The primary operational view. Contains two mutually exclusive tabs:

**Queue tab** (default) — two-column kanban layout: **Pending** (left) and **Queued** (right). Pending is the staging area where new tasks land for review and curation — coalescing (merge and split) happens here. Queued is the execution runway — sorting (reorder) happens here, and the worker pulls from here. The currently active task (if any) appears prominently, showing its live streamed output. This tab shows only pre-execution and active tasks — the workspace for what is upcoming and what is running right now. Provides controls for cancelling and approving/rejecting. Selecting a task shows its streamed output.

**History tab** — the record of completed work, presented as a two-column kanban layout that mirrors the Queue tab's structure:

- **Completed** (left column) — tasks that ran to completion without error. These are the expected, successful outcomes. Each card displays the outcome summary and commit range. This column answers: "what got done?"
- **Non-Success** (right column) — tasks that reached a terminal state without succeeding: failed, timed out, cancelled, interrupted, rejected. Each card carries a status badge identifying its specific terminal state and surfaces error context inline — the user sees *why* at a glance, not just *that* it didn't succeed. This column answers: "what needs attention?"

Both columns are ordered by completion time (most recent first). The two-column layout makes the success/non-success distinction spatial and immediate — failures are in their own column, visible at a glance rather than interspersed in a single list. Provides controls for resuming and retrying tasks. Tasks flow from the Queue tab to the History tab when they finish, landing in the appropriate column based on outcome.

The two tabs are parallel views of the same domain — one shows what's happening, the other shows what happened. They share the Dispatch rail item; the user switches between them within the view.

**Drag Interaction Model** (Queue tab) — drag operations are specialized by column, reflecting the distinct purpose of each stage:

- **Pending column (coalescing)** — drag operations in pending support **merge** (drop onto a same-job task to coalesce) and **transfer** (drop into the queued column to promote). Reordering within pending is not meaningful — pending is a staging area, not a priority queue. The order tasks leave pending is determined by when the user transfers them to queued.
- **Queued column (sorting)** — drag operations in queued support **reorder** (drop between tasks to change execution priority) and **transfer** (drop into the pending column to demote). Merge is not available in queued — coalescing decisions are made during staging, not after commitment to run.
- **Transfer** (between columns) — drop into the other column. Moves the task from pending to queued or from queued to pending. The transferred task is appended to the end of the target column. Visual feedback: the target column highlights as a drop zone.

Each column has one primary drag operation plus transfer. Pending owns coalescing (merge); queued owns sorting (reorder). Cross-column drag is exclusively a transfer — it changes state without merging or reordering within the target column.

### Activity Feed (`Feed.jsx`)
Git-centric view showing commit history enriched with task metadata. Each commit shows author, message, file stats, and — if the commit came from a task — the linked job and trigger type.

### Jobs (`Tasks.jsx`)
The primary configuration and dispatch surface. Job configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Each job expands to show all configurable properties: instructions, model, subscriptions, schedule, dependencies, timeout, approval, tools, and MCP servers. Inline dispatch for immediate execution — the most direct way to trigger work.

### Files
A project file browser providing read-only access to project content. The user searches for files by glob pattern and views their contents inline. Markdown files render as formatted documents; code files render with syntax highlighting. This view enables direct inspection of project files without leaving the application or switching to an external editor.

### MCP Servers
A dedicated surface for managing external tool servers. MCP servers extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view manages the global server registry: registration, health monitoring, enable/disable, and removal. Per-job server assignment remains on the Jobs configuration surface. See [Tool Mediation — External MCP Servers](tool-mediation.md#external-mcp-servers) for lifecycle details.

### Dashboard

Aggregated operational visibility — a read-only surface that answers "how are my agents doing?" without inspecting individual tasks. All data derives from existing tables (`tasks` lifecycle columns, `chat_events` with `mcp_tool_use` event type, `chat_sessions` linking sessions to tasks). No new data collection, no write operations.

**Time window selector** — a global control (today, 7 days, 30 days) that scopes all dashboard sections to the same window. All queries filter on `completed_at` (or `started_at` for the timeline) within the selected range.

**Four sections:**

- **Job Health Summary** — per-job task counts by terminal state (completed, failed, timed out, cancelled, interrupted, rejected), success rate (completed / total terminal), and trend indicator (current window vs. previous equivalent window). Jobs are ordered by health — low success rates and degrading trends are visually prominent. Terminal state breakdown uses the existing `error` column convention: NULL = completed, specific strings = distinct failure modes (see [Dispatch Engine — Terminal States](dispatch-engine.md#terminal-states)).

- **Timeline** — horizontal bars per task positioned by `started_at` and sized by duration (`completed_at - started_at`), color-coded by job. Rendered with positioned HTML/CSS elements — no chart library. Reveals scheduling density, idle gaps, and duration outliers. Long-running tasks (significantly above the job's median) are visually distinct.

- **Agent Dispatch Chains** — visualizes the `agent` trigger type. For agent-initiated tasks, traces the chain back to the original trigger using `trigger_detail` (which carries the originating task reference). Shows chain depth and job-to-job dispatch patterns aggregated over the time window. This makes the coordination topology legible — which jobs dispatch which, how deep chains go, where coordination breaks down.

- **Tool Usage Patterns** — per-job tool frequency and error rates derived from `chat_events` where `event_type = 'mcp_tool_use'`. Joins through `chat_sessions` (session → task → job) to attribute tool calls to jobs. Surfaces persistent tool errors that indicate configuration or instruction problems. Secondary to health and timing — supports investigation after triage.

**Data access pattern**: the dashboard introduces a new query surface over existing tables but requires no schema changes. The key queries are time-windowed aggregations:
- `tasks` grouped by `job_id` with terminal state classification (from `error` column), filtered by `completed_at` within the time window
- `tasks` with `started_at` and `completed_at` for timeline positioning, filtered by time window
- `tasks` filtered by `trigger = 'agent'` with `trigger_detail` for dispatch chain reconstruction
- `chat_events` joined through `chat_sessions` → `tasks` for per-job tool attribution, filtered by event timestamp

These queries may benefit from an index on `tasks(completed_at)` for efficient time-window filtering — the current indexes (`idx_tasks_pending`, `idx_tasks_running`) are optimized for queue operations, not historical aggregation. This is an implementation concern for the Engineer.

### Settings (`Settings.jsx`)
Platform configuration: auto-queueing toggle, default model, default timeout. Auto-queueing controls where newly created tasks land — when enabled, tasks skip pending and go directly to queued; when disabled, all new tasks enter pending.

### Chat (`Chat.jsx`)
Interactive conversation interface in the side tray. Manages chat sessions, displays message history, and streams responses via SSE.

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
