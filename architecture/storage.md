# Storage

Two SQLite databases serve distinct scopes: one per-project for operational state, one per-application for cross-project metadata.

## Project Database

**Location**: `<project>/.maistro/maistro.db` (gitignored)
**Access**: Async via `aiosqlite` — single persistent connection, reused across the process lifetime
**Module surface**: `backend/database.py` is a thin re-export shim. Implementation lives in seven domain modules.

The project database holds all operational state for a single project: job definitions, task records, the task lifecycle event log, chat sessions, chat messages, raw CLI events, MCP server configs, key-value configuration, and Governor runs and findings.

### Module Decomposition

The implementation is split across domain modules so each file owns one concern. `database.py` re-exports the public surface so legacy `from backend import database as db` call sites keep working without churn — new code imports the domain module directly.

| Module | Owns |
|--------|------|
| `db_core` | Connection lifecycle, `SCHEMA_SQL`/`SEED_SQL`, `init_db`, project-switch readers-draining protocol |
| `db_migrations` | Isolated legacy-DB migrations only (do not add new entries — edit `SCHEMA_SQL` and recreate the dev DB) |
| `db_jobs` | Job CRUD, EAV property registry, slugify, cascade target lookup |
| `db_tasks` | Task CRUD, state machine, event log, coalescing (write-side R5) |
| `db_chat` | Chat sessions, messages, raw event audit log |
| `db_config` | Key-value config + external MCP server registration and cascade delete |
| `db_dashboard` | Read-only operational analytics queries |
| `db_governor` | Governor counters, runs, findings |

The boundary discipline: a module owns the queries that touch its tables and the helpers that compose them. Cross-module reads import explicitly; there are no implicit globals beyond the connection itself.

### Connection Management

A module-level singleton (`_conn`) in `db_core` is lazily initialized on first access and reused until explicitly closed. `init_db()` closes any prior connection before opening a new one — this is how project switching works without leaking connections.

Pragmas applied on every connection:
- `journal_mode=WAL` — concurrent reads during writes, critical because the worker writes task records while routes read them
- `synchronous=NORMAL` — durability/throughput tradeoff appropriate for operational state
- `foreign_keys=ON` — enforces referential integrity (cascading deletes depend on this)
- `temp_store=MEMORY`, `mmap_size=64MB`, `cache_size=8MB` — keep hot working set in memory

### Project-Switch Coordination

The connection singleton is shared by three concurrent consumers: the worker loop, the scheduler loop, and HTTP request handlers (including SSE streams). Closing the connection during a project switch must coordinate with all of them to prevent use-after-close errors.

#### Coordination Model

A reader-counting guard in `db_core` (`db_read_guard()`) wraps any DB operation that must complete atomically against project switch. While any guard is held, `close_db()` blocks. `close_db()` sets a `_closing` flag to prevent new guards, waits up to 10 seconds for in-flight readers to drain, then closes.

- **`db_read_guard()`** — async context manager that increments an active-reader count on entry, decrements on exit. Raises `RuntimeError` if entered after `_closing` is set.
- **`close_db()`** — sets `_closing`, awaits the readers-drained event, closes the connection, and clears caches (job property defs).

The granularity is per-operation, not per-request. A long-running task execution does not hold the guard for its full duration — only individual DB calls within it.

#### Ordering Constraint

Project switch follows this sequence:
1. Set `state._switching` to fail new `require_project()` calls (prevents new work from starting)
2. Wait for active readers to drain via the `db_read_guard()` mechanism
3. Close the old connection
4. Open the new connection and set `PROJECT_DIR`
5. Clear the switching flag

This ensures no consumer sees a partially-switched state where `PROJECT_DIR` points to the new project but the connection still belongs to the old one (or is closed).

The HTTP layer additionally checks `worker.get_active_task_id()` before allowing a project switch — if a task is running, the switch returns 409. This coarse-grained guard is the only thing protecting the working directory itself from changing under an executing CLI subprocess.

### Schema

The schema is applied via `CREATE TABLE IF NOT EXISTS` on every `init_db()` call — idempotent. There is no general-purpose migration system. `SEED_SQL` populates `job_property_defs` with core property definitions and sets default config values. `db_migrations.run_migrations()` exists only for legacy table renames (goals→jobs, slug PK→INTEGER PK) and is not where new schema changes belong.

Fourteen tables plus one view:

| Table | Purpose |
|-------|---------|
| `jobs` | Job identity (`INTEGER PRIMARY KEY AUTOINCREMENT`, `slug` UNIQUE, name, created_at) |
| `job_property_defs` | EAV registry — property keys, defaults, and types |
| `job_properties` | EAV overrides — per-job property values |
| `job_learnings` | Per-job structured guidance — discrete, addressable, individually-toggleable rules (id, body, enabled, position, source, timestamps) |
| `tasks` | Atomic task identity + queue placement + materialized status |
| `task_executions` | Per-execution outcome data (one row per task that ran) — keyed by `task_id` |
| `task_events` | Immutable lifecycle event log — source of truth for when transitions happened |
| `chat_sessions` | Session metadata, links jobs and tasks to their output |
| `chat_messages` | Durable chat messages (role + content) |
| `chat_events` | Raw NDJSON audit trail per session |
| `mcp_servers` | External tool server registrations (name, command, args, env, enabled) |
| `config` | Key-value configuration store |
| `governor_runs` | Each Governor invocation: trigger, task count, findings count, started/completed_at, error |
| `governor_findings` | Suggestions and observations produced by Governor runs (status, body, execution result) |
| `tasks_resolved` (view) | Coalescing-aware projection joining tasks with task_executions — see [Task Record](#task-record) |

### Entity-Attribute-Value Property System

Job properties use EAV rather than columns. `job_property_defs` defines the universe of property keys with a default value and a type (`string`, `json`, `integer`, `boolean`). `job_properties` holds per-job overrides.

On read, `get_job()` loads all defs, applies defaults, then overlays job-specific values. The `_cast_property()` helper coerces stored strings to the declared type — `json.loads` for JSON, `int()` for integers, lowercase string comparison for booleans. Property defs are cached at module level in `db_jobs` and reset by `close_db()` on project switch.

This design means adding a new property requires only a `SEED_SQL` insert — no schema migration, no column addition. The tradeoff is no column-level constraints or indexes on property values.

DESIGN's job-learnings reset adds a sibling structured surface that does *not* fit the EAV pattern — learnings need per-row identity (toggle, reorder, source provenance, agent write-back), which an EAV property cannot express. The `job_learnings` table (id, job_id FK CASCADE, body, enabled, position, source `human|agent`, timestamps) lives alongside the EAV registry. The two surfaces compose: prose lives in the EAV `description`, structured rules live in `job_learnings`. Prompt assembly concatenates enabled rows after the description block (zero rows = byte-identical prompt; jobs that don't use the surface are unaffected).

The closed-loop is wired: operators manage the list via the dedicated Learnings tab in `Tasks.jsx` (`source='human'` on every write); agents read the list via the always-available `list_learnings` MCP tool, and — for jobs with `allow_learning_self_modification=true` — write to it via gated `add_learning` / `update_learning` / `delete_learning` tools that go through `/api/learnings/agent-add`, `/api/learnings/{id}/agent-update`, `/api/learnings/{id}/agent-delete`. The agent-write rule is asymmetric: agents can only edit or delete rows they themselves authored (`source='agent'`); operator-authored rows are immutable from the agent side regardless of the permission. `db_learnings.update_learning` and `delete_learning` enforce this via a `require_source` parameter that the agent-side routes pass; operator routes pass `None`. See [proposals/archive/job-learnings-decomposition.md](proposals/archive/job-learnings-decomposition.md) for the design history.

### Task Record

Task data is split across three tables, each owning one concern:

| Table | Owns | Mutability |
|---|---|---|
| `tasks` | Identity (id, job_id, trigger, trigger_detail, context, resume_session_id), queue placement (status, queued_at, sort_order, coalesced_id, approval), creation timestamp | Append-only after insert; only `status` and queue-placement columns ever change |
| `task_executions` | Per-execution outcome data (session_id, start_commit, result_commit, stop_reason, num_turns, cost_usd, started_at, completed_at, error, worktree_path, task_branch, orphan_stash_ref). Keyed by `task_id` (PK + FK ON DELETE CASCADE). | Insert-on-activation, update-on-terminal. One row per task that actually ran. |
| `task_events` | Immutable lifecycle log. Source of truth for when transitions happened. | Append-only |

Key columns and constraints:

- **`tasks.status`** — materialized from the latest lifecycle event in `task_events`. All hot-path queries (worker, queue rendering, coalescing) filter on this column. Updated atomically with each event insert through `transition_task`, which now wraps the dual-write in `try/except/rollback` so a partial failure can't leave the events log diverged from the column (see [Task Lifecycle — Transition Function](task-lifecycle.md#transition-function)).
- **`tasks.coalesced_id`** — nullable foreign key referencing another `tasks` row. When set, this task is subordinate to the referenced root task. The queue view filters on `coalesced_id IS NULL` to show only standalone and root tasks. Routes acting on a task ID also act on all rows where `coalesced_id` equals that ID. See [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition).
- **`task_executions.task_id`** is both the PK and an FK to `tasks(id) ON DELETE CASCADE`. The 1:0..1 relationship reflects the current model: tasks atomic, never re-executed (reply/resume creates new tasks). If multi-execution is ever needed, this evolves to a separate AUTOINCREMENT PK.

#### Why the Split

Before normalization, `tasks` was 21+ columns mixing three concerns: atomic identity, queue placement, and per-execution outcome data. The `tasks_resolved` view's COALESCE-on-every-outcome-column was a code smell saying "these fields don't belong on tasks; they belong on whatever-actually-ran." The smell got worse with each new outcome column (Phase 2 of workspace isolation added one, Phase 3 added two).

The split makes the design contract literal: tasks are atomic and immutable except for materialized status and queue placement. Per-execution data lives elsewhere. Adding new execution-side columns now requires zero changes to the tasks table — only to `task_executions`.

#### Coalescing-Aware Reads: `tasks_resolved` View

Coalesced subordinates inherit their root's outcome. Subordinates have no row in `task_executions` (they never ran independently), so their effective outcome IS whatever the root's execution row contains.

The `tasks_resolved` view is a four-way join: `tasks` LEFT JOIN `task_executions` (self) LEFT JOIN `tasks` (root) LEFT JOIN `task_executions` (root). Outcome columns are `COALESCE(te.col, re.col)` — prefer self's execution row, fall through to the root's. Intrinsic columns (id, job_id, trigger, context, coalesced_id) pass through from `tasks` unchanged. Two derived columns flag the relationship: `is_subordinate` (1 if `coalesced_id IS NOT NULL`) and `effective_root_id` (`COALESCE(coalesced_id, id)`). The depth-1 invariant on `coalesced_id` (see below) makes the single-level join sufficient — no recursion needed.

Two semantic profiles for queries:

- **"Per-task as the agent saw it"** → read from `tasks_resolved` (or call `get_task_resolved()`). Use this for output/diff/outcome endpoints, the Governor's recent-tasks view, and anywhere a user might click on a subordinate.
- **"Per-execution / per-cost"** → read from `tasks t JOIN task_executions te ON te.task_id = t.id WHERE t.coalesced_id IS NULL`. One row per actual run, no double-counting. Use this for aggregates (`dashboard_health`, cost totals, success-rate metrics).

Helper functions in `db_tasks` route writes between the two tables: outcome fields named in `_OUTCOME_FIELDS` are directed to `task_executions` via `upsert_execution`; non-outcome fields stay on `tasks`. `transition_task`, `transition_tasks_batch`, `update_task`, and `update_tasks_batch` all do this routing internally so callers see the same kwargs interface as before.

#### Depth-1 Invariant (DB-Level)

The coalescing model requires that a coalesce group has depth exactly 1: a subordinate's `coalesced_id` always points at a root, and a root never itself becomes a subordinate without first flattening its children. Three SQLite triggers enforce this at the database level so a future regression in the application code fails loudly instead of silently producing depth-N chains:

| Trigger | Fires on | Aborts when |
|---------|----------|-------------|
| `tasks_depth1_insert` | INSERT | New row's `coalesced_id` points at a row that itself has a non-null `coalesced_id` |
| `tasks_depth1_update_target` | UPDATE OF `coalesced_id` | New `coalesced_id` points at a subordinate |
| `tasks_depth1_update_self` | UPDATE OF `coalesced_id` | The task being re-parented currently has subordinates of its own |

The application layer cooperates: every coalesce-mutating operation (`coalesce_under`, `merge_tasks`, `_flatten_coalesce`) re-points subordinates *before* re-parenting their root, so the invariant holds at every intermediate state. The triggers are a hard backstop, not the primary mechanism.

#### Coalescing Locks (Application-Level)

Every coalesce-mutating operation also requires the participants to be in pre-execution states:

- `split_task`, `uncoalesce_task`, `merge_tasks` — all participants must be `pending`
- `transfer_task` — task must be `pending` or `queued`, roots only

As a result, a terminal coalesced subtree is structurally immutable — the root's outcome columns are permanent. Reply and resume "unlock" a terminal subtree by introducing a *new* non-terminal root above it (inverted coalescing — see [Trigger System — Inverted Coalescing](trigger-system.md#inverted-coalescing-reply-and-resume)).

### Task Events

The `task_events` table is the source of truth for task lifecycle history. Each row records an immutable event: a state transition with a timestamp and optional detail. See [Task Lifecycle](task-lifecycle.md) for the full specification including event types, standard query procedures, and duration computation.

Key properties:
- Append-only — events are never updated (deleted only by CASCADE when the parent task is deleted)
- Indexed on `(task_id, id DESC)` for latest-event queries and `(task_id, event, id DESC)` for latest-event-of-type queries
- The `detail` column carries event-specific context (error messages, commit hashes, session IDs) as free-form text or JSON

### Governor Tables

`governor_runs` records each Governor invocation (trigger source, task count at trigger, started/completed timestamps, findings count, error). `governor_findings` records the structured output (type=suggestion|observation, status, title, body, optional execution_result for executed suggestions, FK to the run that produced it).

The `governor_task_counter` config key drives the every-10-tasks autotrigger. See [Governor](governor.md) for the full specification.

> **Direction**: DESIGN's reset to a thread-based Governor replaces `governor_findings` with two new tables — `governor_threads` (id, title, status open|closed, opener governor|human, unread_for_human, created/last-activity/closed timestamps) and `governor_messages` (id, thread_id FK CASCADE, author governor|human, body, optional `action_payload` JSON for typed structured proposals, optional run_id FK, created_at). `governor_runs` keeps its shape but the `trigger` value set narrows to `auto` (survey) and `reply`. The schema and indexes are spelled out in [proposals/governor-threads.md](proposals/governor-threads.md). Per the no-migration-system policy, the dev DB is recreated when the proposal ships; an optional one-shot conversion goes in `migrate_db.py`, not in `db_migrations.py`.

### Dashboard Aggregation Queries

The Activity dashboard (see [Frontend — Activity](frontend.md#activity-dashboardjsx)) introduces a read-only aggregation workload over existing tables and over `git log --numstat` parsed in-process. Unlike queue operations which filter on lifecycle state (pending, queued, active), dashboard queries filter on terminal events and aggregate across jobs. Key patterns:

- **Health**: `tasks JOIN task_executions WHERE coalesced_id IS NULL` grouped by `job_id`, classified by `status`, filtered by `task_executions.completed_at` within the window — subordinates excluded so a coalesce group counts as one outcome
- **Job Impact**: `git log --numstat` parsed by `git.parse_log_with_files`, attributed by author name (`db_jobs.job_for_commit_author`: exact job name, else slugified name against the job slug; anything else, including deleted jobs, is the Operator pseudo-row). The same query path also rolls up per-job `cost_usd`, `num_turns`, completed/non-success counts from `task_executions` for the execution-summary half of the panel
- **Timeline**: `task_events` pairs of `activated` and terminal events, computing duration from their timestamps
- **Commit History**: the same parsed `git log --numstat` output drives the bottom-half scrollable list and its diff drawer (the diff itself comes from `git show` on demand)

The primary worker index is on `(status, approval, coalesced_id)` — covers the worker's queued-task lookup. `idx_task_executions_completed_at` supports time-windowed dashboard queries (the index moved with the column when outcome data was split out of `tasks`).

The dashboard no longer queries `chat_events` for tool-usage analytics or reconstructs dispatch chains from `trigger_detail`. Both panels were dropped in the reorientation — see [proposals/archive/dashboard-commits-reorientation.md](proposals/archive/dashboard-commits-reorientation.md) for the rationale.

### Key Invariants

- The `tasks.context` column stores pre-formatted context text built at the enqueue site
- All foreign keys referencing `jobs(id)` — on `job_properties`, `tasks`, and `chat_sessions` — use `ON DELETE CASCADE`. Deleting a job is a single `DELETE FROM jobs` statement; the database handles dependent row cleanup automatically
- Deleting an external MCP server cascades to job references: the platform removes the server name from every job's `mcp_servers` property list in the same transaction as the server deletion. This is an application-level cascade (not FK-based) because `mcp_servers` is a JSON property stored in the EAV system, not a relational reference
- Task state is materialized in the `status` column, set exclusively through `transition_task` / `transition_tasks_batch`. The `task_events` table is the source of truth for transition history. If the two ever disagree, the event log wins
- Tasks with non-null `coalesced_id` are invisible in queue listings but included when their root task is dispatched or acted upon
- The depth-1 invariant on `coalesced_id` is enforced by SQLite triggers and re-asserted in code on every re-parenting

## Application Database

**Location**: `<repo>/.maistro/app.db` (gitignored)
**Access**: Synchronous via `sqlite3` — short-lived connections per operation
**Module**: `backend/appstate.py`

The app database holds two tables: `recent_projects` (path, name, opened_at) and `job_templates` (cross-project reusable job definitions). It tracks which project directories the user has opened and when, enabling the recent-projects list on the landing screen, and stores templates a user can apply to new projects.

Sync access is deliberate — this database is only touched during project open/close operations and template management, never during hot paths like task processing.

## Relationship Between the Two

The app database knows about project paths and templates. The project database knows nothing about the app. They never reference each other's data. If either is deleted, the other continues to function — the app DB just loses its recent list and templates; the project DB just loses operational state while git content remains intact.
