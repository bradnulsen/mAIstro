# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start

```bash
# Backend (from repo root)
pip install -r requirements.txt
python run.py                 # uvicorn with hot-reload on backend/

# Frontend (separate terminal)
cd frontend
npm install
npm run dev                   # dev server on :5173, proxies /api and /health to :8420
npm run build                 # production bundle to frontend/dist/
```

Backend on http://localhost:8420, frontend on http://localhost:5173. No test suite, linter, or formatter is configured. Hot-reload covers `backend/`; everything else needs a manual restart. Set `MAISTRO_DEBUG=1` for verbose backend logging.

## Documentation Layout

The repo's design documentation is layered. When you need depth, read these in order:

- **`DESIGN.md`** — product requirements, the "why."
- **`STRATEGY.md`** — current priorities and what's in flight.
- **`REMEDIATION.md`** — known structural debt and remediation plans.
- **`architecture/`** — authoritative deep-dives by subsystem (storage, task-lifecycle, streaming-and-sessions, dispatch-engine, prompt-assembly, tool-mediation, trigger-system, governor, project-lifecycle, frontend, git-integration, cli-bridge, job-configuration). When a subsystem is non-trivial, the architecture doc is the source of truth and is more current than this file.

## Architecture

**mAistro** is an intent-to-reality development engine where LLM-powered jobs coordinate through git. Jobs are persistent configuration entities; tasks are atomic units of work dispatched from jobs.

**Naming.** Always use "Job." Earlier docs used "Goal" as a synonym — that's been unified across documents, code, DB tables, API routes, and variables.

### Stack
- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode) + sse-starlette.
- **Frontend**: React 19 + Vite 6, no TypeScript, no state library — pure `useState`/`useEffect` with polling and SSE subscriptions.
- **LLM**: Claude CLI invoked as subprocess with `--output-format stream-json` (NDJSON streaming). On Windows, `cli.py` bypasses npm's `.CMD` wrappers by resolving the underlying Node.js script directly — required for correct process lifecycle (terminate/kill). Async subprocess (`asyncio.create_subprocess_exec`) is unavailable on Windows ProactorEventLoop, so `Popen` + thread readers are used instead.
- **Realtime**: SSE for task streaming and queue updates.

### Data Model

- **Jobs** (`jobs` table): `INTEGER PRIMARY KEY AUTOINCREMENT` ID + `slug` (derived via `slugify()`). Configuration via EAV (`job_property_defs` + `job_properties`) — properties are `string`, `json`, `integer`, or `boolean` with defaults. Notable properties: `max_turns` (default 100), `timeout`, `coalesce_tasks`, `require_approval`, `schedule` (cron), `allowed_tools`, `allowed_internal_tools`, `mcp_servers`, `subscriptions`, `allowed_dispatch_targets`. Description is the north star — declarative, first-principle definition of good output. Slug drives git authorship (`<slug>@maistro.local`) and branch naming.
- **Tasks** (`tasks` table): atomic. Each row has exactly one trigger, one context, one `job_id` FK. Tasks are never mutated after creation — retry, reply, and resume create new tasks and coalesce the original under the new one.
- **Coalescing**: `coalesced_id` FK links subordinates to a root. Depth-1 is enforced by SQLite triggers (`tasks_depth1_insert`, `tasks_depth1_update_target`, `tasks_depth1_update_self`). `coalesce_under()` and `merge_tasks()` flatten before re-parenting so depth-1 holds at every intermediate state. `cascade_completion()` is the single helper used by every worker completion path to terminal-cascade subordinates of a root.
- **Coalescing lock**: every coalesce-mutating op (`split_task`, `uncoalesce_task`, `merge_tasks`) requires all participants to be `pending`; `transfer_task` requires `pending`/`queued` and roots only. Therefore a terminal coalesced subtree is structurally immutable — outcome data on the root is permanent. Reply/resume "unlock" a terminal subtree by introducing a new non-terminal root above it.
- **`tasks` vs `task_executions`**: `tasks` holds intrinsic identity + queue placement + materialized status. Per-execution outcome data (`session_id`, `start_commit`, `result_commit`, `stop_reason`, `num_turns`, `cost_usd`, `started_at`, `completed_at`, `error`, `worktree_path`, `task_branch`) lives in a sibling `task_executions` table keyed by `task_id` — one row per task that actually ran. Coalesced subordinates have no `task_executions` row; their effective outcome is the root's. Write helpers in `db_tasks` (`transition_task`, `update_task`, etc.) route outcome fields to `task_executions` via `upsert_execution` automatically — callers see the same kwargs interface.
- **Reading task data — two semantic profiles**:
  - **"Per-task as the agent saw it"** → read from the `tasks_resolved` view or call `get_task_resolved()`. The view is a 4-way join (`tasks` LEFT JOIN `task_executions` self LEFT JOIN `tasks` root LEFT JOIN `task_executions` root); outcome columns are `COALESCE(self_exec.col, root_exec.col)`, intrinsic columns pass through. Adds derived `is_subordinate` and `effective_root_id`. Use this for output/diff/outcome endpoints, the Governor's recent-tasks view, and anywhere a user might click on a subordinate.
  - **"Per-execution / per-cost"** → `tasks t JOIN task_executions te ON te.task_id = t.id WHERE t.coalesced_id IS NULL`. One row per actual run, no double-counting. Use for aggregates (`dashboard_health`, cost totals, success-rate metrics).

### Task Lifecycle (Event-Sourced)

Task lifecycle is **event-sourced** in `task_events`. Each row is an immutable fact: "task X entered state Y at time Z with optional detail." The `status` column on `tasks` is a *materialized cache* of the latest lifecycle event for query performance — if it ever disagrees with the event log, the event log wins. See [architecture/task-lifecycle.md](architecture/task-lifecycle.md) for the full rationale and event schema.

Legal transitions:

```
pending  → queued / cancelled / rejected
queued   → pending / active / cancelled       (queued→pending is the only backward transition; emits 'restored')
active   → completed / exhausted / failed / cancelled / timed_out / interrupted
```

Terminal: `completed`, `exhausted`, `failed`, `cancelled`, `timed_out`, `interrupted`, `rejected`. `exhausted` (turn-limit) is distinct from `completed` (natural end), `failed` (error), and `timed_out` (wall-clock).

All status changes go through `transition_task()` / `transition_tasks_batch()` (in `backend/db_tasks.py`, re-exported via `backend/database.py`). They validate the transition, write the event row, and update the materialized status column in the same transaction. When a root transitions terminal, subordinates cascade in batch via `cascade_completion()`.

Duration is computed from event pairs (`activated` → terminal), not from column arithmetic — this preserves correctness across retry cycles. `compute_durations_from_events` is the helper.

### Two-Stage Queue
**pending** (staging area, user curates) → **queued** (execution runway, worker pulls) → **active** → **completed** (or other terminal). Auto-queueing setting controls whether new tasks land in pending or skip directly to queued.

### Data Flow
1. User opens a target project directory via the UI — backend initializes `<project>/.maistro/maistro.db` and installs a git post-commit hook.
2. Jobs are configured with summary, description, subscriptions, model, and properties.
3. Triggers (manual, watch, scheduled, dependency, agent, resume, reply) create atomic `tasks` rows. The background worker pulls one task at a time.
4. On activation, the worker creates a per-task git worktree at `.maistro/worktrees/task-<id>/` on branch `<job-slug>/task-<id>` from the current main HEAD. The CLI subprocess runs with `cwd=<worktree>`; the MCP server resolves writes against `MAISTRO_WORKSPACE_DIR` which points at the same path.
5. Worker assembles the system/user prompt, spawns the Claude CLI subprocess, streams NDJSON output, and persists messages and raw events incrementally.
6. The CLI agent commits its own changes via its tools to the task branch — no auto-commit from the platform.
7. On `completed`, the worker integrates the task branch by merging into main (`--ff-only`, falling back to `--no-ff` if main moved). Operator-dirty working tree is transparently stashed and re-applied; conflict policy is **operator wins** (any merge or stash-replay conflict transitions the task to `failed` and preserves the worktree). Successful integration removes the worktree and branch. Non-success terminals preserve the worktree+branch for operator inspection / discard.
8. Post-commit hook notifies backend — `is_ancestor(commit, HEAD)` filters out task-branch commits so watch fires only on integrations into main. Subscribed jobs auto-enqueue; coalescing links new tasks to a pending root via `coalesced_id`.

### Task Workspace Isolation

Each task executes in its own git worktree, so the operator's main checkout is structurally untouchable by agents. See [architecture/git-integration.md](architecture/git-integration.md) for full lifecycle, helpers (`worktree_add`, `worktree_remove`, `merge_branch`, `merge_abort`, `is_ancestor`, etc.), and recovery (`_sweep_stale` + `_sweep_orphan_worktrees` reconcile crashes on startup). Key invariants:

- **Integration is the gate to `completed`.** A task is only `completed` after its branch successfully merges into main. Merge failure → `failed` with the conflict captured in `error`; worktree+branch preserved.
- **No commits → `failed`.** If `worktree_head == start_commit`, the worker fails the task ("agent finished without committing").
- **Non-success terminals preserve the workspace.** `exhausted`, `failed`, `timed_out`, `cancelled`, `interrupted` all leave the worktree and branch in place. The detail-drawer `WorkspaceBanner` exposes path, branch, a manual-merge command, and a confirm-gated `POST /api/tasks/{id}/workspace/discard`.
- **Disk pressure is real.** Worktrees from non-success terminals accumulate until the operator discards them. There's no auto-prune yet (see [git-integration.md — Disk Pressure](architecture/git-integration.md)).

### MCP Server & Tool Governance

- `backend/mcp_server.py` — internal stdio MCP server: git operations (status, log, diff, commit, branch ops), project context (list_files, read_file, list_jobs, get_queue_status), inter-agent dispatch (`dispatch_task`).
- `backend/governor_mcp.py` — Governor's separate stdio MCP server (read tools always; write tools only in execution mode).
- `backend/mcp_config.py` — assembles `--mcp-config` JSON per dispatch, combining internal + external MCP servers.
- **Three-dimensional tool control**: `allowed_tools` (CLI native) × `allowed_internal_tools` (internal MCP, passed via `MAISTRO_ALLOWED_INTERNAL_TOOLS` env var) × `mcp_servers` (external MCP). All compose independently per job. All tool calls are audit-logged.

### Governor

Autonomous meta-analysis agent. Triggered every 10 successful task completions (or manually via `POST /api/governor/trigger`). Reads jobs, recent tasks (through `tasks_resolved`), health metrics, and emits structured findings (suggestions / observations) for the operator to approve, decline, or execute. Approved suggestions can be applied via a write-enabled `execution` run. Replaced the standalone chat surface that earlier versions exposed. See [architecture/governor.md](architecture/governor.md).

### Key Design Decisions

- **Git is the source of truth for project content.** SQLite holds only operational state — job configs, task records, event log, chat sessions/messages, raw event audit, MCP servers, KV config, Governor data.
- **Two SQLite databases.** Project DB at `<project>/.maistro/maistro.db`; app DB at `<repo>/.maistro/app.db` for recent-projects list and cross-project job templates.
- **Atomic tasks.** Never mutated; retry/reply/resume create new tasks and coalesce the prior one.
- **Vite proxies `/api` and `/health` to backend.** Frontend always calls same-origin URLs.

## Backend Module Layout

The backend is organized so each module owns one concern. The two big past splits — database (R2) and routes (R9) — are done; `backend/database.py` and `backend/main.py` are now thin shims.

### `backend/database.py` is a re-export shim

Real implementation lives in:

| Module | Owns |
|---|---|
| `db_core` | Connection lifecycle, `SCHEMA_SQL` / `SEED_SQL`, `init_db`, project-switch readers-draining (`db_read_guard`, `close_db`) |
| `db_migrations` | Isolated legacy-DB migration runner (run on startup; **do not add new entries** — edit `SCHEMA_SQL` and recreate the dev DB) |
| `db_jobs` | Job CRUD, EAV property registry, `slugify`, cascade lookup |
| `db_tasks` | Task CRUD, state machine, event log, coalescing (`transition_task`, `coalesce_under`, `cascade_completion`, `merge_tasks`, `split_task`, `transfer_task`). Routes outcome fields to `task_executions` via `upsert_execution` so callers don't have to know about the split. |
| `db_chat` | Chat sessions, messages, raw event audit log |
| `db_config` | Key-value config + external MCP server registration and cascade delete |
| `db_dashboard` | Read-only operational analytics queries |
| `db_governor` | Governor counters, runs, findings |

New code should import the domain module directly. The re-export shim exists so existing `from backend import database as db; db.foo()` call sites keep working without churn.

### `backend/main.py` is a thin shell

Just logging setup, lifespan, CORS, `/health`, and `include_router` calls. All routes live in `*_routes.py` modules: `job_routes`, `queue_routes`, `governor_routes`, `project_routes`, `feed_routes`, `git_routes`, `mcp_routes`, `dashboard_routes`, `config_routes`.

### Other backend modules

- `state.py` — shared mutable state (`PROJECT_DIR`, `_switching` flag) and `require_project()` to break circular imports.
- `appstate.py` — app-level DB at `<repo>/.maistro/app.db`. Owns the recent-projects list and cross-project job templates (sync sqlite3, not aiosqlite — these are short, infrequent reads outside any project context).
- `worker.py` — background worker: pulls from queue, runs one task at a time, manages lifecycle via `transition_task()`, handles cancellation/timeout watchdog and stale-task sweep on startup.
- `scheduler.py` — cron-based scheduler: checks job schedules every 30s, enqueues when due.
- `dispatch.py` — prompt assembly (`build_dispatch_system_prompt`, `build_user_prompt`), watch trigger matching (`_any_file_matches`, `_glob_to_regex`), job manifest.
- `cli.py` — Claude CLI subprocess invocation, NDJSON parsing. On `is_error` results, emits `result_meta` alongside the error event with `stop_reason` derived from the CLI's `subtype` (e.g. `error_max_turns` → `max_turns`), so the worker correctly classifies turn-limit failures as `exhausted` rather than `completed`.
- `git.py` — git subprocess abstraction (still sync `subprocess.run`; see Known Debt).
- `events.py` — single source of truth for SSE event types and wire serialization (`to_sse()`).
- `pubsub.py` — task-level + global queue-level subscriber registries for SSE.
- `governor.py` — orchestration (trigger handling, prompt assembly, finding parsing, suggestion execution).
- `mcp_probe.py` — external MCP server tool discovery via stdio handshake.

### Frontend layout

- `App.jsx` — shell with rail navigation, project opener, view router.
- `api.js` — `fetchJSON` (request/response) + `fetchSSE` (streaming) — the only place backend URLs are constructed.
- `App.css` — design token system: all visual constants as CSS custom properties on `:root` (colors, type scale `--text-3xs`–`--text-2xl`, spacing `--space-1`–`--space-9`, radius, z-index, layout dims, 10 job identity colors `--job-color-0`–`--job-color-9`). **Use existing tokens rather than hardcoded values.**
- `util.js` — `getTaskStatus` (status with timestamp fallback), `formatDate`, `formatDuration`, label maps.
- `components/` — Queue (Dispatch kanban), Feed, Tasks (job config), Settings, Governor, Dashboard, Files, McpServers, HelpTip.

## Conventions

- **Job IDs are integers.** Slugs are derived (`slugify`) and stored on the job for git authorship + branch naming. Commit authorship: `<JobName> <<job-id>@maistro.local>`.
- **Triggers**: `manual`, `commit` (watch), `dependency`, `schedule`, `agent`, `resume`, `reply`.
- **Watch behavior**: jobs with non-empty `subscriptions` auto-trigger on matching commits. There's no separate watch toggle — subscription presence is the toggle. Subscriptions are also injected as context on every task.
- **Coalescing**: `coalesce_tasks=true` on a job auto-coalesces new tasks at enqueue time (global, all triggers). `schedule` triggers always coalesce globally regardless of this setting. Manual coalesce/decompose via UI drag-drop is the same FK operation. Reply/resume use *inverted* coalescing: the new task becomes root, the original becomes subordinate.
- **Task columns**: `trigger` (type), `trigger_detail` (specifics), `context` (pre-formatted text) — real columns, not JSON.
- **Queue**: auto-processing or paused via `/api/queue/settings` (`auto_dispatch` toggle).
- **Ports**: backend 8420, frontend 5173.
- **API routes**: all prefixed `/api/`. REST conventions: `GET/POST /api/jobs/`, `PATCH/DELETE /api/jobs/:id`. Tasks at `/api/tasks/` (output via `/api/tasks/{task_id}/output` — there is no `/api/chat/` namespace, though `chat_sessions`/`chat_messages` tables still back task output). Queue settings at `/api/queue/settings`. Governor at `/api/governor/`.
- **SSE events** (defined in `events.py`): `text`, `thinking`, `tool_use`, `assistant_complete`, `result_meta`, `result`, `session_id`, `error`, `done`, `task`, `queue_changed`.

### Database Schema

- **No general-purpose migration system.** `SCHEMA_SQL` uses `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` for idempotent init. `SEED_SQL` uses `INSERT OR IGNORE`. To change schema: edit `SCHEMA_SQL` in `db_core` and recreate the dev DB.
- **`backend/db_migrations.py`** runs on startup but only handles legacy table renames / PK conversions from the goals→jobs and slug→INTEGER transitions. Do not add new entries.
- **`migrate_db.py`** at the repo root is a one-shot script for converting older project DBs offline — not part of the running backend.

### Project Switch Coordination

- `db_core` implements a readers-draining protocol: `db_read_guard()` tracks active readers, `close_db()` sets a `_closing` flag and waits up to 10s for readers to drain before closing.
- `state._switching` flag prevents new `db_read_guard()` entries during a switch.
- Worker and scheduler check `state.PROJECT_DIR` inside `db_read_guard()` and catch `RuntimeError("closing...")` if a switch lands mid-operation.
- HTTP layer rejects project switch with 409 if `worker.get_active_task_id()` is set.

## Known Debt (see `REMEDIATION.md`)

The big structural splits (R2 database, R9 routes, R5 write-side coalescing) are done. Remaining:

- **R7: Async git operations.** `git.py` still uses sync `subprocess.run`, blocking the event loop on every git call from worker, scheduler, dispatch, and feed routes. Wrap in `asyncio.to_thread`, make callers `await`.
- **R8: Move glob matching.** `_any_file_matches` and `_glob_to_regex` are trigger-matching functions misfiled in `dispatch.py`. Move to `git.py` or a dedicated `matching.py`.
- **R5 read-side residual.** Task-card rendering in `Queue.jsx` / `Tasks.jsx` shows null metrics for subordinates because it reads `task.num_turns` etc. directly. Cosmetic, not correctness — switch to `get_task_resolved` or fall back to root values via a frontend helper as those surfaces are touched.
