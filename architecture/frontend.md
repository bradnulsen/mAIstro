# Frontend

The frontend is a single-page React application that provides the operator interface for project management, job configuration, dispatch monitoring, and interactive chat.

**Stack**: React 19, Vite 6, no TypeScript, no state management library.

## Application Shell

`App.jsx` provides the outer layout:

- **Rail navigation**: vertical icon bar on the left — Dispatch, Activity (Feed), Jobs, Files, MCP Servers, Settings
- **Status bar**: horizontal strip showing all jobs with running indicators (pulsing dot for active tasks)
- **Main area**: renders the active view
- **Chat tray**: a resizable side panel (drag-to-resize, click-to-toggle) housing the interactive chat

The app polls job status every 5 seconds to keep the status bar current.

## Views

### Dispatch (`Queue.jsx`)
The primary operational view. Two-column kanban layout: **Pending** (left) and **Queued** (right). Pending is the staging area where new tasks land for review and curation. Queued is the execution runway — the worker pulls from here. Active and completed tasks appear below the queued column with outcome summaries for completed work. Provides controls for cancelling, approving/rejecting, resuming, retrying, and rating completed tasks. Split breaks a multi-trigger pre-execution task into individual tasks. Displays task output with streaming text, tool use events, and diff views.

**Drag Interaction Model** — three drag operations share a single drag gesture, disambiguated by drop target:

- **Reorder** (within same column) — drop between tasks in the same column. Visual feedback: an insertion line between tasks. The dragged task moves to that position.
- **Merge** (within same column) — drop onto a task's central zone in the same column. Visual feedback: the target task highlights with a merge indicator. The merge zone activates only when the drop target belongs to the same job as the dragged task and both are in the same state. When these conditions are not met, the central zone falls back to reorder behavior.
- **Transfer** (between columns) — drop into the other column. Moves the task from pending to queued or from queued to pending. The transferred task is appended to the end of the target column. Visual feedback: the target column highlights as a drop zone.

The tolerance split between reorder and merge zones is a UI tuning parameter. The essential contract: the user's spatial intent — "place between" vs. "place onto" vs. "move across" — determines which operation occurs. Ordering and coalescing are same-state operations; cross-column drag is exclusively a transfer.

### Activity Feed (`Feed.jsx`)
Git-centric view showing commit history enriched with task metadata. Each commit shows author, message, file stats, and — if the commit came from a task — the linked job and trigger type.

### Jobs (`Tasks.jsx`)
The primary configuration and dispatch surface. Job configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Each job expands to show all configurable properties: instructions, model, subscriptions, schedule, dependencies, timeout, approval, tools, and MCP servers. Inline dispatch for immediate execution — the most direct way to trigger work.

### Files
A project file browser providing read-only access to project content. The user searches for files by glob pattern and views their contents inline. Markdown files render as formatted documents; code files render with syntax highlighting. This view enables direct inspection of project files without leaving the application or switching to an external editor.

### MCP Servers
A dedicated surface for managing external tool servers. MCP servers extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view manages the global server registry: registration, health monitoring, enable/disable, and removal. Per-job server assignment remains on the Jobs configuration surface. See [Tool Mediation — External MCP Servers](tool-mediation.md#external-mcp-servers) for lifecycle details.

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
