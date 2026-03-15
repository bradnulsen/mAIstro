# Project Lifecycle

The project lifecycle system manages opening, closing, and switching between project directories. It coordinates initialization of the database, git hooks, and background services.

## Opening a Project

`POST /api/project/open` triggers a multi-step initialization:

1. **Path validation**: resolves to absolute path, verifies directory exists
2. **Active dispatch guard**: if switching projects (different path than current), blocks if any dispatch is running — prevents state corruption from changing the working directory mid-execution
3. **Git initialization**: `ensure_repo()` creates a git repository if none exists
4. **Project directory registration**: sets the global `PROJECT_DIR` in `backend/state.py`
5. **Database initialization**: `init_db()` closes any prior connection, opens `<project>/.maistro/maistro.db`, applies schema, seeds defaults
6. **Post-commit hook installation**: writes the watch trigger hook to `.git/hooks/post-commit`
7. **Gitignore management**: ensures `.maistro/` and `.claude/` are gitignored
8. **Recent projects update**: records the path in the app-level database
9. **Worker notification**: wakes the worker to process any pending dispatches for the newly-opened project

## Closing a Project

`POST /api/project/close` with the same active-dispatch guard. Clears `PROJECT_DIR` and closes the database connection.

## Shared Mutable State

`backend/state.py` holds two shared utilities:

- `PROJECT_DIR`: the currently-open project path, or `None`. Read by every subsystem that needs to know the working directory.
- `require_project()`: guard function that raises HTTP 400 if no project is loaded. Called at the top of every route that needs a project context.
- `utcnow()`: UTC timestamp string formatter used for all database timestamps.

This module exists to break circular imports — worker, scheduler, dispatch, and route modules all need the project directory but can't import each other.

## Project Isolation

Each project has its own SQLite database in its own `.maistro/` directory. Opening a new project closes the old database connection and opens a new one. There is no cross-project state — the only thing that spans projects is the app-level recent-projects list.

The worker sweeps stale dispatches once per project open, resetting any that were mid-flight when the process last died.

## Relationship to Other Systems

- [Storage](storage.md) manages the database lifecycle that project open/close drives
- [Git Integration](git-integration.md) provides the initialization operations (repo, hook, gitignore)
- [Dispatch Engine](dispatch-engine.md) enforces the active-dispatch guard and receives the wake notification
