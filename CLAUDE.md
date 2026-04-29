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

# Frontend production build
cd frontend
npm run build   # outputs to frontend/dist/
```

Backend runs on http://localhost:8420, frontend on http://localhost:5173. Backend requires manual restart after code changes. No test suite, linter, or formatter is configured.

## Architecture

**mAistro** — an intent-to-reality development engine where LLM-powered jobs coordinate through git. Jobs are persistent configuration entities; tasks are atomic units of work dispatched from jobs. See `DESIGN.md` for full requirements and `STRATEGY.md` for current priorities.

**Naming.** All documents, code, database tables, API routes, and variable names use "Job." `DESIGN.md` previously used "Goal" as a user-facing synonym — this has been unified. Always use "job."

### Stack
- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode) + sse-starlette
- **Frontend**: React 19 + Vite 6 (no TypeScript, no state library)
- **LLM**: Claude CLI invoked as subprocess with `--output-format stream-json` (NDJSON streaming). On Windows, the CLI layer (`cli.py`) bypasses npm's `.CMD` wrappers by resolving the underlying Node.js script directly — this is required for correct process lifecycle (terminate/kill). Async subprocess (`asyncio.create_subprocess_exec`) is unavailable on Windows ProactorEventLoop, so `Popen` + thread readers are used instead.
- **Realtime**: Server-Sent Events (SSE) for task streaming and chat

### Data Model
- **Jobs** (`jobs` table): persistent configuration entities with `INTEGER PRIMARY KEY AUTOINCREMENT` ID and a `slug` (derived from name via `slugify()`). EAV properties in `job_property_defs` + `job_properties`. Hold name, summary, description, subscriptions, model, schedule, etc. Description is the north star — a declarative, first-principle definition of what good output looks like. Slug is used for git authorship (`<slug>@maistro.local`) and branch naming (`<slug>/description`).
- **Tasks** (`tasks` table): atomic units of work. Each task has exactly one trigger, one context, and belongs to one job via `job_id` INTEGER FK. Tasks have an authoritative `status` column with a validated state machine (see below). Tasks are never mutated after creation — reply and resume create new tasks and coalesce the original under the new one.
- **Coalescing**: `coalesced_id` FK on tasks links subordinate tasks to a root task. Coalesce = set FK, decompose = clear FK. Depth-1 invariant enforced by SQLite triggers (`tasks_depth1_insert`, `tasks_depth1_update_target`, `tasks_depth1_update_self`) — a coalesced_id must point at a root, and a task with subordinates cannot itself become a subordinate without flattening first. `coalesce_under()` and `merge_tasks()` handle this by flattening *before* re-parenting so depth-1 holds at every intermediate state. `cascade_completion()` is the single helper used by every worker completion path to terminal-cascade the subordinates of a root.
- **Coalescing lock**: every coalesce-mutating op (`split_task`, `uncoalesce_task`, `merge_tasks`) requires all participants to be `pending`; `transfer_task` requires `pending`/`queued` and roots only. As a result, a terminal coalesced subtree is structurally immutable — outcome data on the root is permanent. Reply/resume "unlock" a terminal subtree by introducing a new non-terminal root above it.
- **Reading task data with coalescing awareness**: outcome columns (`session_id`, `start_commit`, `result_commit`, `stop_reason`, `num_turns`, `cost_usd`, `started_at`, `completed_at`, `error`) are populated only on the task that actually ran. Two semantic profiles for queries:
  - **"Per-task as the agent saw it"** → read from the `tasks_resolved` view or call `db.get_task_resolved()`. Outcome columns COALESCE through to the root for subordinates; intrinsic columns (id, trigger, context) pass through. Use this for output/diff/outcome endpoints, the Governor's recent-tasks view, and anywhere a user might click on a subordinate.
  - **"Per-execution / per-cost"** → read from `tasks WHERE coalesced_id IS NULL`. One row per actual run, no double-counting. Use this for aggregates (`dashboard_health`, cost totals, success-rate metrics).

### Task Status State Machine
Tasks have an authoritative `status` column. Legal transitions:
- `pending` → `queued`, `cancelled`, `rejected`
- `queued` → `pending`, `active`, `cancelled`
- `active` → `completed`, `exhausted`, `failed`, `cancelled`, `timed_out`, `interrupted`
- All of `completed`, `exhausted`, `failed`, `cancelled`, `timed_out`, `interrupted`, `rejected` are terminal

`exhausted` means the agent hit its turn limit (`max_turns` property, default 50) without finishing — distinct from `completed` (natural end), `failed` (error), and `timed_out` (wall-clock timeout). Each task also records `stop_reason`, `num_turns`, and `cost_usd` metadata columns.

Event sourcing: `task_events` table records every lifecycle transition (dispatched, queued, activated, completed, etc.) with timestamps and optional detail. Used for timeline reconstruction and dashboard analytics.

All status changes go through `transition_task()` / `transition_tasks_batch()` in `database.py`, which validate the transition and set timestamps (`queued_at`, `started_at`, `completed_at`) automatically. When a root task transitions, subordinates (via `coalesced_id`) cascade in batch.

### Two-Stage Queue
Tasks progress through: **pending** (staging area, user curates) → **queued** (execution runway, worker pulls) → **active** → **completed/failed**. Auto-queueing setting controls whether new tasks land in pending or skip directly to queued.

### Data Flow
1. User opens a target project directory via the UI — backend initializes `.maistro/maistro.db` inside it and installs a git post-commit hook
2. Jobs are configured with `summary` (one-liner), `description` (full north star text), subscription glob patterns, and a model
3. **Task queue**: all triggers (manual, watch, scheduled, dependency, agent) create an atomic `tasks` record. A background worker pulls from the queue and processes one task at a time
4. The worker builds the system/user prompt, spawns the Claude CLI subprocess, streams NDJSON output, and stores messages durably in a chat session linked to the task
5. The CLI agent commits its own changes via its tools — no auto-commit from the platform
6. Post-commit hook notifies backend — jobs with subscriptions matching changed files get auto-enqueued (coalescing links new tasks to pending root tasks via FK)

### MCP Server & Tool Governance
- `backend/mcp_server.py` — internal stdio MCP server providing agents with git operations (status, log, diff, commit, branch create/switch/merge), project context (list_files, read_file, list_jobs, get_queue_status), and inter-agent dispatch (dispatch_task)
- `backend/mcp_config.py` — generates `--mcp-config` JSON for Claude CLI, combining internal + external MCP servers per job config
- Three-dimensional tool control: `allowed_tools` (CLI native), `allowed_internal_tools` (internal MCP via env var `MAISTRO_ALLOWED_INTERNAL_TOOLS`), `mcp_servers` (external MCP) — compose independently per job
- All tool calls are audit-logged to the backend

### Key Design Decisions
- **Git as source of truth**: all project content lives in git. The SQLite DB holds only operational state — job configs, task queue, chat sessions.
- **Two SQLite databases**: project DB at `<project>/.maistro/maistro.db`; app DB at `<repo>/.maistro/app.db` holds recent-projects list and cross-project job templates.
- **Atomic tasks**: each task row has exactly one trigger and one context — never mutated after creation. Retry and resume create new tasks; the original is coalesced under the new one via FK.
- **Job properties as key-value overrides**: properties can be `string`, `json`, `integer`, or `boolean` with defaults. Notable properties include `max_turns` (integer, default 50), `timeout` (seconds), `coalesce_tasks`, `require_approval`, `schedule` (cron), `allowed_tools`, `allowed_internal_tools`, `mcp_servers`, `subscriptions`, `allowed_dispatch_targets`.
- **Task creates chat sessions**: each task links to a chat session for durable output storage and audit trail.
- **Vite proxies `/api` and `/health` to backend**: frontend makes API calls to same origin; Vite dev server proxies to port 8420.

## Key Files

- `run.py` — Uvicorn launcher (hot-reload on `backend/`)
- `migrate_db.py` — One-shot legacy DB migration script (not part of the running backend)
- `backend/main.py` — FastAPI app, lifespan, CORS initialization; aggregates routers and defines project/feed/git/hook/MCP/config routes
- `backend/job_routes.py` — Job CRUD, reorder, and subscription routes (APIRouter)
- `backend/queue_routes.py` — Task lifecycle and queue control routes (APIRouter)
- `backend/governor.py` — Governor agent: autonomous meta-analysis, CLI invocation, finding parsing, suggestion execution
- `backend/governor_routes.py` — Governor REST routes: findings CRUD, manual trigger, status, run history (APIRouter)
- `backend/governor_mcp.py` — Governor MCP stdio server: read tools (jobs, tasks, git, health, findings) + write tools (job config, queue settings) in execution mode
- `backend/database.py` — Project SQLite schema, EAV property system, all CRUD helpers (async)
- `backend/appstate.py` — App-level SQLite DB for recent-projects list and cross-project job templates (sync, separate from project DB)
- `backend/state.py` — Shared mutable state (`PROJECT_DIR`) and utilities (`utcnow`, `require_project`) to avoid circular imports
- `backend/git.py` — Git subprocess abstraction (log, diff, commit, hook installer)
- `backend/cli.py` — Claude CLI subprocess invocation: stdin piping, NDJSON parsing, event schema. On Windows, bypasses `.CMD` wrappers by extracting the Node.js script path and invoking directly
- `backend/dispatch.py` — Prompt assembly (`build_dispatch_system_prompt`/`build_user_prompt`), watch trigger matching, job manifest
- `backend/scheduler.py` — Cron-based background scheduler: checks job schedules every 30s, enqueues tasks when due
- `backend/worker.py` — Background task worker: pulls from queue, runs tasks one at a time, manages lifecycle via `transition_task()`, handles cancellation/timeout watchdog and stale task sweep on startup
- `backend/events.py` — CLI event schema: single source of truth for event types and SSE wire serialization (`to_sse()`)
- `backend/pubsub.py` — Task-level and queue-level event pub/sub for SSE streaming (per-task subscriber queues + global queue-change notifications)
- `backend/mcp_server.py` — Internal MCP stdio server: git tools, project context, inter-agent dispatch
- `backend/mcp_config.py` — MCP config generator for Claude CLI invocations
- `backend/mcp_probe.py` — External MCP server tool discovery via stdio handshake (used by tool inventory endpoint)
- `frontend/src/App.jsx` — Shell with rail navigation, project opener, view router
- `frontend/src/api.js` — API client with `fetchJSON` and `fetchSSE` helpers
- `frontend/src/App.css` — Design token system (all visual constants as CSS custom properties)
- `frontend/src/util.js` — Shared utilities: `getTaskStatus` (status derivation with fallback), `formatDate`, `formatDuration`, trigger/status label maps
- `frontend/src/components/` — `Queue.jsx` (unified three-column Dispatch: Upcoming/Active/Resolved kanban with bottom detail drawer), `Feed.jsx` (git activity), `Tasks.jsx` (job config + dispatch), `Settings.jsx` (config, queue, model, MCP servers), `Governor.jsx` (Governor findings feed, approve/decline, manual trigger, run history), `Dashboard.jsx`, `Files.jsx` (file browser with syntax highlighting), `McpServers.jsx` (external MCP server management), `HelpTip.jsx` (contextual help tooltips)

## Conventions

- Job IDs are integers (autoincrement). Slugs are derived from names (`database.slugify`) and stored on the job record for git authorship and branch naming
- Job commit authorship: `<GoalName> <<job-id>@maistro.local>`
- SSE event types (defined in `events.py`): `text`, `thinking`, `tool_use`, `assistant_complete`, `result_meta`, `result`, `session_id`, `error`, `done`, `queue_changed`, `task`
- Task triggers: `manual`, `commit` (watch), `dependency` (upstream job completed), `schedule`, `resume`, `reply`
- Watch behavior: jobs with non-empty subscriptions auto-trigger on matching commits (no separate toggle — subscriptions presence = watch active)
- Task coalescing: each task is atomic (one trigger, one context). `coalesced_id` FK links subordinate tasks to a root. `coalesce_tasks=true` on a job auto-coalesces new tasks at enqueue time (global, all triggers). `schedule` triggers always coalesce globally regardless of this setting. Manual coalesce/decompose via drag-drop in the UI is the same FK operation. Reply/resume use inverted coalescing: new task becomes the root, original becomes subordinate.
- Task status is authoritative via the `status` column. The frontend `util.js:getTaskStatus()` uses it with a fallback derivation from timestamps. Running state for jobs is derived from tasks with `status='active'`, not stored as a job property
- Subscriptions serve dual purpose: trigger matching (watch) and context injection (all tasks)
- Task columns: `trigger` (type), `trigger_detail` (specifics), `context` (pre-formatted text) — real columns, not JSON
- Queue can be auto-processing or paused — controlled via `/api/queue/settings` (auto_dispatch toggle)
- Backend port: 8420, Frontend port: 5173
- All API routes are prefixed `/api/`. REST conventions: `GET /api/jobs/`, `POST /api/jobs/`, `PATCH /api/jobs/:id`, `DELETE /api/jobs/:id`. Tasks at `/api/tasks/` (chat output is fetched via `/api/tasks/{task_id}/output` — there is no separate `/api/chat/` namespace, though `chat_sessions`/`chat_messages` tables still back task output). Queue settings at `/api/queue/settings`. Governor at `/api/governor/`.
- Frontend uses no state management library — pure React `useState`/`useEffect` with polling and SSE subscriptions. `api.js` centralizes all backend calls via `fetchJSON` (request/response) and `fetchSSE` (streaming).

### Database Schema
- No migration system — `SCHEMA_SQL` uses `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` for idempotent init
- `SEED_SQL` uses `INSERT OR IGNORE` for property defs and config defaults
- All tables use `job` terminology: `jobs` (INTEGER PK + slug), `job_properties`, `job_property_defs`, `job_id` INTEGER FK columns
- To add schema changes: update `SCHEMA_SQL` directly. Existing DBs will need manual migration or recreation.

### Project Switch Coordination
- `database.py` implements a readers-draining protocol: `db_read_guard()` context manager tracks active readers, `close_db()` sets `_closing` flag and waits for all readers to drain (10s timeout)
- `state._switching` flag prevents new `db_read_guard()` entries during switch
- Worker and scheduler check `state.PROJECT_DIR` inside `db_read_guard()` — `RuntimeError("closing...")` is caught if project switches mid-operation
- HTTP layer checks `worker.get_active_task_id()` before allowing project switch (409 if task active)

### Frontend CSS
- All visual constants live in `App.css` as CSS custom properties on `:root` — colors (`--bg`, `--surface`, `--accent`, `--danger`, etc.), type scale (`--text-3xs` through `--text-2xl`), spacing (`--space-1` through `--space-9`), radius, z-index, layout dimensions
- 10 job identity colors (`--job-color-0` through `--job-color-9`) used across all surfaces
- Use existing tokens rather than hardcoded values when adding or modifying styles

## Implementation Status

**Built and working**: Two-stage queue, all 5 trigger types + resume/reply, coalescing (auto + manual merge/split), approval gates, timeout enforcement, internal MCP server (12 tools), three-dimensional tool control, inter-agent dispatch with depth limiting, activity dashboard (health/timeline/chains/tool usage), outcome summaries, execution pipeline fidelity (thinking blocks streamed/persisted, execution metadata captured), external MCP robustness (registration validation, pre-dispatch health checks, cascade deletion), dispatch diff view, job templates (cross-project reusable job definitions), centralized event schema + pub/sub, Governor (autonomous meta-analysis agent with findings feed, approve/decline suggestions, dedicated MCP tools), all UI views (Dispatch, Feed, Jobs, Files, MCP Servers, Dashboard, Governor, Settings).

**Designed but not yet built** (see `STRATEGY.md` priorities):
- **P1: External MCP Polish** — environment variable UI for MCP server config, server status in dispatch context, runtime MCP error attribution, config file validation before write.
- **P3: Task Session Interrogation** — resume completed task sessions in read-only mode for follow-up questions. Infrastructure exists (CLI `--resume`, `allowed_internal_tools`). Needs: UI surface, read-only tool stripping on dispatch.

**Known technical debt** (see `REMEDIATION.md` for full detail and sequencing):
- `database.py` is a ~2000 line god module; split plan documented as R2
- Coalescing logic spread across 12+ touch points; depth-1 invariant maintained only by code discipline (R5)
- `git.py` uses sync `subprocess.run`, blocks async event loop (R7)
- Route extraction from `main.py` is partial — project, feed, git, dashboard, config routes remain inline (R9)
