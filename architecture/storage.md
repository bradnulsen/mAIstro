# Storage

Two SQLite databases serve distinct scopes: one per-project for operational state, one per-application for cross-project metadata.

## Project Database

**Location**: `<project>/.maistro/maistro.db` (gitignored)
**Access**: Async via `aiosqlite` — single persistent connection, reused across the process lifetime
**Module**: `backend/database.py`

The project database holds all operational state for a single project: task definitions, dispatch queue records, chat sessions, chat messages, raw CLI events, MCP server configs, and key-value configuration.

### Connection Management

A module-level singleton (`_conn`) is lazily initialized on first access and reused until explicitly closed. `init_db()` closes any prior connection before opening a new one — this is how project switching works without leaking connections.

Pragmas applied on every connection:
- `journal_mode=WAL` — concurrent reads during writes, critical because the worker writes dispatch records while routes read them
- `foreign_keys=ON` — enforces referential integrity (cascading deletes depend on this)

### Schema

Seven tables:

| Table | Purpose |
|-------|---------|
| `tasks` | Task identity (id, name, created_at) |
| `task_property_defs` | EAV registry — defines property keys, default values, and types |
| `task_properties` | EAV overrides — per-task property values |
| `dispatch_queue` | Every dispatch record with full lifecycle columns |
| `chat_sessions` | Session metadata, links dispatches to their output |
| `chat_messages` | Durable chat messages (role + content) |
| `chat_events` | Raw NDJSON audit trail per session |
| `mcp_servers` | External tool server registrations |
| `config` | Key-value configuration store |

Schema is applied via `CREATE TABLE IF NOT EXISTS` on every `init_db()` call — idempotent, no migration framework. The seed SQL populates `task_property_defs` with core property definitions and sets default config values.

### Entity-Attribute-Value Property System

Task properties use EAV rather than columns. `task_property_defs` defines the universe of property keys with a default value and a type (`string`, `json`, `integer`, `boolean`). `task_properties` holds per-task overrides.

On read, `get_task()` loads all defs, applies defaults, then overlays task-specific values. The `_cast_property()` helper coerces stored strings to the declared type — `json.loads` for JSON, `int()` for integers, lowercase string comparison for booleans.

This design means adding a new property requires only a seed SQL insert — no schema migration, no column addition. The tradeoff is no column-level constraints or indexes on property values.

### Key Invariants

- The `dispatch_queue.triggers` column stores a JSON array of trigger entries, parsed on every read via `_parse_dispatch_row()`
- Foreign keys with `ON DELETE CASCADE` handle `task_properties` cleanup; `chat_sessions` and `dispatch_queue` are explicitly deleted in `delete_task()` because they reference `tasks` but need cleanup before the cascade fires
- The `running` status is derived at query time from `dispatch_queue` (started but not completed), never stored as a property

## Application Database

**Location**: `<repo>/.maistro/app.db` (gitignored)
**Access**: Synchronous via `sqlite3` — short-lived connections per operation
**Module**: `backend/appstate.py`

The app database holds exactly one table: `recent_projects` (path, name, opened_at). It tracks which project directories the user has opened and when, enabling the recent-projects list on the landing screen.

Sync access is deliberate — this database is only touched during project open/close operations, never during hot paths like dispatch processing.

## Relationship Between the Two

The app database knows about project paths. The project database knows nothing about the app. They never reference each other's data. If either is deleted, the other continues to function — the app DB just loses its recent list; the project DB just loses operational state while git content remains intact.
