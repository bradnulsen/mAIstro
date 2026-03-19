# Project Lifecycle

The project lifecycle system manages opening, closing, and switching between project directories. It coordinates initialization of the database, git hooks, and background services.

## Opening a Project

`POST /api/project/open` triggers a multi-step initialization:

1. **Path validation**: resolves to absolute path, verifies directory exists
2. **Active task guard**: if switching projects (different path than current), blocks if any task is running — prevents state corruption from changing the working directory mid-execution
3. **Git initialization**: `ensure_repo()` creates a git repository if none exists
4. **Project directory registration**: sets the global `PROJECT_DIR` in `backend/state.py`
5. **Database initialization**: `init_db()` closes any prior connection, opens `<project>/.maistro/maistro.db`, applies schema, seeds defaults
6. **Post-commit hook installation**: writes the watch trigger hook to `.git/hooks/post-commit`
7. **Gitignore management**: ensures `.maistro/` and `.claude/` are gitignored
8. **Recent projects update**: records the path in the app-level database
9. **Worker notification**: wakes the worker to process any pending tasks for the newly-opened project

## Closing a Project

`POST /api/project/close` with the same active-task guard. Clears `PROJECT_DIR` and closes the database connection.

## Project Switch Coordination

Opening a new project (or closing the current one) requires closing the active database connection. Four concurrent consumers share this connection: the worker, the scheduler, HTTP handlers, and the chat CLI runner. Closing the connection without coordinating with in-flight operations causes use-after-close errors.

### Current Guards

The active-task check (step 2 of opening) prevents project switch while the worker is executing a task. This covers the most dangerous case — the worker holds the connection for the full duration of CLI execution.

### Gaps

- **Scheduler**: polls every 30 seconds, making multiple DB calls per tick (list_jobs, config reads, enqueue). A project switch mid-tick closes the connection under it. The scheduler has no idle guard.
- **HTTP handlers**: any request that has passed `require_project()` may be mid-query when a different request triggers project switch. The two requests execute concurrently.
- **Worker loop (non-task)**: the worker's poll loop calls `get_oldest_queued_task()` outside the active-task guard. If a project switch races this call, the worker hits a closed connection.

### Coordination Mechanism

The connection coordination model is specified in [Storage — Project-Switch Coordination](storage.md#project-switch-coordination). In summary: a reader-counting guard in `database.py` lets `close_db()` wait until all in-flight DB operations complete before closing. Project switch sets a "switching" flag first to prevent new operations from starting, drains active readers, then closes and reopens the connection atomically.

## Shared Mutable State

`backend/state.py` holds two shared utilities:

- `PROJECT_DIR`: the currently-open project path, or `None`. Read by every subsystem that needs to know the working directory.
- `require_project()`: guard function that raises HTTP 400 if no project is loaded. Called at the top of every route that needs a project context.
- `utcnow()`: UTC timestamp string formatter used for all database timestamps.

This module exists to break circular imports — worker, scheduler, dispatch, and route modules all need the project directory but can't import each other.

## Project Isolation

Each project has its own SQLite database in its own `.maistro/` directory. Opening a new project closes the old database connection and opens a new one. There is no cross-project state — the only thing that spans projects is the app-level recent-projects list.

The worker sweeps stale tasks once per project open, resetting any that were mid-flight when the process last died.

## Relationship to Other Systems

- [Storage](storage.md) manages the database lifecycle that project open/close drives
- [Git Integration](git-integration.md) provides the initialization operations (repo, hook, gitignore)
- [Dispatch Engine](dispatch-engine.md) enforces the active-task guard and receives the wake notification
