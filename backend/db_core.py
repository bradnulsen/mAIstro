"""Database connection management, schema, and init.

Owns the persistent aiosqlite connection, the readers-draining protocol that
coordinates project switches, and the SCHEMA_SQL / SEED_SQL strings applied
on every init_db. Migration logic is deferred to backend.db_migrations so
this module stays focused on connection + schema.
"""

import asyncio
import contextlib
import logging
import os

import aiosqlite

log = logging.getLogger("maistro.database")

DB_PATH: str | None = None
_conn: aiosqlite.Connection | None = None

# ── Project-switch coordination (R3) ──────────────────────
# Reader-counting guard prevents close_db() from pulling the connection
# out from under in-flight DB operations. See storage.md § Project-Switch
# Coordination for the full spec.
_active_readers: int = 0
_closing: bool = False
_readers_drained = asyncio.Event()
_readers_drained.set()  # starts drained (no readers)


def get_db_path(project_dir: str) -> str:
    maistro_dir = os.path.join(project_dir, ".maistro")
    os.makedirs(maistro_dir, exist_ok=True)
    return os.path.join(maistro_dir, "maistro.db")


@contextlib.asynccontextmanager
async def db_read_guard():
    """Acquire a read guard around a DB operation span.

    While any guard is held, close_db() blocks. Once close_db() sets the
    _closing flag, new guards raise instead of entering — callers should
    let the operation fail gracefully (the project is switching).
    """
    global _active_readers
    if _closing:
        raise RuntimeError("Database is closing — project switch in progress")
    _active_readers += 1
    _readers_drained.clear()
    try:
        yield
    finally:
        _active_readers -= 1
        if _active_readers == 0:
            _readers_drained.set()


async def get_db() -> aiosqlite.Connection:
    """Return the persistent database connection, creating it if needed."""
    global _conn
    if _conn is None:
        assert DB_PATH, "Database not initialized — call init_db first"
        _conn = await aiosqlite.connect(DB_PATH)
        _conn._conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA synchronous=NORMAL")
        await _conn.execute("PRAGMA cache_size=-8000")
        await _conn.execute("PRAGMA foreign_keys=ON")
        await _conn.execute("PRAGMA temp_store=MEMORY")
        await _conn.execute("PRAGMA mmap_size=67108864")  # 64 MB memory-mapped I/O
    return _conn


async def close_db():
    """Close the persistent connection, waiting for active readers to drain.

    Sets _closing to prevent new readers, waits for in-flight operations
    to finish, then closes the connection and resets state.
    """
    global _conn, DB_PATH, _closing
    _closing = True
    # Wait for active readers to drain (timeout prevents deadlock)
    try:
        await asyncio.wait_for(_readers_drained.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        log.warning("[database] Timed out waiting for %d active readers to drain — closing anyway", _active_readers)
    if _conn is not None:
        await _conn.close()
        _conn = None
    DB_PATH = None
    # Clear job-property cache so the next project doesn't see stale defs.
    # Deferred import avoids a db_core ↔ db_jobs cycle.
    from backend import db_jobs
    db_jobs._reset_caches()
    _closing = False


async def init_db(project_dir: str):
    """Open or create the project DB, apply schema, seed defaults, run migrations."""
    global DB_PATH
    # Close previous connection if switching projects
    await close_db()
    DB_PATH = get_db_path(project_dir)
    db = await get_db()
    await db.executescript(SCHEMA_SQL)
    await db.executescript(SEED_SQL)
    # Migrations are isolated in their own module — keep the core init path
    # uncluttered by table-recreation logic.
    from backend.db_migrations import run_migrations
    await run_migrations(db)
    await db.commit()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_property_defs (
    key TEXT PRIMARY KEY,
    default_value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string'
);

CREATE TABLE IF NOT EXISTS job_properties (
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT REFERENCES job_property_defs(key),
    value TEXT NOT NULL,
    PRIMARY KEY (job_id, key)
);

-- `tasks` holds the atomic identity of a unit of work plus its current
-- queue placement. Per the design, tasks are append-only after creation —
-- reply/resume don't mutate, they create new tasks and coalesce the
-- original. Per-execution outcome data lives in `task_executions`; the
-- lifecycle log lives in `task_events`. The `status` column here is a
-- materialized cache of the latest lifecycle event for cheap queue queries.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    trigger TEXT NOT NULL,
    trigger_detail TEXT,
    context TEXT,
    resume_session_id TEXT,
    approval TEXT,
    created_at DATETIME DEFAULT (datetime('now')),
    queued_at DATETIME,
    sort_order INTEGER,
    coalesced_id INTEGER REFERENCES tasks(id)
);

-- One row per task that actually ran (or attempted to run). The PK is
-- task_id so the relationship is 1:0..1 — never re-executed in the
-- current model. Subordinates have no row here; they inherit the root's
-- execution via the `tasks_resolved` view's JOIN.
CREATE TABLE IF NOT EXISTS task_executions (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    session_id TEXT,
    start_commit TEXT,
    result_commit TEXT,
    stop_reason TEXT,
    num_turns INTEGER,
    cost_usd REAL,
    started_at DATETIME,
    completed_at DATETIME,
    error TEXT,
    -- Per-task git worktree (workspace isolation Phase 3).
    worktree_path TEXT,
    task_branch TEXT,
    -- Stash safety net for non-success terminals (workspace isolation Phase 2).
    -- Dead in normal Phase 3 operation; preserved as a fallback.
    orphan_stash_ref TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id),
    title TEXT,
    cli_session_id TEXT,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    detail TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events (task_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_task_events_type
    ON task_events (task_id, event, id DESC);

CREATE TABLE IF NOT EXISTS mcp_servers (
    name TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    -- Set when the row was installed from a .mcpb bundle; cleanup on
    -- delete must rm the extracted directory so disk doesn't leak.
    bundle_dir TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_coalesced
    ON tasks (coalesced_id);

CREATE INDEX IF NOT EXISTS idx_tasks_job_status
    ON tasks (job_id, status);

CREATE INDEX IF NOT EXISTS idx_tasks_status_worker
    ON tasks (status, approval, coalesced_id);

-- Drop the old column-based completed_at index (the column moved to
-- task_executions). Idempotent: safe whether or not it existed.
DROP INDEX IF EXISTS idx_tasks_completed_at;

CREATE INDEX IF NOT EXISTS idx_task_executions_completed_at
    ON task_executions (completed_at);

-- View: tasks_resolved
-- Centralized read seam for coalescing-aware queries. Outcome data
-- (per-execution) is JOINed in from `task_executions`; for subordinates,
-- the JOIN reaches through to the root's execution via `r.id = t.coalesced_id`.
-- Intrinsic columns (id, job_id, trigger, context, etc.) are passed
-- through unchanged. Depth-1 invariant on coalesced_id makes the single
-- LEFT JOIN sufficient.
-- DROP+CREATE keeps the view definition in sync on every init_db run
-- (views hold no data, so this is safe).
DROP VIEW IF EXISTS tasks_resolved;
CREATE VIEW tasks_resolved AS
SELECT
    t.id,
    t.job_id,
    t.status,
    t.trigger,
    t.trigger_detail,
    t.context,
    t.coalesced_id,
    t.approval,
    t.created_at,
    t.queued_at,
    t.sort_order,
    t.resume_session_id,
    -- Outcome columns: prefer self's execution row, fall through to root's.
    COALESCE(te.session_id, re.session_id) AS session_id,
    COALESCE(te.start_commit, re.start_commit) AS start_commit,
    COALESCE(te.result_commit, re.result_commit) AS result_commit,
    COALESCE(te.stop_reason, re.stop_reason) AS stop_reason,
    COALESCE(te.num_turns, re.num_turns) AS num_turns,
    COALESCE(te.cost_usd, re.cost_usd) AS cost_usd,
    COALESCE(te.started_at, re.started_at) AS started_at,
    COALESCE(te.completed_at, re.completed_at) AS completed_at,
    COALESCE(te.error, re.error) AS error,
    COALESCE(te.orphan_stash_ref, re.orphan_stash_ref) AS orphan_stash_ref,
    COALESCE(te.worktree_path, re.worktree_path) AS worktree_path,
    COALESCE(te.task_branch, re.task_branch) AS task_branch,
    -- Markers so consumers can distinguish a real run from a fall-through.
    CASE WHEN t.coalesced_id IS NOT NULL THEN 1 ELSE 0 END AS is_subordinate,
    COALESCE(t.coalesced_id, t.id) AS effective_root_id
FROM tasks t
LEFT JOIN task_executions te ON te.task_id = t.id
LEFT JOIN tasks r ON r.id = t.coalesced_id
LEFT JOIN task_executions re ON re.task_id = r.id;

-- Depth-1 invariant: a task's coalesced_id must point at a root
-- (not at another subordinate), and a task that has its own
-- subordinates cannot itself become a subordinate. Every coalescing
-- code path (coalesce_under, merge_tasks, _flatten_coalesce) is
-- required to maintain this; the triggers turn it into a hard
-- DB-level invariant so future regressions fail loudly instead of
-- silently producing depth-N chains.
DROP TRIGGER IF EXISTS tasks_depth1_insert;
CREATE TRIGGER tasks_depth1_insert
BEFORE INSERT ON tasks
WHEN NEW.coalesced_id IS NOT NULL AND
     EXISTS (SELECT 1 FROM tasks WHERE id = NEW.coalesced_id AND coalesced_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'depth-1 violation: coalesced_id points at a subordinate');
END;

DROP TRIGGER IF EXISTS tasks_depth1_update_target;
CREATE TRIGGER tasks_depth1_update_target
BEFORE UPDATE OF coalesced_id ON tasks
WHEN NEW.coalesced_id IS NOT NULL AND
     EXISTS (SELECT 1 FROM tasks WHERE id = NEW.coalesced_id AND coalesced_id IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'depth-1 violation: coalesced_id points at a subordinate');
END;

DROP TRIGGER IF EXISTS tasks_depth1_update_self;
CREATE TRIGGER tasks_depth1_update_self
BEFORE UPDATE OF coalesced_id ON tasks
WHEN NEW.coalesced_id IS NOT NULL AND OLD.coalesced_id IS NULL AND
     EXISTS (SELECT 1 FROM tasks WHERE coalesced_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'depth-1 violation: task has subordinates; flatten before re-parenting');
END;

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages (session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_chat_events_session
    ON chat_events (session_id);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_cli_session
    ON chat_sessions (cli_session_id);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_task
    ON chat_sessions (task_id);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_job
    ON chat_sessions (job_id);

CREATE INDEX IF NOT EXISTS idx_job_properties_key
    ON job_properties (key);

CREATE INDEX IF NOT EXISTS idx_job_properties_job_id
    ON job_properties (job_id);

CREATE TABLE IF NOT EXISTS governor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    task_count_at_trigger INTEGER,
    findings_count INTEGER DEFAULT 0,
    started_at DATETIME DEFAULT (datetime('now')),
    completed_at DATETIME,
    error TEXT
);

CREATE TABLE IF NOT EXISTS governor_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    execution_result TEXT,
    governor_run_id INTEGER REFERENCES governor_runs(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT (datetime('now')),
    updated_at DATETIME DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_governor_findings_run
    ON governor_findings (governor_run_id);

CREATE INDEX IF NOT EXISTS idx_governor_findings_status
    ON governor_findings (status);

CREATE INDEX IF NOT EXISTS idx_governor_runs_started
    ON governor_runs (started_at DESC);

"""

SEED_SQL = """
INSERT OR IGNORE INTO job_property_defs (key, default_value, type) VALUES
    ('summary', '', 'string'),
    ('description', '', 'string'),
    ('model', 'sonnet', 'string'),
    ('allowed_tools', '[]', 'json'),
    ('mcp_servers', '[]', 'json'),
    ('subscriptions', '[]', 'json'),
    ('coalesce_tasks', 'false', 'boolean'),
    ('sort_order', '0', 'integer'),
    ('schedule', '', 'string'),
    ('timeout', '900', 'integer'),
    ('max_turns', '100', 'integer'),
    ('cascades_from', '[]', 'json'),
    ('require_approval', 'false', 'boolean'),
    ('allowed_internal_tools', '[]', 'json'),
    ('allowed_dispatch_targets', '[]', 'json');

INSERT OR IGNORE INTO config (key, value) VALUES ('queue_auto_dispatch', 'false');
INSERT OR IGNORE INTO config (key, value) VALUES ('agent_dispatch_depth_limit', '5');
INSERT OR IGNORE INTO config (key, value) VALUES ('governor_task_counter', '0');
"""
