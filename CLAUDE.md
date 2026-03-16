# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start

```bash
# Backend (from repo root)
pip install -r requirements.txt
python run.py

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Backend runs on http://localhost:8420 (uvicorn with `--reload`), frontend on http://localhost:5173.

## Architecture

**mAistro** — an intent-to-reality development engine where LLM-powered tasks coordinate through git. The app itself is the agent; tasks are configurable units of work that get dispatched.

### Stack
- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode) + sse-starlette
- **Frontend**: React 19 + Vite 6 (no TypeScript, no state library)
- **LLM**: Claude CLI invoked as subprocess with `--output-format stream-json` (NDJSON streaming)
- **Realtime**: Server-Sent Events (SSE) for dispatch streaming and chat

### Data Flow
1. User opens a target project directory via the UI — backend initializes `.maistro/maistro.db` inside it and installs a git post-commit hook
2. Tasks are configured with `description` (short reference), `instructions` (detailed task-specific prompt), subscription glob patterns, `depends_on` (list of upstream task IDs), and a model
3. **Dispatch is queue-first**: all dispatches (manual, watch-triggered, scheduled, dependency-triggered) create a `dispatch_queue` record. A background worker pulls from the queue and processes one dispatch at a time
4. The worker builds the system/user prompt, spawns the Claude CLI subprocess, streams NDJSON output, and stores messages durably in a chat session linked to the dispatch
5. The CLI agent commits its own changes via its tools — no auto-commit from the platform
6. Post-commit hook notifies backend — tasks with subscriptions matching changed files get auto-enqueued (multiple commits coalesce into one pending dispatch)

### Key Design Decisions
- **Git as source of truth**: all project content lives in git. The SQLite DB holds only operational state — task configs, dispatch queue, chat sessions.
- **Two SQLite databases**: project DB at `<project>/.maistro/maistro.db`; app DB at `<repo>/.maistro/app.db` holds recent-projects list.
- **Internal MCP server**: the platform hosts a context-aware MCP server mediating agent operations. Tool calls are observable and policy-governed. Agents also retain access to native CLI tools.
- **Task properties as key-value overrides**: properties can be `string`, `json`, `integer`, or `boolean` with defaults.
- **Dispatch creates chat sessions**: each dispatch links to a chat session for durable output storage and audit trail.
- **Vite proxies `/api` to backend**: frontend makes API calls to same origin; Vite dev server proxies to port 8420.

## Key Files

- `run.py` — Uvicorn launcher (hot-reload on `backend/`)
- `backend/main.py` — FastAPI app, lifespan, CORS initialization; aggregates routers and defines project/feed/git/hook/MCP/config routes
- `backend/task_routes.py` — Task CRUD, reorder, and subscription routes (APIRouter)
- `backend/queue_routes.py` — Dispatch and queue control routes (APIRouter)
- `backend/chat.py` — Chat/conversation routes for executive assistant interface (APIRouter, distinct from task dispatches)
- `backend/database.py` — Project SQLite schema, EAV property system, migrations, all CRUD helpers (async)
- `backend/appstate.py` — App-level SQLite DB for recent-projects list (sync, separate from project DB)
- `backend/state.py` — Shared mutable state (`PROJECT_DIR`) and utilities (`utcnow`, `require_project`) to avoid circular imports
- `backend/git.py` — Git subprocess abstraction (log, diff, commit, hook installer)
- `backend/cli.py` — Claude CLI subprocess invocation: stdin piping, NDJSON parsing, event schema
- `backend/dispatch.py` — Prompt assembly (`build_dispatch_system_prompt`/`build_user_prompt`), dispatch lifecycle, watch trigger matching, task manifest
- `backend/scheduler.py` — Cron-based background scheduler: checks task schedules every 30s, enqueues dispatches when due
- `backend/worker.py` — Background dispatch worker: pulls from queue, runs dispatches one at a time, manages lifecycle (started_at/completed_at/error), handles cancellation and stale dispatch sweep on startup
- `frontend/src/App.jsx` — Shell with rail navigation, project opener, view router
- `frontend/src/api.js` — API client with `fetchJSON` and `fetchSSE` helpers
- `frontend/src/components/` — `Queue.jsx` (dispatch queue management + output viewer), `Feed.jsx` (git activity), `Tasks.jsx` (config + dispatch), `Settings.jsx` (config, queue, model, MCP servers), `Chat.jsx` (chat interface)

## Conventions

- Task IDs are slugified from names (see `database.slugify`)
- Task commit authorship: `<TaskName> <<task-id>@maistro.local>`
- SSE event types: `text`, `result`, `error`, `session_id`, `dispatch`, `tool_use`
- Dispatch triggers: `manual`, `commit` (watch), `dependency` (upstream task completed), `schedule`, `resume`, `retry`
- Watch behavior: tasks with non-empty subscriptions auto-trigger on matching commits (no separate toggle — subscriptions presence = watch active)
- Dispatch coalescing: `commit` and `dependency` coalesce with other pending dispatches of the same type (multiple commits/dependency resolutions while busy = one catch-up run); `schedule` always coalesces globally; `coalesce_dispatches=true` coalesces globally across all trigger types; `manual`/`resume` never coalesce; `retry` resurrects the original dispatch in-place (appends a retry trigger, resets lifecycle fields)
- Running state is derived from `dispatch_queue` (started_at IS NOT NULL AND completed_at IS NULL), not stored as a task property
- Subscriptions serve dual purpose: trigger matching (watch) and context injection (all dispatches)
- Trigger context is stored in the `dispatch_queue.triggers` JSON array (each entry has `trigger`, `detail`, `context`). Context strings are pre-formatted at the enqueue site
- Queue can be auto-processing or paused — controlled via `/api/queue/settings` (auto_dispatch toggle)
- Backend port: 8420, Frontend port: 5173