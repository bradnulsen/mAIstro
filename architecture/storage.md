# Storage

Two SQLite databases serve distinct scopes: one per-project for operational state, one per-application for cross-project metadata.

## Project Database

**Location**: `<project>/.maistro/maistro.db` (gitignored)
**Access**: Async via `aiosqlite` — single persistent connection, reused across the process lifetime
**Module**: `backend/database.py`

The project database holds all operational state for a single project: job definitions, task records, chat sessions, chat messages, raw CLI events, MCP server configs, and key-value configuration.

### Connection Management

A module-level singleton (`_conn`) is lazily initialized on first access and reused until explicitly closed. `init_db()` closes any prior connection before opening a new one — this is how project switching works without leaking connections.

Pragmas applied on every connection:
- `journal_mode=WAL` — concurrent reads during writes, critical because the worker writes task records while routes read them
- `foreign_keys=ON` — enforces referential integrity (cascading deletes depend on this)

### Project-Switch Coordination

The connection singleton is shared by four concurrent consumers: the worker loop, the scheduler loop, HTTP request handlers, and the chat CLI runner. Closing the connection during a project switch must coordinate with all of them to prevent use-after-close errors.

#### The Problem

`close_db()` nulls the singleton and closes the underlying connection immediately. If any consumer is mid-query — has called `get_db()` and is between `execute()` and `commit()` — the connection becomes invalid under it. The race is possible because:

1. **Worker**: guarded by an active-task check at the HTTP layer, but the check and the close are not atomic. The worker reads `PROJECT_DIR` at the top of its loop, then makes DB calls — if `close_db()` runs between those two points, the worker hits a closed connection.
2. **Scheduler**: has no guard at all. It reads `DB_PATH` and `PROJECT_DIR` at loop top, then iterates all jobs and makes multiple DB calls (list_jobs, get_config, enqueue_task). A project switch mid-iteration closes the connection under it.
3. **HTTP handlers**: any in-flight request that has already passed `require_project()` can be mid-query when another request triggers project switch.
4. **Chat CLI runner**: long-running streaming sessions that make DB calls throughout their lifetime.

#### Coordination Model

An `asyncio.Lock` (`_db_lock`) in `database.py` guards the connection lifecycle. All consumers acquire this lock as a shared reader; `close_db()` acquires it exclusively before closing.

Since Python's `asyncio.Lock` has no reader/writer mode, the implementation uses a counting semaphore pattern:

- **`db_read_guard()`** — async context manager that increments an active-reader count on entry, decrements on exit. While the count is nonzero, `close_db()` blocks.
- **`close_db()`** — sets a "closing" flag that prevents new readers from entering, then waits until the active-reader count reaches zero before closing the connection.

The granularity of protection is per-operation, not per-request. Each `get_db()` call site wraps its query-to-commit span in `db_read_guard()`. This keeps the critical section narrow — a long-running task execution holds the guard only around individual DB operations, not for the entire task lifetime.

#### Consumer-Specific Guards

| Consumer | Current guard | Required guard |
|----------|--------------|----------------|
| Worker | Active-task HTTP check blocks project switch while a task runs | Sufficient — task execution is the long pole; individual DB calls within the loop iteration are fast |
| Scheduler | None | `db_read_guard()` around the entire per-tick job scan, or around each DB call within the tick |
| HTTP handlers | `require_project()` checks project is loaded | `db_read_guard()` around DB-touching operations, or project switch waits for in-flight requests to drain |
| Chat runner | None | `db_read_guard()` around each DB call within the streaming session |

The worker's active-task check is the coarse-grained guard that prevents the most dangerous case (project switch during task execution). The `db_read_guard()` covers the remaining gaps — scheduler ticks and HTTP handler races.

#### Ordering Constraint

Project switch must follow this sequence:
1. Set a "switching" flag that causes new `require_project()` calls to fail (prevents new work from starting)
2. Wait for active readers to drain (the `db_read_guard()` mechanism)
3. Close the old connection
4. Open the new connection and set `PROJECT_DIR`
5. Clear the "switching" flag

This ensures no consumer sees a partially-switched state where `PROJECT_DIR` points to the new project but the connection still belongs to the old one (or is closed).

### Schema

Eight tables:

| Table | Purpose |
|-------|---------|
| `jobs` | Job identity (id, name, created_at) |
| `job_property_defs` | EAV registry — defines property keys, default values, and types |
| `job_properties` | EAV overrides — per-job property values |
| `tasks` | Task identity, execution metadata (stop_reason, num_turns, cost_usd), and materialized status |
| `task_events` | Immutable lifecycle event log — source of truth for when transitions happened |
| `chat_sessions` | Session metadata, links jobs and tasks to their output |
| `chat_messages` | Durable chat messages (role + content) |
| `chat_events` | Raw NDJSON audit trail per session |
| `mcp_servers` | External tool server registrations |
| `config` | Key-value configuration store |

Schema is applied via `CREATE TABLE IF NOT EXISTS` on every `init_db()` call — idempotent. A lightweight migration system (`_migrate`) runs after schema creation, gated on a `schema_version` integer in the `config` table. Each migration checks the current version and advances it atomically. This handles changes that `CREATE TABLE IF NOT EXISTS` cannot express (e.g., adding FK constraints to existing tables via table recreation). The seed SQL populates `job_property_defs` with core property definitions and sets default config values.

### Entity-Attribute-Value Property System

Job properties use EAV rather than columns. `job_property_defs` defines the universe of property keys with a default value and a type (`string`, `json`, `integer`, `boolean`). `job_properties` holds per-job overrides.

On read, `get_job()` loads all defs, applies defaults, then overlays job-specific values. The `_cast_property()` helper coerces stored strings to the declared type — `json.loads` for JSON, `int()` for integers, lowercase string comparison for booleans.

This design means adding a new property requires only a seed SQL insert — no schema migration, no column addition. The tradeoff is no column-level constraints or indexes on property values.

### Task Record

The task record carries identity (immutable after creation), execution metadata (set once during execution), queue management columns, and a materialized `status` for query performance:

- **`status`** — materialized from the latest lifecycle event in `task_events`. All hot-path queries (worker, queue rendering, coalescing) filter on this column. Updated atomically with each event insert. See [Task Lifecycle](task-lifecycle.md).
- **`coalesced_id`** — nullable foreign key referencing another `tasks` row. When set, this task is subordinate to the referenced root task. The queue view filters on `coalesced_id IS NULL` to show only standalone and root tasks. Routes acting on a task ID also act on all rows where `coalesced_id` equals that ID. See [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition).

Lifecycle timestamp columns (`queued_at`, `started_at`, `completed_at`) are transitional — maintained via dual-write during migration but redundant with the event log. See [Task Lifecycle — Migration Path](task-lifecycle.md#migration-path) for the removal plan.

### Task Events

The `task_events` table is the source of truth for task lifecycle history. Each row records an immutable event: a state transition with a timestamp and optional detail. See [Task Lifecycle](task-lifecycle.md) for the full specification including event types, standard query procedures, and duration computation.

Key properties:
- Append-only — events are never updated (deleted only by CASCADE when the parent task is deleted)
- Indexed on `(task_id, id DESC)` for latest-event queries and `(task_id, event, id DESC)` for latest-event-of-type queries
- The `detail` column carries event-specific context (error messages, commit hashes, session IDs) as free-form text or JSON

### Dashboard Aggregation Queries

The Activity Dashboard (see [Frontend — Dashboard](frontend.md#dashboard)) introduces a read-only aggregation workload over existing tables. Unlike queue operations which filter on lifecycle state (pending, queued, active), dashboard queries filter on terminal events and aggregate across jobs. Key patterns:

- **Health**: `tasks` grouped by `job_id`, classified by `status` column value (materialized), filtered by time range
- **Timeline**: `task_events` pairs of `active` and terminal events, computing duration from their timestamps
- **Dispatch chains**: `tasks` filtered on `trigger = 'agent'`, following `trigger_detail` references
- **Tool usage**: `chat_events` (where `event_type = 'mcp_tool_use'`) joined through `chat_sessions.task_id` → `tasks.job_id`

The primary worker index is on `(status, approval, coalesced_id)` — covers the worker's queued-task lookup.

### Key Invariants

- The `tasks.context` column stores pre-formatted context text built at the enqueue site
- All foreign keys referencing `jobs(id)` — on `job_properties`, `tasks`, and `chat_sessions` — use `ON DELETE CASCADE`. Deleting a job is a single `DELETE FROM jobs` statement; the database handles dependent row cleanup automatically
- Task state is materialized in the `status` column, set exclusively through `transition_task`. The `task_events` table is the source of truth for transition history. If the two ever disagree, the event log wins
- Tasks with non-null `coalesced_id` are invisible in queue listings but included when their root task is dispatched or acted upon

## Application Database

**Location**: `<repo>/.maistro/app.db` (gitignored)
**Access**: Synchronous via `sqlite3` — short-lived connections per operation
**Module**: `backend/appstate.py`

The app database holds exactly one table: `recent_projects` (path, name, opened_at). It tracks which project directories the user has opened and when, enabling the recent-projects list on the landing screen.

Sync access is deliberate — this database is only touched during project open/close operations, never during hot paths like task processing.

## Relationship Between the Two

The app database knows about project paths. The project database knows nothing about the app. They never reference each other's data. If either is deleted, the other continues to function — the app DB just loses its recent list; the project DB just loses operational state while git content remains intact.
