# Project Lifecycle

The project lifecycle system manages opening, closing, and switching between project directories. It coordinates initialization of the database, git hooks, and background services. Project state is install-independent: a project DB written by a dev checkout is readable by the installed build and vice versa.

## Two Databases

mAistro maintains two SQLite databases with different lifecycles:

- **Project DB** at `<project>/.maistro/maistro.db` — jobs, triggers, dispatches, event log, chat sessions, raw event audit, MCP servers, KV config, Governor data. Open/closed by project switch. Async (`aiosqlite`).
- **App DB** at `<app-data>/app.db` — recent-projects list and cross-project job templates. Always open while the process runs; not affected by project switch. Sync (`sqlite3`), because these are short, infrequent reads outside any project context. Owned by `backend/appstate.py`.

The app-data directory is resolved by `appstate._resolve_appdata_dir()` in this order:

1. `$MAISTRO_APPDATA` env var (override; useful for portable installs or smoke tests)
2. `%APPDATA%\mAistro\` on Windows
3. `~/Library/Application Support/mAistro/` on macOS
4. `$XDG_DATA_HOME/mAistro/` (default `~/.local/share/mAistro/`) on Linux

Dev runs (`python run.py` from a checkout) default the app-data directory to a repo-local `.maistro/` — `run.py` sets `MAISTRO_APPDATA` before backend startup if it isn't already set, so an active checkout keeps its own recent-projects list and templates without migrating to the user-app-data location an installed build uses.

## Opening a Project

`POST /api/project/open` triggers a multi-step initialization:

1. **Path validation**: resolves to absolute path, verifies directory exists
2. **Active dispatch guard**: if switching projects (different path than current), blocks with HTTP 409 if any dispatch is running — prevents state corruption from changing the working directory mid-execution. The check is `worker.get_active_task_id()`.
3. **Git initialization**: `ensure_repo()` creates a git repository if none exists
4. **Project directory registration**: sets the global `PROJECT_DIR` in `backend/state.py`
5. **Database initialization**: `init_db()` closes any prior connection, opens `<project>/.maistro/maistro.db`, applies `SCHEMA_SQL` (idempotent `CREATE ... IF NOT EXISTS`), seeds defaults
6. **Worktree sweep**: `_sweep_stale` + `_sweep_orphan_worktrees` reconcile any worktrees / triggers left mid-flight by a prior crash
7. **Post-commit hook installation**: writes the watch trigger hook to `.git/hooks/post-commit`
8. **Gitignore management**: ensures `.maistro/` and `.claude/` are gitignored
9. **Recent projects update**: records the path in the app DB
10. **Worker notification**: wakes the worker to process any pending dispatches for the newly-opened project

## Closing a Project

`POST /api/project/close` with the same active-dispatch guard. Clears `PROJECT_DIR` and closes the database connection. The app DB stays open.

## Project Switch Coordination

Opening a new project (or closing the current one) requires closing the active project DB connection. Three concurrent consumers share it: the worker, the scheduler, and HTTP handlers. Closing the connection without coordinating with in-flight operations causes use-after-close errors.

### Readers-Draining Protocol

`db_core` implements the coordination:

- `db_read_guard()` async context manager — every read site enters/exits this guard, which tracks active readers. It also re-checks `state.PROJECT_DIR` on entry and raises `RuntimeError("closing...")` if a switch is mid-flight, so callers can fall through cleanly.
- `state._switching` flag — set true before close, blocks new guard entries.
- `close_db()` — sets `_closing`, waits up to 10s for active readers to drain, then closes.

Worker and scheduler both run their poll loops inside `db_read_guard()` and catch the closing exception, so a switch that lands mid-tick is non-fatal.

The HTTP layer rejects project switch with HTTP 409 if `worker.get_active_task_id()` is set — the active-dispatch case is too long-running to wait out within the 10s drain window, so it's blocked outright.

See [Storage — Project-Switch Coordination](storage.md#project-switch-coordination) for the storage-level details.

## Shared Mutable State

`backend/state.py` holds shared utilities to break circular imports — worker, scheduler, dispatch, and route modules all need the project directory but can't import each other.

- `PROJECT_DIR`: the currently-open project path, or `None`
- `_switching`: flag set during project switch to block new `db_read_guard()` entries
- `require_project()`: guard function that raises HTTP 400 if no project is loaded; called at the top of every route that needs a project context
- `utcnow()`: UTC timestamp string formatter used for all database timestamps

## Project Isolation

Each project has its own project DB in its own `.maistro/` directory. Opening a new project closes the old DB connection and opens a new one. There is no cross-project state — the only thing that spans projects is the app DB's recent-projects list and (optional) shared job templates.

Per-project state — including worktrees under `.maistro/worktrees/task-<id>/` — survives mAistro uninstall, since `.maistro/` lives inside the project directory and the installer never touches it.

## Relationship to Other Systems

- [Storage](storage.md) manages the database lifecycle that project open/close drives
- [Git Integration](git-integration.md) provides the initialization operations (repo, hook, gitignore) and the worktree sweep helpers
- [Dispatch Engine](dispatch-engine.md) enforces the active-dispatch guard and receives the wake notification
