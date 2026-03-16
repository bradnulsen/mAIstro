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

### Schema

Seven tables:

| Table | Purpose |
|-------|---------|
| `jobs` | Job identity (id, name, created_at) |
| `job_property_defs` | EAV registry — defines property keys, default values, and types |
| `job_properties` | EAV overrides — per-job property values |
| `tasks` | Every task record with full lifecycle columns |
| `chat_sessions` | Session metadata, links jobs and tasks to their output |
| `chat_messages` | Durable chat messages (role + content) |
| `chat_events` | Raw NDJSON audit trail per session |
| `mcp_servers` | External tool server registrations |
| `config` | Key-value configuration store |

Schema is applied via `CREATE TABLE IF NOT EXISTS` on every `init_db()` call — idempotent, no migration framework. The seed SQL populates `job_property_defs` with core property definitions and sets default config values.

### Entity-Attribute-Value Property System

Job properties use EAV rather than columns. `job_property_defs` defines the universe of property keys with a default value and a type (`string`, `json`, `integer`, `boolean`). `job_properties` holds per-job overrides.

On read, `get_job()` loads all defs, applies defaults, then overlays job-specific values. The `_cast_property()` helper coerces stored strings to the declared type — `json.loads` for JSON, `int()` for integers, lowercase string comparison for booleans.

This design means adding a new property requires only a seed SQL insert — no schema migration, no column addition. The tradeoff is no column-level constraints or indexes on property values.

### Task Table Extensions

Two columns extend the `tasks` table beyond the core lifecycle:

- **`rating`** — nullable binary signal (positive/negative) set by the user on completed tasks. Defaults to null (unrated). Stored directly on the task record for efficient query and display.
- **`coalesced_id`** — nullable foreign key referencing another `tasks` row. When set, this task is subordinate to the referenced root task. The queue view filters on `coalesced_id IS NULL` to show only standalone and root tasks. Routes acting on a task ID also act on all rows where `coalesced_id` equals that ID. See [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition).

### Key Invariants

- The `tasks.context` column stores pre-formatted context text built at the enqueue site
- Foreign keys with `ON DELETE CASCADE` handle `job_properties` cleanup; `chat_sessions` and `tasks` are explicitly deleted in `delete_job()` because they reference `jobs` but need cleanup before the cascade fires
- The `running` status is derived at query time from `tasks` (started but not completed), never stored as a property
- Tasks with non-null `coalesced_id` are invisible in queue listings but included when their root task is dispatched or acted upon

## Application Database

**Location**: `<repo>/.maistro/app.db` (gitignored)
**Access**: Synchronous via `sqlite3` — short-lived connections per operation
**Module**: `backend/appstate.py`

The app database holds exactly one table: `recent_projects` (path, name, opened_at). It tracks which project directories the user has opened and when, enabling the recent-projects list on the landing screen.

Sync access is deliberate — this database is only touched during project open/close operations, never during hot paths like task processing.

## Relationship Between the Two

The app database knows about project paths. The project database knows nothing about the app. They never reference each other's data. If either is deleted, the other continues to function — the app DB just loses its recent list; the project DB just loses operational state while git content remains intact.
