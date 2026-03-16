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

**mAistro** — an intent-to-reality development engine where LLM-powered jobs coordinate through git. Jobs are persistent configuration entities; tasks are atomic units of work dispatched from jobs.

### Stack
- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode) + sse-starlette
- **Frontend**: React 19 + Vite 6 (no TypeScript, no state library)
- **LLM**: Claude CLI invoked as subprocess with `--output-format stream-json` (NDJSON streaming)
- **Realtime**: Server-Sent Events (SSE) for task streaming and chat

### Data Model
- **Jobs** (`jobs` table): persistent configuration entities with EAV properties (`job_property_defs` + `job_properties`). Hold name, instructions, subscriptions, model, depends_on, schedule, etc.
- **Tasks** (`tasks` table): atomic units of work. Each task has exactly one trigger, one context, and belongs to one job via `job_id` FK. Tasks are never mutated after creation.
- **Coalescing**: `coalesced_id` FK on tasks links subordinate tasks to a root task. Coalesce = set FK, decompose = clear FK. No data mutation.

### Data Flow
1. User opens a target project directory via the UI — backend initializes `.maistro/maistro.db` inside it and installs a git post-commit hook
2. Jobs are configured with `description` (short reference), `instructions` (detailed job-specific prompt), subscription glob patterns, `depends_on` (list of upstream job IDs), and a model
3. **Task queue**: all triggers (manual, watch, scheduled, dependency) create an atomic `tasks` record. A background worker pulls from the queue and processes one task at a time
4. The worker builds the system/user prompt, spawns the Claude CLI subprocess, streams NDJSON output, and stores messages durably in a chat session linked to the task
5. The CLI agent commits its own changes via its tools — no auto-commit from the platform
6. Post-commit hook notifies backend — jobs with subscriptions matching changed files get auto-enqueued (coalescing links new tasks to pending root tasks via FK)

### Key Design Decisions
- **Git as source of truth**: all project content lives in git. The SQLite DB holds only operational state — job configs, task queue, chat sessions.
- **Two SQLite databases**: project DB at `<project>/.maistro/maistro.db`; app DB at `<repo>/.maistro/app.db` holds recent-projects list.
- **Atomic tasks**: each task row has exactly one trigger and one context — never mutated after creation. Coalescing is a lightweight FK operation.
- **Internal MCP server**: the platform hosts a context-aware MCP server mediating agent operations. Tool calls are observable and policy-governed. Agents also retain access to native CLI tools.
- **Job properties as key-value overrides**: properties can be `string`, `json`, `integer`, or `boolean` with defaults.
- **Task creates chat sessions**: each task links to a chat session for durable output storage and audit trail.
- **Vite proxies `/api` to backend**: frontend makes API calls to same origin; Vite dev server proxies to port 8420.

## Key Files

- `run.py` — Uvicorn launcher (hot-reload on `backend/`)
- `backend/main.py` — FastAPI app, lifespan, CORS initialization; aggregates routers and defines project/feed/git/hook/MCP/config routes
- `backend/job_routes.py` — Job CRUD, reorder, and subscription routes (APIRouter)
- `backend/queue_routes.py` — Task lifecycle and queue control routes (APIRouter)
- `backend/chat.py` — Chat/conversation routes for executive assistant interface (APIRouter, distinct from task dispatches)
- `backend/database.py` — Project SQLite schema, EAV property system, migrations, all CRUD helpers (async)
- `backend/appstate.py` — App-level SQLite DB for recent-projects list (sync, separate from project DB)
- `backend/state.py` — Shared mutable state (`PROJECT_DIR`) and utilities (`utcnow`, `require_project`) to avoid circular imports
- `backend/git.py` — Git subprocess abstraction (log, diff, commit, hook installer)
- `backend/cli.py` — Claude CLI subprocess invocation: stdin piping, NDJSON parsing, event schema
- `backend/dispatch.py` — Prompt assembly (`build_task_system_prompt`/`build_user_prompt`), task lifecycle, watch trigger matching, job manifest
- `backend/scheduler.py` — Cron-based background scheduler: checks job schedules every 30s, enqueues tasks when due
- `backend/worker.py` — Background task worker: pulls from queue, runs tasks one at a time, manages lifecycle (started_at/completed_at/error), handles cancellation and stale task sweep on startup
- `frontend/src/App.jsx` — Shell with rail navigation, project opener, view router
- `frontend/src/api.js` — API client with `fetchJSON` and `fetchSSE` helpers
- `frontend/src/components/` — `Queue.jsx` (task queue management + output viewer), `Feed.jsx` (git activity), `Tasks.jsx` (job config + dispatch), `Settings.jsx` (config, queue, model, MCP servers), `Chat.jsx` (chat interface)

## Conventions

- Job IDs are slugified from names (see `database.slugify`)
- Job commit authorship: `<JobName> <<job-id>@maistro.local>`
- SSE event types: `text`, `result`, `error`, `session_id`, `task`, `tool_use`
- Task triggers: `manual`, `commit` (watch), `dependency` (upstream job completed), `schedule`, `resume`, `retry`
- Watch behavior: jobs with non-empty subscriptions auto-trigger on matching commits (no separate toggle — subscriptions presence = watch active)
- Task coalescing: each task is atomic (one trigger, one context). `coalesced_id` FK links subordinate tasks to a root. `coalesce_tasks=true` on a job auto-coalesces new tasks at enqueue time. Manual coalesce/decompose via drag-drop in the UI is the same FK operation.
- Running state is derived from `tasks` (started_at IS NOT NULL AND completed_at IS NULL), not stored as a job property
- Subscriptions serve dual purpose: trigger matching (watch) and context injection (all tasks)
- Task columns: `trigger` (type), `trigger_detail` (specifics), `context` (pre-formatted text) — real columns, not JSON
- Queue can be auto-processing or paused — controlled via `/api/queue/settings` (auto_dispatch toggle)
- Backend port: 8420, Frontend port: 5173
