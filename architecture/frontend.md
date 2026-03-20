# Frontend

The frontend is a single-page React application that provides the operator interface for project management, goal configuration, dispatch monitoring, and interactive chat.

**Stack**: React 19, Vite 6, no TypeScript, no state management library.

## Application Shell

`App.jsx` provides the outer layout:

- **Rail navigation**: vertical icon bar on the left — Dispatch, Activity (Feed), Goals, Files, MCP Servers, Dashboard, Settings
- **Command bar**: persistent operational control surface pinned to the top of every view, containing goal indicators, dispatch popouts, auto-queue toggle, and batch queue actions (see below)
- **Main area**: renders the active view
- **Chat tray**: a resizable side panel (drag-to-resize, click-to-toggle) housing the interactive chat

The app polls goal status every 5 seconds to keep the command bar current.

## Command Bar

The command bar is the primary interaction point for dispatch and queue management — accessible from every view without navigation.

**Goal indicators** — one colored indicator per goal, using the goal's identity color. Each shows operational state: idle, has pending tasks, has queued tasks, or has an active task. Clicking an indicator opens a **dispatch popout** — a lightweight panel anchored to the indicator with a context field and dispatch action for immediate manual dispatch. The popout closes after dispatch or on click-away.

**Auto-queue toggle** — the global setting controlling whether new tasks skip pending and go directly to queued. Elevated from Settings because it directly governs how every trigger routes into the queue.

**Queue all / Shelve all** — batch actions for bulk queue management. Queue all transfers all pending tasks to queued. Shelve all transfers all queued (non-active) tasks back to pending. Each action is only available when applicable tasks exist. Shelve all does not affect the currently running task.

## Views

### Dispatch (`Queue.jsx`)
The primary operational view. A single three-column kanban that makes the entire task lifecycle visible at once:

- **Upcoming** (left column) — the staging area. Newly created tasks land here by default. The user reviews, coalesces (merge and split), and curates tasks before promoting them. Pending tasks are not eligible for execution. Tasks are displayed by creation time. This column answers: "what work is waiting for my attention?"
- **Active** (center column) — the execution pipeline. Contains queued tasks awaiting their turn and the currently running task. The running task (if any) appears at the top of the column, visually distinct from queued tasks below it. The user reorders queued tasks to control execution priority. This column answers: "what is running and what runs next?"
- **Resolved** (right column) — all terminal states. Every task that has finished — completed, failed, timed out, cancelled, interrupted, rejected — lands here. Each card carries a status badge identifying its terminal state. Completed (success) cards display the outcome summary and commit range. Non-success cards surface the error context inline. Ordered by completion time (most recent first). This column answers: "what happened?"

Tasks flow left to right through their lifecycle: Upcoming → Active → Resolved. The user drags tasks between Upcoming and Active to promote (pending → queued) or demote (queued → pending). Provides controls for cancelling active tasks, approving/rejecting tasks awaiting approval, and resuming/retrying resolved tasks.

**Detail drawer** — selecting any task opens a drawer that slides up from the bottom of the view. For active tasks, the drawer shows live streamed output (text, tool use, thinking indicators). For resolved tasks, it shows the stored session output, outcome summary, and diff. For upcoming tasks, it shows trigger context and task metadata. The drawer is resizable — the user controls how much vertical space it occupies. Closing the drawer returns full space to the columns. The drawer keeps the column layout visible above it, preserving spatial context while the user inspects a specific task.

**Drag Interaction Model** — drag operations are specialized by column, reflecting the distinct purpose of each stage:

- **Upcoming column (coalescing)** — drag operations in Upcoming support **merge** (drop onto a same-goal task to coalesce) and **transfer** (drop into the Active column to promote). Reordering within Upcoming is not meaningful — it is a staging area, not a priority queue.
- **Active column (sorting)** — drag operations in Active support **reorder** (drop between tasks to change execution priority) and **transfer** (drop into the Upcoming column to demote). Merge is not available in Active — coalescing decisions are made during staging, not after commitment to run.
- **Transfer** (between Upcoming and Active) — drop into the other column. Moves the task from pending to queued or from queued to pending. The transferred task is appended to the end of the target column. Visual feedback: the target column highlights as a drop zone.

Each column has one primary drag operation plus transfer. Upcoming owns coalescing (merge); Active owns sorting (reorder). Cross-column drag is exclusively a transfer — it changes state without merging or reordering within the target column. The Resolved column does not participate in drag operations.

### Activity Feed (`Feed.jsx`)
Git-centric view showing commit history enriched with task metadata. Each commit shows author, message, file stats, and — if the commit came from a task — the linked goal and trigger type.

### Goals (`Tasks.jsx`)
The goal configuration surface. Goal configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Each goal expands to show all configurable properties: instructions, model, subscriptions, schedule, dependencies, timeout, approval, tools, and MCP servers. Manual dispatch is not on this view — it lives on the command bar, where operational actions belong. The Goals view is purely for defining what goals are, not for triggering them.

### Files
A project file browser providing read-only access to project content. The user searches for files by glob pattern and views their contents inline. Markdown files render as formatted documents; code files render with syntax highlighting. This view enables direct inspection of project files without leaving the application or switching to an external editor.

### MCP Servers
A dedicated surface for managing external tool servers. MCP servers extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view manages the global server registry: registration, health monitoring, enable/disable, and removal. Per-goal server assignment remains on the Goals configuration surface. See [Tool Mediation — External MCP Servers](tool-mediation.md#external-mcp-servers) for lifecycle details.

### Dashboard

Aggregated operational visibility — a read-only surface that answers "how are my agents doing?" without inspecting individual tasks. All data derives from existing tables (`tasks` lifecycle columns, `chat_events` with `mcp_tool_use` event type, `chat_sessions` linking sessions to tasks). No new data collection, no write operations.

**Time window selector** — a global control (today, 7 days, 30 days) that scopes all dashboard sections to the same window. All queries filter on `completed_at` (or `started_at` for the timeline) within the selected range.

**Four sections:**

- **Goal Health Summary** — per-goal task counts by terminal state (completed, failed, timed out, cancelled, interrupted, rejected), success rate (completed / total terminal), and trend indicator (current window vs. previous equivalent window). Goals are ordered by health — low success rates and degrading trends are visually prominent. Terminal state breakdown uses the `status` column directly — each terminal status (`completed`, `failed`, `cancelled`, `timed_out`, `interrupted`, `rejected`) maps to a health category (see [Dispatch Engine — Task Status](dispatch-engine.md#task-status)).

- **Timeline** — horizontal bars per task positioned by `started_at` and sized by duration (`completed_at - started_at`), color-coded by goal. Rendered with positioned HTML/CSS elements — no chart library. Reveals scheduling density, idle gaps, and duration outliers. Long-running tasks (significantly above the goal's median) are visually distinct.

- **Agent Dispatch Chains** — visualizes the `agent` trigger type. For agent-initiated tasks, traces the chain back to the original trigger using `trigger_detail` (which carries the originating task reference). Shows chain depth and goal-to-goal dispatch patterns aggregated over the time window. This makes the coordination topology legible — which goals dispatch which, how deep chains go, where coordination breaks down.

- **Tool Usage Patterns** — per-goal tool frequency and error rates derived from `chat_events` where `event_type = 'mcp_tool_use'`. Joins through `chat_sessions` (session → task → goal) to attribute tool calls to goals. Surfaces persistent tool errors that indicate configuration or instruction problems. Secondary to health and timing — supports investigation after triage.

**Data access pattern**: the dashboard introduces a new query surface over existing tables but requires no schema changes. The key queries are time-windowed aggregations:
- `tasks` grouped by `job_id` (goal) with terminal state classification (from `status` column), filtered by `completed_at` within the time window
- `tasks` with `started_at` and `completed_at` for timeline positioning, filtered by time window
- `tasks` filtered by `trigger = 'agent'` with `trigger_detail` for dispatch chain reconstruction
- `chat_events` joined through `chat_sessions` → `tasks` for per-goal tool attribution, filtered by event timestamp

These queries may benefit from an index on `tasks(completed_at)` for efficient time-window filtering — the current indexes (`idx_tasks_status_worker`, `idx_tasks_job_status`) are optimized for queue operations, not historical aggregation. `idx_tasks_completed_at` covers the dashboard time-window predicate.

### Settings (`Settings.jsx`)
Platform configuration: default model, default timeout. The auto-queueing toggle previously here has been elevated to the command bar for immediate access.

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

No state library. React `useState` and `useEffect` throughout. The app component holds project state and goal list; views manage their own local state. Goal list refresh is centralized in the app and passed down.

## Relationship to Other Systems

- Communicates exclusively through the HTTP API and SSE streams defined by the backend routers
- [Streaming and Sessions](streaming-and-sessions.md) defines the SSE protocol the frontend consumes
- [Dispatch Engine](dispatch-engine.md) exposes the queue and control endpoints
- [Job Configuration](job-configuration.md) exposes the CRUD endpoints
