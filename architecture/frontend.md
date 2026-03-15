# Frontend

The frontend is a single-page React application that provides the operator interface for project management, task configuration, dispatch monitoring, and interactive chat.

**Stack**: React 19, Vite 6, no TypeScript, no state management library.

## Application Shell

`App.jsx` provides the outer layout:

- **Rail navigation**: vertical icon bar on the left — Queue, Activity (Feed), Tasks, Files, Settings
- **Status bar**: horizontal strip showing all tasks with running indicators (pulsing dot for active dispatches)
- **Main area**: renders the active view
- **Chat tray**: a resizable side panel (drag-to-resize, click-to-toggle) housing the interactive chat

The app polls task status every 5 seconds to keep the status bar current.

## Views

### Queue (`Queue.jsx`)
The primary operational view. Shows the dispatch queue with status indicators (pending, running, completed, error). Provides controls for processing, cancelling, approving/rejecting, resuming, and retrying dispatches. Displays dispatch output with streaming text, tool use events, and diff views.

### Activity Feed (`Feed.jsx`)
Git-centric view showing commit history enriched with dispatch metadata. Each commit shows author, message, file stats, and — if the commit came from a dispatch — the linked task and trigger type.

### Tasks (`Tasks.jsx`)
Configuration view for task CRUD. Each task expands to show all configurable properties: instructions, model, subscriptions, schedule, dependencies, timeout, approval, tools, and MCP servers. Supports drag-to-reorder and inline dispatch.

### Files
A project file browser providing read-only access to project content. The user searches for files by glob pattern and views their contents inline. Markdown files render as formatted documents; code files render with syntax highlighting. This view enables direct inspection of project files without leaving the application or switching to an external editor.

### Settings (`Settings.jsx`)
Queue settings (auto-dispatch toggle), MCP server management, and global configuration.

## Contextual Help

Configuration fields that involve syntax rules, non-obvious behavior, or domain concepts surface hover tooltips. The tooltip attaches to a help indicator adjacent to the field label — not on the input itself — preserving normal interaction.

Tooltips explain rules and behavior, not just labels. They answer "what do I type here?" and "what will this do?" Required surfaces include: subscription glob syntax, cron expression format, allowed tools, approval gates, coalescing, dependencies, timeout, auto-dispatch, MCP servers, and model selection.

This is a frontend-only concern — tooltip content is static, derived from the domain rules documented in DESIGN.md. No backend involvement.

### Chat (`Chat.jsx`)
Interactive conversation interface in the side tray. Manages chat sessions, displays message history, and streams responses via SSE.

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

No state library. React `useState` and `useEffect` throughout. The app component holds project state and task list; views manage their own local state. Task list refresh is centralized in the app and passed down.

## Relationship to Other Systems

- Communicates exclusively through the HTTP API and SSE streams defined by the backend routers
- [Streaming and Sessions](streaming-and-sessions.md) defines the SSE protocol the frontend consumes
- [Dispatch Engine](dispatch-engine.md) exposes the queue and control endpoints
- [Task Configuration](task-configuration.md) exposes the CRUD endpoints
