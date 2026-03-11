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

Backend runs on http://localhost:8420 (uvicorn with `--reload`), frontend on http://localhost:5173. No test suite or linter is configured.

## Architecture

**mAistro** — an intent-to-reality development engine where LLM-powered tasks coordinate through git. The app itself is the agent; tasks are configurable units of work that get dispatched.

### Stack
- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode) + sse-starlette
- **Frontend**: React 19 + Vite 6 (no TypeScript, no state library)
- **LLM**: Claude CLI invoked as subprocess with `--output-format stream-json` (NDJSON streaming)
- **Realtime**: Server-Sent Events (SSE) for dispatch streaming and chat

### Data Flow
1. User opens a target project directory via the UI — backend initializes `.maistro/maistro.db` inside it and installs a git post-commit hook
2. Tasks are configured with `description` (short reference), `instructions` (detailed task-specific prompt), subscription glob patterns, and a model
3. **Dispatch is queue-first**: all dispatches (manual, watch-triggered, task-queued) create a `dispatch_queue` record. A background worker pulls from the queue and processes one dispatch at a time
4. The worker builds the system/user prompt, spawns the Claude CLI subprocess, streams NDJSON output, and stores messages durably in a chat session linked to the dispatch
5. The CLI agent commits its own changes via its tools — no auto-commit from the platform
6. Post-commit hook notifies backend — watch-enabled tasks whose subscriptions match changed files get auto-enqueued (with cooldown)

### Key Design Decisions
- **Git as source of truth**: all project content lives in git. The SQLite DB (`.maistro/` dir, gitignored) holds only operational state — task configs, dispatch queue, chat sessions
- **Two SQLite databases**: project DB at `<project>/.maistro/maistro.db` (async via aiosqlite); app DB at `<repo>/.maistro/app.db` (sync, `appstate.py`) holds recent-projects list
- **Queue-first dispatch**: dispatches are always enqueued first, then processed asynchronously by the background worker. Frontend polls `/api/dispatch/{id}/output` for stored results. This decouples request handling from long-running CLI invocations
- **Task properties are EAV**: `task_property_defs` table defines keys with defaults and types; `task_properties` stores per-task overrides. Types: `string`, `json`, `integer`, `boolean`
- **Claude CLI via Popen+threads**: `cli.py` uses `subprocess.Popen` with thread readers pushing to `asyncio.Queue` (avoids Windows ProactorEventLoop issues). Pipes prompt and system prompt via stdin to avoid cmd arg quoting issues. Reads NDJSON from both stdout and stderr (CLI writes to stderr on `--resume`)
- **Universal system prompt**: `MAISTRO_SYSTEM_PROMPT` in `dispatch.py` covers execution mode, documentation principles, and git workflow for all tasks. Task-specific `instructions` are layered into the user prompt
- **Dispatch creates chat sessions**: each dispatch gets a linked chat session for durable output storage — serves as a permanent audit trail
- **Vite proxies `/api` to backend**: frontend makes API calls to same origin, Vite dev server proxies to port 8420

## Key Files

- `run.py` — Uvicorn launcher (hot-reload on `backend/`)
- `backend/main.py` — FastAPI app, all API routes, global `PROJECT_DIR` state, CORS, lifespan
- `backend/database.py` — Project SQLite schema, EAV property system, migrations, all CRUD helpers (async)
- `backend/appstate.py` — App-level SQLite DB for recent-projects list (sync, separate from project DB)
- `backend/git.py` — Git subprocess abstraction (log, diff, commit, hook installer)
- `backend/cli.py` — Claude CLI subprocess invocation: stdin piping, NDJSON parsing, event schema
- `backend/dispatch.py` — Prompt assembly (`build_system_prompt`/`build_user_prompt`), dispatch lifecycle, watch trigger matching, task manifest
- `backend/worker.py` — Background dispatch worker: pulls from queue, runs dispatches one at a time, manages lifecycle (started_at/completed_at/error), handles cancellation and stale dispatch sweep on startup
- `frontend/src/App.jsx` — Shell with rail navigation, project opener, view router
- `frontend/src/api.js` — API client with `fetchJSON` and `fetchSSE` helpers
- `frontend/src/components/` — `Queue.jsx` (dispatch queue management + output viewer), `Feed.jsx` (git activity), `Tasks.jsx` (config + dispatch), `Chat.jsx` (chat interface)

## Conventions

- Task IDs are slugified from names (see `database.slugify`)
- Task commit authorship: `<TaskName> <<task-id>@maistro.local>`
- SSE event types: `text`, `result`, `error`, `session_id`, `dispatch`, `tool_use`
- Dispatch triggers: `manual`, `commit` (watch), `task_queue` (queued by another task)
- Watch behavior: controlled by `watch_enabled` boolean property on tasks (replaces old `mode` property)
- Running state is derived from `dispatch_queue` (started_at IS NOT NULL AND completed_at IS NULL), not stored as a task property
- Subscriptions serve dual purpose: trigger matching (watch) and context injection (all dispatches)
- `dispatch_queue.context` column stores trigger-specific data (human instructions, task handoff, commit metadata)
- Queue can be auto-processing or manual — controlled via `/api/queue/settings` (auto_dispatch toggle)
- Backend port: 8420, Frontend port: 5173
- Always commit after making code changes