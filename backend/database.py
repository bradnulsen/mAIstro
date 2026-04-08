"""SQLite database layer for mAistro — schema, init, CRUD helpers."""

import asyncio
import aiosqlite
import contextlib
import json
import logging
import os
import re
import uuid

log = logging.getLogger("maistro.database")

DB_PATH: str | None = None
_conn: aiosqlite.Connection | None = None
_property_defs_cache: list | None = None

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
    global _conn, DB_PATH, _property_defs_cache, _closing
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
    _property_defs_cache = None
    _closing = False


async def init_db(project_dir: str):
    global DB_PATH
    # Close previous connection if switching projects
    await close_db()
    DB_PATH = get_db_path(project_dir)
    db = await get_db()
    await db.executescript(SCHEMA_SQL)
    await db.executescript(SEED_SQL)
    await _run_migrations(db)
    await db.commit()


async def _run_migrations(db: aiosqlite.Connection):
    """Apply incremental schema changes to existing databases."""
    # M3: Convert jobs.id from TEXT slug to INTEGER AUTOINCREMENT (run first)
    job_col_types = {c["name"]: c["type"] for c in await db.execute_fetchall("PRAGMA table_info(jobs)")}
    if job_col_types.get("id") == "TEXT":
        log.info("[database] Migration M3: converting jobs.id from TEXT slug to INTEGER")
        await db.execute("PRAGMA foreign_keys=OFF")

        old_jobs = await db.execute_fetchall("SELECT id, name, created_at FROM jobs")
        slug_to_int: dict[str, int] = {j["id"]: i + 1 for i, j in enumerate(old_jobs)}

        # Recreate jobs with INTEGER PK; old id column was the slug
        await db.execute("ALTER TABLE jobs RENAME TO _jobs_old")
        await db.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """)
        for j in old_jobs:
            await db.execute(
                "INSERT INTO jobs (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                (slug_to_int[j["id"]], j["id"], j["name"], j["created_at"])
            )
        await db.execute("DROP TABLE _jobs_old")

        # Recreate job_properties with INTEGER job_id
        old_props = await db.execute_fetchall("SELECT job_id, key, value FROM job_properties")
        await db.execute("ALTER TABLE job_properties RENAME TO _job_properties_old")
        await db.execute("""
            CREATE TABLE job_properties (
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                key TEXT REFERENCES job_property_defs(key),
                value TEXT NOT NULL,
                PRIMARY KEY (job_id, key)
            )
        """)
        for p in old_props:
            new_jid = slug_to_int.get(p["job_id"])
            if new_jid:
                await db.execute(
                    "INSERT OR IGNORE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
                    (new_jid, p["key"], p["value"])
                )
        await db.execute("DROP TABLE _job_properties_old")

        # Recreate tasks with job_id INTEGER
        old_tasks = await db.execute_fetchall("SELECT * FROM tasks")
        task_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(tasks)")]
        await db.execute("ALTER TABLE tasks RENAME TO _tasks_old")
        await db.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                trigger TEXT NOT NULL,
                trigger_detail TEXT,
                context TEXT,
                session_id TEXT,
                resume_session_id TEXT,
                approval TEXT,
                start_commit TEXT,
                stop_reason TEXT,
                num_turns INTEGER,
                cost_usd REAL,
                created_at DATETIME DEFAULT (datetime('now')),
                started_at DATETIME,
                completed_at DATETIME,
                result_commit TEXT,
                error TEXT,
                queued_at DATETIME,
                sort_order INTEGER,
                coalesced_id INTEGER REFERENCES tasks(id)
            )
        """)
        new_task_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(tasks)")]
        copy_cols = [c for c in task_cols if c in set(new_task_cols) and c != "job_id"]
        for t in old_tasks:
            row = dict(t)
            new_jid = slug_to_int.get(row["job_id"])
            if new_jid is None:
                continue
            row["job_id"] = new_jid
            all_cols = ["job_id"] + copy_cols
            vals = [row[c] for c in all_cols]
            ph = ", ".join("?" * len(all_cols))
            await db.execute(f"INSERT INTO tasks ({', '.join(all_cols)}) VALUES ({ph})", vals)
        await db.execute("DROP TABLE _tasks_old")

        # Recreate chat_sessions with job_id INTEGER
        old_sessions = await db.execute_fetchall("SELECT * FROM chat_sessions")
        cs_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")]
        await db.execute("ALTER TABLE chat_sessions RENAME TO _chat_sessions_old")
        await db.execute("""
            CREATE TABLE chat_sessions (
                id TEXT PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                task_id INTEGER REFERENCES tasks(id),
                title TEXT,
                cli_session_id TEXT,
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """)
        new_cs_cols = [c["name"] for c in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")]
        copy_cs_cols = [c for c in cs_cols if c in set(new_cs_cols) and c != "job_id"]
        for s in old_sessions:
            row = dict(s)
            old_jid = row.get("job_id")
            new_jid = slug_to_int.get(old_jid) if old_jid else None
            row["job_id"] = new_jid
            all_cols = ["job_id"] + copy_cs_cols
            vals = [row[c] for c in all_cols]
            ph = ", ".join("?" * len(all_cols))
            await db.execute(f"INSERT INTO chat_sessions ({', '.join(all_cols)}) VALUES ({ph})", vals)
        await db.execute("DROP TABLE _chat_sessions_old")

        await db.execute("PRAGMA foreign_keys=ON")
        log.info("[database] Migration M3: done — %d jobs converted", len(old_jobs))

    # M2: Rename goals→jobs tables and goal_id→job_id columns (run before M1)
    tables = {r["name"] for r in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    if "goals" in tables:
        log.info("[database] Migration M2: renaming goals→jobs tables and columns")
        # SCHEMA_SQL may have already created empty jobs/job_property_defs/job_properties
        # tables — drop them so we can rename the real ones from goals.
        for empty_table in ("jobs", "job_property_defs", "job_properties"):
            if empty_table in tables:
                await db.execute(f"DROP TABLE {empty_table}")
        await db.execute("ALTER TABLE goals RENAME TO jobs")
        await db.execute("ALTER TABLE goal_property_defs RENAME TO job_property_defs")
        await db.execute("ALTER TABLE goal_properties RENAME TO job_properties")
        await db.execute("ALTER TABLE tasks RENAME COLUMN goal_id TO job_id")
        await db.execute("ALTER TABLE chat_sessions RENAME COLUMN goal_id TO job_id")
        # job_properties.goal_id column rename (table was just renamed from goal_properties)
        await db.execute("ALTER TABLE job_properties RENAME COLUMN goal_id TO job_id")
        log.info("[database] Migration M2: done")

    # Cleanup: drop any leftover _*_old tables from previous migration runs
    tables = {r["name"] for r in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    for stale in ("_tasks_old", "_jobs_old", "_job_properties_old", "_chat_sessions_old"):
        if stale in tables:
            await db.execute(f"DROP TABLE {stale}")
            log.warning("[database] Dropped stale migration table: %s", stale)

    # Add cli_session_id to chat_sessions if missing (needed for session resume)
    cs_cols = {c["name"] for c in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")}
    if "cli_session_id" not in cs_cols:
        log.info("[database] Migration: adding cli_session_id to chat_sessions")
        await db.execute("ALTER TABLE chat_sessions ADD COLUMN cli_session_id TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_cli_session "
            "ON chat_sessions (cli_session_id)"
        )
        await db.commit()

    # M1: Add slug column to jobs if missing (added after INTEGER PK migration)
    cols = await db.execute_fetchall("PRAGMA table_info(jobs)")
    col_names = {c["name"] for c in cols}
    if "slug" not in col_names:
        log.info("[database] Migration M1: adding slug column to jobs")
        await db.execute("ALTER TABLE jobs ADD COLUMN slug TEXT")
        rows = await db.execute_fetchall("SELECT id, name FROM jobs")
        for row in rows:
            slug = slugify(row["name"])
            # Ensure uniqueness by appending ID on collision
            existing = await db.execute_fetchall(
                "SELECT id FROM jobs WHERE slug = ? AND id != ?", (slug, row["id"])
            )
            if existing:
                slug = f"{slug}-{row['id']}"
            await db.execute("UPDATE jobs SET slug = ? WHERE id = ?", (slug, row["id"]))
        log.info("[database] Migration M1: done")


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

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    trigger TEXT NOT NULL,
    trigger_detail TEXT,
    context TEXT,
    session_id TEXT,
    resume_session_id TEXT,
    approval TEXT,
    start_commit TEXT,
    stop_reason TEXT,
    num_turns INTEGER,
    cost_usd REAL,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    result_commit TEXT,
    error TEXT,
    queued_at DATETIME,
    sort_order INTEGER,
    coalesced_id INTEGER REFERENCES tasks(id)
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
    enabled INTEGER DEFAULT 1
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

CREATE INDEX IF NOT EXISTS idx_tasks_completed_at
    ON tasks (completed_at);

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
    ('max_turns', '50', 'integer'),
    ('cascades_from', '[]', 'json'),
    ('require_approval', 'false', 'boolean'),
    ('allowed_internal_tools', '[]', 'json'),
    ('allowed_dispatch_targets', '[]', 'json');

INSERT OR IGNORE INTO config (key, value) VALUES ('queue_auto_dispatch', 'false');
INSERT OR IGNORE INTO config (key, value) VALUES ('agent_dispatch_depth_limit', '5');
"""


def slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug


# ── Job CRUD ─────────────────────────────────────────────

async def create_job(name: str, properties: dict | None = None) -> dict:
    slug = slugify(name)
    db = await get_db()
    cursor = await db.execute("INSERT INTO jobs (slug, name) VALUES (?, ?)", (slug, name))
    job_id = cursor.lastrowid
    if properties:
        for key, value in properties.items():
            val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            await db.execute(
                "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
                (job_id, key, val)
            )
    await db.commit()
    return await get_job(job_id)


async def _get_property_defs() -> list:
    """Return job_property_defs rows, using a module-level cache."""
    global _property_defs_cache
    if _property_defs_cache is None:
        conn = await get_db()
        rows = await conn.execute_fetchall("SELECT key, default_value, type FROM job_property_defs")
        _property_defs_cache = [dict(r) for r in rows]
    return _property_defs_cache


async def get_job(job_id: int, running_ids: set | None = None) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id, slug, name, created_at FROM jobs WHERE id = ?", (job_id,)
    )
    if not row:
        return None
    job = dict(row[0])

    # Get all property defs with defaults, override with job-specific values
    defs = await _get_property_defs()
    props = {}
    for d in defs:
        props[d["key"]] = _cast_property(d["default_value"], d["type"])

    job_props = await db.execute_fetchall(
        "SELECT key, value FROM job_properties WHERE job_id = ?", (job_id,)
    )
    def_types = {d["key"]: d["type"] for d in defs}
    for p in job_props:
        props[p["key"]] = _cast_property(p["value"], def_types.get(p["key"], "string"))

    slug_rows = await db.execute_fetchall("SELECT slug, id FROM jobs")
    _normalize_int_list_props(props, {r["slug"]: r["id"] for r in slug_rows})

    # Derive running status from tasks table
    if running_ids is not None:
        props["running"] = job_id in running_ids
    else:
        r = await db.execute_fetchall(
            "SELECT 1 FROM tasks WHERE job_id = ? AND status = 'active' LIMIT 1",
            (job_id,)
        )
        props["running"] = bool(r)

    job["properties"] = props
    return job


async def list_jobs() -> list[dict]:
    """Return all jobs. Uses 3 batch queries instead of 2N+2 (N+1 avoided)."""
    conn = await get_db()
    job_rows = await conn.execute_fetchall("SELECT id, slug, name, created_at FROM jobs")
    if not job_rows:
        return []

    # Task state counts per job for command bar indicators
    state_rows = await conn.execute_fetchall(
        "SELECT job_id, status, COUNT(*) as cnt FROM tasks "
        "WHERE status IN ('pending', 'queued', 'active') GROUP BY job_id, status"
    )
    running_ids = set()
    pending_counts: dict[int, int] = {}
    queued_counts: dict[int, int] = {}
    for r in state_rows:
        jid, st, cnt = r["job_id"], r["status"], r["cnt"]
        if st == "active":
            running_ids.add(jid)
        elif st == "pending":
            pending_counts[jid] = cnt
        elif st == "queued":
            queued_counts[jid] = cnt

    prop_rows = await conn.execute_fetchall("SELECT job_id, key, value FROM job_properties")
    props_by_job: dict[int, dict] = {}
    for p in prop_rows:
        props_by_job.setdefault(p["job_id"], {})[p["key"]] = p["value"]

    defs = await _get_property_defs()
    def_defaults = {d["key"]: d["default_value"] for d in defs}
    def_types = {d["key"]: d["type"] for d in defs}

    default_props = {k: _cast_property(v, def_types[k]) for k, v in def_defaults.items()}
    slug_to_id = {row["slug"]: row["id"] for row in job_rows}

    jobs = []
    for row in job_rows:
        job = dict(row)
        props = default_props.copy()
        for key, value in props_by_job.get(job["id"], {}).items():
            props[key] = _cast_property(value, def_types.get(key, "string"))
        _normalize_int_list_props(props, slug_to_id)
        props["running"] = job["id"] in running_ids
        props["pending_count"] = pending_counts.get(job["id"], 0)
        props["queued_count"] = queued_counts.get(job["id"], 0)
        job["properties"] = props
        jobs.append(job)
    jobs.sort(key=lambda j: j["properties"].get("sort_order", 0))
    return jobs


async def update_job(job_id: int, updates: dict) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None

    if "name" in updates:
        new_name = updates.pop("name")
        new_slug = slugify(new_name)
        await db.execute("UPDATE jobs SET name = ?, slug = ? WHERE id = ?", (new_name, new_slug, job_id))

    for key, value in updates.items():
        val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        await db.execute(
            "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
            (job_id, key, val)
        )
    await db.commit()
    return await get_job(job_id)


async def reorder_jobs(job_ids: list[int]):
    """Set sort_order for multiple jobs in a single transaction."""
    db = await get_db()
    await db.executemany(
        "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, 'sort_order', ?)",
        [(job_id, str(i)) for i, job_id in enumerate(job_ids)],
    )
    await db.commit()


async def delete_job(job_id: int) -> bool:
    """Delete a job. CASCADE FKs on tasks, chat_sessions, and job_properties
    automatically remove dependent rows."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cursor.rowcount > 0


async def get_cascade_targets(completed_job_id: int) -> list[dict]:
    """Return jobs that cascade from the completed job.

    Each job may declare cascades_from: a list of upstream job IDs.
    When a job completes, we find all jobs whose cascades_from list
    includes the completed job and enqueue them.
    """
    conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT job_id, value FROM job_properties WHERE key = 'cascades_from'"
    )

    # Parse JSON, coerce to ints, and filter to exact membership
    slug_rows = await conn.execute_fetchall("SELECT slug, id FROM jobs")
    slug_to_id = {r["slug"]: r["id"] for r in slug_rows}
    candidate_ids = []
    for r in rows:
        try:
            upstreams = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(upstreams, list):
            continue
        # Coerce any stale slug strings to integer IDs
        resolved = []
        for item in upstreams:
            if isinstance(item, int):
                resolved.append(item)
            else:
                try:
                    resolved.append(int(item))
                except (ValueError, TypeError):
                    mapped = slug_to_id.get(str(item))
                    if mapped is not None:
                        resolved.append(mapped)
        if completed_job_id in resolved:
            candidate_ids.append(r["job_id"])

    if not candidate_ids:
        return []

    placeholders = ",".join("?" * len(candidate_ids))

    job_rows = await conn.execute_fetchall(
        f"SELECT id, slug, name, created_at FROM jobs WHERE id IN ({placeholders})",
        candidate_ids,
    )
    if not job_rows:
        return []

    running_rows = await conn.execute_fetchall(
        "SELECT DISTINCT job_id FROM tasks WHERE status = 'active'"
    )
    running_ids = {r["job_id"] for r in running_rows}

    prop_rows = await conn.execute_fetchall(
        f"SELECT job_id, key, value FROM job_properties WHERE job_id IN ({placeholders})",
        candidate_ids,
    )
    props_by_job: dict[int, dict] = {}
    for p in prop_rows:
        props_by_job.setdefault(p["job_id"], {})[p["key"]] = p["value"]

    defs = await _get_property_defs()
    def_defaults = {d["key"]: d["default_value"] for d in defs}
    def_types = {d["key"]: d["type"] for d in defs}
    default_props = {k: _cast_property(v, def_types[k]) for k, v in def_defaults.items()}

    jobs = []
    for row in job_rows:
        job = dict(row)
        props = default_props.copy()
        for key, value in props_by_job.get(job["id"], {}).items():
            props[key] = _cast_property(value, def_types.get(key, "string"))
        props["running"] = job["id"] in running_ids
        job["properties"] = props
        jobs.append(job)
    return jobs


# ── Tasks (atomic work items) ─────────────────────────────

async def enqueue_task(job_id: int, trigger: str,
                       trigger_detail: str | None = None,
                       context: str | None = None) -> int:
    """Enqueue an atomic task. Coalescing links via coalesced_id instead of mutating data.

    Coalescing rules:
    - 'schedule' always coalesces globally
    - 'commit', 'dependency', and 'agent' coalesce with other pending tasks of the same type
    - coalesce_tasks=true coalesces globally
    - All other triggers (manual, resume, reply) never coalesce
    """
    db = await get_db()

    # Fetch only the two properties needed
    prop_rows = await db.execute_fetchall(
        "SELECT key, value FROM job_properties WHERE job_id = ? AND key IN ('coalesce_tasks', 'require_approval')",
        (job_id,)
    )
    job_props = {r["key"]: r["value"] for r in prop_rows}
    coalesce_global = (trigger == "schedule") or (job_props.get("coalesce_tasks", "").lower() == "true")
    coalesce_same_type = trigger in ("commit", "cascade", "agent")

    # Approval gate: manual tasks bypass, others check job property
    approval = None
    if trigger != "manual" and job_props.get("require_approval", "").lower() == "true":
        approval = "pending"

    # Auto-queueing: when enabled, new tasks skip pending and go directly to queued
    from backend.state import utcnow
    queued_at = None
    status = "pending"
    auto_queue = await get_config("queue_auto_dispatch")
    if auto_queue == "true":
        queued_at = utcnow()
        status = "queued"

    # Insert the atomic task (not committed yet — coalesce update shares the transaction)
    now = utcnow()
    cursor = await db.execute(
        """INSERT INTO tasks (job_id, status, trigger, trigger_detail, context, approval, queued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, status, trigger, trigger_detail, context, approval, queued_at)
    )
    new_id = cursor.lastrowid

    # Emit lifecycle events: dispatched (and queued if auto-queued)
    await db.execute(
        "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, 'dispatched', ?, ?)",
        (new_id, trigger, now)
    )
    if queued_at:
        await db.execute(
            "INSERT INTO task_events (task_id, event, created_at) VALUES (?, 'queued', ?)",
            (new_id, queued_at)
        )

    # Coalesce: find an existing pending root and link this task to it
    root_id = None
    if coalesce_global or coalesce_same_type:
        query = """SELECT id FROM tasks
                   WHERE job_id = ? AND status IN ('pending', 'queued')
                     AND coalesced_id IS NULL AND id != ?"""
        params: list = [job_id, new_id]
        if coalesce_same_type and not coalesce_global:
            query += " AND trigger = ?"
            params.append(trigger)
        query += " ORDER BY created_at ASC LIMIT 1"
        rows = await db.execute_fetchall(query, params)
        if rows:
            root_id = rows[0]["id"]
            await db.execute(
                "UPDATE tasks SET coalesced_id = ? WHERE id = ?",
                (root_id, new_id)
            )

    # Single commit: insert + optional coalesce link land atomically
    await db.commit()
    return root_id if root_id is not None else new_id


async def get_task(task_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT t.*, j.name as job_name FROM tasks t JOIN jobs j ON j.id = t.job_id WHERE t.id = ?",
        (task_id,)
    )
    if not rows:
        return None
    task = dict(rows[0])
    apply_events(task, await get_task_events(task_id, db))
    return task


async def get_agent_dispatch_depth(task_id: int) -> int:
    """Trace the agent dispatch chain back from a task and return its depth.

    Each agent-triggered task has trigger_detail of format 'source_job_id#source_task_id'.
    Follows the chain until a non-agent trigger is found or the chain breaks.
    Returns the number of agent dispatch hops (0 if the task itself is not agent-triggered).
    """
    conn = await get_db()
    depth = 0
    current_id = task_id
    seen = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        rows = await conn.execute_fetchall(
            "SELECT trigger, trigger_detail FROM tasks WHERE id = ?",
            (current_id,)
        )
        if not rows:
            break
        row = rows[0]
        if row["trigger"] != "agent":
            break
        depth += 1
        # trigger_detail is 'source_job_id#source_task_id'
        detail = row["trigger_detail"] or ""
        parts = detail.rsplit("#", 1)
        if len(parts) == 2 and parts[1].isdigit():
            current_id = int(parts[1])
        else:
            break
    return depth


async def get_task_queue(limit: int = 50) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.*, j.name as job_name,
                  COUNT(sub.id) as subordinate_count
           FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           LEFT JOIN tasks sub ON sub.coalesced_id = t.id
           WHERE t.coalesced_id IS NULL
           GROUP BY t.id
           ORDER BY t.created_at DESC LIMIT ?""",
        (limit,)
    )
    return [dict(r) for r in rows]


async def get_oldest_queued_task() -> dict | None:
    """Get the highest-priority queued task (skips pending, approval-pending, and subordinates)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.*, j.name as job_name FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.status = 'queued'
             AND (t.approval IS NULL OR t.approval = 'approved')
             AND t.coalesced_id IS NULL
           ORDER BY t.sort_order IS NULL, t.sort_order ASC, t.created_at ASC
           LIMIT 1"""
    )
    return dict(rows[0]) if rows else None


async def update_task(task_id: int, _commit: bool = True, **kwargs):
    db = await get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    await db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
    if _commit:
        await db.commit()


async def update_tasks_batch(task_ids: list[int], **kwargs):
    """Update multiple tasks with the same field values in a single statement.

    Always commits — even when task_ids is empty — to flush any prior
    uncommitted writes in the same transaction (e.g. a preceding
    update_task with _commit=False).
    """
    db = await get_db()
    if task_ids:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        placeholders = ",".join("?" * len(task_ids))
        vals = list(kwargs.values()) + list(task_ids)
        await db.execute(f"UPDATE tasks SET {sets} WHERE id IN ({placeholders})", vals)
    await db.commit()


# ── Task Status State Machine ─────────────────────────────

# Legal transitions: (current_status, new_status) -> allowed
LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    ("pending", "queued"),
    ("pending", "cancelled"),
    ("pending", "rejected"),
    ("queued", "pending"),
    ("queued", "active"),
    ("queued", "cancelled"),
    ("active", "completed"),
    ("active", "exhausted"),
    ("active", "failed"),
    ("active", "cancelled"),
    ("active", "timed_out"),
    ("active", "interrupted"),
}

TERMINAL_STATUSES = frozenset({"completed", "exhausted", "failed", "cancelled", "interrupted", "timed_out", "rejected"})

# Map status → event name for task_events writes.
_STATUS_TO_EVENT = {
    "pending": "restored",
    "queued": "queued",
    "active": "activated",
    "completed": "completed",
    "exhausted": "exhausted",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "interrupted": "interrupted",
    "rejected": "rejected",
}

# Reverse mapping: event name → status.
# Includes both current and legacy event names for backward compat with pre-migration data.
_EVENT_TO_STATUS = {
    # Current event names
    "dispatched": "pending",
    "restored": "pending",
    "queued": "queued",
    "activated": "active",
    "completed": "completed",
    "exhausted": "exhausted",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "interrupted": "interrupted",
    "rejected": "rejected",
    # Legacy event names (pre-migration data)
    "created": "pending",
    "unqueued": "pending",
    "active": "active",
}

_STATUS_EVENTS = frozenset(_EVENT_TO_STATUS.keys())


async def get_task_status_from_events(task_id: int, conn=None) -> str | None:
    """Derive current task status from the latest lifecycle event.

    Returns None if the task has no events.
    """
    if conn is None:
        conn = await get_db()
    placeholders = ",".join(f"'{e}'" for e in _STATUS_EVENTS)
    rows = await conn.execute_fetchall(
        f"SELECT event FROM task_events WHERE task_id = ? AND event IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id,)
    )
    if not rows:
        return None
    return _EVENT_TO_STATUS[rows[0]["event"]]


async def _resolve_task_status(task_id: int, conn) -> str:
    """Get current task status, backfilling from the status column if no events exist.

    Tasks created before task_events was introduced have no events. This
    synthesises the missing event so subsequent transitions work normally.
    Raises ValueError if the task doesn't exist.
    """
    from backend.state import utcnow
    status = await get_task_status_from_events(task_id, conn)
    if status is not None:
        return status
    rows = await conn.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        raise ValueError(f"Task #{task_id} not found")
    status = rows[0]["status"]
    synth_event = _STATUS_TO_EVENT.get(status, status)
    await conn.execute(
        "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
        (task_id, synth_event, "backfilled", utcnow())
    )
    log.warning("[database] Task #%d had no events — backfilled from status=%s", task_id, status)
    return status

# Map status to the timestamp column that should be set on transition
_STATUS_TIMESTAMP = {
    "queued": "queued_at",
    "active": "started_at",
    "completed": "completed_at",
    "exhausted": "completed_at",
    "failed": "completed_at",
    "cancelled": "completed_at",
    "timed_out": "completed_at",
    "interrupted": "completed_at",
    "rejected": "completed_at",
}


def _build_event_detail(new_status: str, fields: dict) -> str | None:
    """Extract event detail from transition fields for the audit log.

    Selects the most meaningful field for the event type: error message for
    failures, session_id for activation, result_commit for completion.
    """
    if new_status in ("failed", "cancelled", "timed_out", "interrupted", "exhausted"):
        return fields.get("error")
    if new_status == "active":
        parts = {}
        if "session_id" in fields:
            parts["session_id"] = fields["session_id"]
        if "start_commit" in fields:
            parts["start_commit"] = fields["start_commit"]
        return json.dumps(parts) if parts else None
    if new_status == "completed":
        return fields.get("result_commit")
    return None


async def transition_task(task_id: int, new_status: str, _commit: bool = True, **fields):
    """Transition a task to a new status with validation.

    All status changes must go through this function. Sets the corresponding
    timestamp column automatically and writes an event to task_events (Phase 1
    dual-write). Additional fields (error, result_commit, session_id, etc.)
    can be passed as kwargs.

    Raises ValueError if the transition is illegal.
    """
    conn = await get_db()
    current_status = await _resolve_task_status(task_id, conn)

    if (current_status, new_status) not in LEGAL_TRANSITIONS:
        raise ValueError(
            f"Illegal transition for task #{task_id}: {current_status} → {new_status}"
        )

    updates = {"status": new_status}

    # Set the corresponding timestamp if applicable
    ts_col = _STATUS_TIMESTAMP.get(new_status)
    if ts_col:
        from backend.state import utcnow
        updates[ts_col] = fields.pop(ts_col, utcnow())

    # When going back to pending, clear queued_at
    if new_status == "pending":
        updates["queued_at"] = None

    updates.update(fields)

    # Dual-write: event log (source of truth) + task record (materialized cache)
    event_type = _STATUS_TO_EVENT[new_status]
    event_detail = _build_event_detail(new_status, fields)
    event_ts = updates.get(ts_col) if ts_col else None
    if event_ts:
        await conn.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, event_detail, event_ts)
        )
    else:
        from backend.state import utcnow
        await conn.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, event_detail, utcnow())
        )

    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [task_id]
    await conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
    if _commit:
        await conn.commit()


async def transition_tasks_batch(task_ids: list[int], new_status: str, **fields):
    """Transition multiple tasks to the same new status.

    Skips validation per-task for performance — caller is responsible for
    ensuring all tasks are in a valid source state. Used for subordinate tasks.
    Writes events for each task in the batch (Phase 1 dual-write).
    """
    if not task_ids:
        return
    conn = await get_db()
    from backend.state import utcnow
    updates = {"status": new_status}
    ts_col = _STATUS_TIMESTAMP.get(new_status)
    now = utcnow()
    if ts_col:
        updates[ts_col] = fields.pop(ts_col, now)
    updates.update(fields)

    # Dual-write: batch-insert events for all tasks
    event_type = _STATUS_TO_EVENT[new_status]
    event_detail = _build_event_detail(new_status, fields)
    event_ts = updates.get(ts_col) if ts_col else now
    await conn.executemany(
        "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
        [(tid, event_type, event_detail, event_ts) for tid in task_ids]
    )

    sets = ", ".join(f"{k} = ?" for k in updates)
    placeholders = ",".join("?" * len(task_ids))
    vals = list(updates.values()) + list(task_ids)
    await conn.execute(f"UPDATE tasks SET {sets} WHERE id IN ({placeholders})", vals)
    await conn.commit()


# ── Task Event Queries ────────────────────────────────────

async def get_task_events(task_id: int, conn=None) -> list[dict]:
    """Full event chain for a task, ordered chronologically.

    This is the canonical read — every event the task has ever experienced,
    in the order it happened. The chain is append-only and immutable.
    """
    if conn is None:
        conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT event, detail, created_at FROM task_events WHERE task_id = ? ORDER BY id ASC",
        (task_id,)
    )
    return [dict(r) for r in rows]


async def get_task_events_batch(task_ids: list[int], conn=None) -> dict[int, list[dict]]:
    """Full event chains for multiple tasks, keyed by task_id.

    Same as get_task_events but batched — one query instead of N.
    """
    if not task_ids:
        return {}
    if conn is None:
        conn = await get_db()
    placeholders = ",".join("?" * len(task_ids))
    rows = await conn.execute_fetchall(
        f"SELECT task_id, event, detail, created_at FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id ASC",
        task_ids
    )
    result: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        tid = d.pop("task_id")
        result.setdefault(tid, []).append(d)
    return result


_TERMINAL_EVENTS = frozenset({"completed", "exhausted", "failed", "cancelled", "timed_out", "interrupted", "rejected"})


def status_from_events(events: list[dict]) -> str | None:
    """Derive current status from an ordered event list (ascending by id).

    Returns None if no lifecycle events are present.
    """
    status = None
    for e in events:
        mapped = _EVENT_TO_STATUS.get(e["event"])
        if mapped is not None:
            status = mapped
    return status


def apply_events(task: dict, events: list[dict]) -> None:
    """Overlay event-derived fields onto a task dict (mutates in place).

    Sets status, timestamps, durations, and the event chain itself.
    This is the single point where event data is materialized onto a task —
    every read path should call this rather than doing ad-hoc derivation.
    """
    task["events"] = events
    event_status = status_from_events(events)
    if event_status is not None:
        task["status"] = event_status
    task.update(timestamps_from_events(events))
    task["durations"] = compute_durations_from_events(events)


def _parse_event_timestamps(events: list[dict]) -> tuple:
    """Extract lifecycle timestamps from an ordered event list.

    Returns (created_at, queued_at, active_at, terminal_at) — each a datetime
    string or None.
    """
    created_at = None
    queued_at = None
    active_at = None
    terminal_at = None

    for e in events:
        evt = e["event"]
        ts = e["created_at"]
        if not isinstance(ts, str):
            continue  # skip bad data (e.g. integer queue positions from v4 migration)
        if evt in ("dispatched", "created") and created_at is None:
            created_at = ts
        elif evt == "queued":
            queued_at = ts
        elif evt in ("activated", "active"):
            active_at = ts
        elif evt in _TERMINAL_EVENTS:
            terminal_at = ts

    return created_at, queued_at, active_at, terminal_at


def timestamps_from_events(events: list[dict]) -> dict:
    """Derive the same timestamps as the legacy columns from the event log.

    Returns dict with queued_at, started_at, completed_at — matching the column
    names so the frontend doesn't need to change.
    """
    created_at, queued_at, active_at, terminal_at = _parse_event_timestamps(events)
    return {
        "queued_at": queued_at,
        "started_at": active_at,
        "completed_at": terminal_at,
    }


def compute_durations_from_events(events: list[dict]) -> dict:
    """Compute execution duration, queue wait, and total lifecycle from an event list.

    Returns a dict with keys: execution_duration, queue_wait, total_duration (all in seconds,
    None if the relevant event pair is missing).
    """
    created_at, queued_at, active_at, terminal_at = _parse_event_timestamps(events)

    def _diff_secs(a, b):
        if a is None or b is None:
            return None
        from datetime import datetime
        if isinstance(a, str):
            a = datetime.fromisoformat(a)
        if isinstance(b, str):
            b = datetime.fromisoformat(b)
        return max(0, (b - a).total_seconds())

    return {
        "execution_duration": _diff_secs(active_at, terminal_at),
        "queue_wait": _diff_secs(queued_at, active_at),
        "total_duration": _diff_secs(created_at, terminal_at),
    }


async def transfer_task(task_id: int, to_queued: bool):
    """Move a task between pending and queued columns. Sets or clears queued_at.
    Also transfers subordinate tasks to maintain group cohesion."""
    from backend.state import utcnow
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, coalesced_id FROM tasks WHERE id = ?",
        (task_id,)
    )
    if not rows:
        raise ValueError("Task not found")
    task = rows[0]
    current_status = await _resolve_task_status(task_id, db)
    if current_status not in ("pending", "queued"):
        raise ValueError("Can only transfer pre-execution tasks")
    if task["coalesced_id"] is not None:
        raise ValueError("Cannot transfer a subordinate — transfer its root instead")

    now = utcnow()
    new_status = "queued" if to_queued else "pending"
    queued_at = now if to_queued else None
    event_type = "queued" if to_queued else "restored"

    # Transfer root and all subordinates; clear sort_order (append to end of target column)
    await db.execute(
        "UPDATE tasks SET status = ?, queued_at = ?, sort_order = NULL WHERE id = ?",
        (new_status, queued_at, task_id)
    )
    # Dual-write: event for the root task
    await db.execute(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
        (task_id, event_type, now)
    )

    # Transfer subordinates
    sub_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE coalesced_id = ? AND status IN ('pending', 'queued')",
        (task_id,)
    )
    await db.execute(
        "UPDATE tasks SET status = ?, queued_at = ? WHERE coalesced_id = ? AND status IN ('pending', 'queued')",
        (new_status, queued_at, task_id)
    )
    # Dual-write: events for subordinates
    if sub_rows:
        await db.executemany(
            "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
            [(r["id"], event_type, now) for r in sub_rows]
        )

    await db.commit()
    return task_id


async def transfer_all_tasks(to_queued: bool) -> int:
    """Batch transfer: all pending→queued or all queued→pending.

    Skips subordinate tasks — they follow their root.
    Returns count of tasks transferred.
    """
    from backend.state import utcnow
    conn = await get_db()
    now = utcnow()

    source_status = "pending" if to_queued else "queued"
    target_status = "queued" if to_queued else "pending"
    queued_at = now if to_queued else None
    event_type = "queued" if to_queued else "restored"

    # Find root tasks (not subordinates) in the source status
    rows = await conn.execute_fetchall(
        "SELECT id FROM tasks WHERE status = ? AND coalesced_id IS NULL",
        (source_status,)
    )
    if not rows:
        return 0

    root_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(root_ids))

    # Transfer roots
    await conn.execute(
        f"UPDATE tasks SET status = ?, queued_at = ?, sort_order = NULL "
        f"WHERE id IN ({placeholders})",
        [target_status, queued_at] + root_ids
    )

    # Transfer subordinates of those roots
    sub_rows = await conn.execute_fetchall(
        f"SELECT id FROM tasks WHERE coalesced_id IN ({placeholders}) "
        f"AND status = ?",
        root_ids + [source_status]
    )
    if sub_rows:
        sub_ids = [s["id"] for s in sub_rows]
        sub_ph = ",".join("?" * len(sub_ids))
        await conn.execute(
            f"UPDATE tasks SET status = ?, queued_at = ? "
            f"WHERE id IN ({sub_ph})",
            [target_status, queued_at] + sub_ids
        )

    # Dual-write events
    all_ids = root_ids + [s["id"] for s in sub_rows]
    await conn.executemany(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
        [(tid, event_type, now) for tid in all_ids]
    )

    await conn.commit()
    return len(root_ids)


async def reorder_tasks(task_ids: list[int]):
    """Set explicit sort_order on pending tasks to control execution priority."""
    db = await get_db()
    await db.executemany(
        "UPDATE tasks SET sort_order = ? WHERE id = ? AND status IN ('pending', 'queued')",
        [(i, tid) for i, tid in enumerate(task_ids)],
    )
    await db.commit()


async def _flatten_coalesce(conn: aiosqlite.Connection, task_id: int, new_root_id: int):
    """After setting task_id.coalesced_id = new_root_id, re-point any tasks
    that were subordinates of task_id to new_root_id instead.
    Ensures coalesce groups are always flat (depth 1)."""
    await conn.execute(
        "UPDATE tasks SET coalesced_id = ? WHERE coalesced_id = ?",
        (new_root_id, task_id)
    )


async def coalesce_under(task_id: int, new_root_id: int):
    """Coalesce task_id (and any of its subordinates) under new_root_id."""
    conn = await get_db()
    await conn.execute(
        "UPDATE tasks SET coalesced_id = ? WHERE id = ?",
        (new_root_id, task_id)
    )
    await _flatten_coalesce(conn, task_id, new_root_id)
    await conn.commit()


async def get_subordinate_tasks(root_id: int) -> list[dict]:
    """Return all tasks with coalesced_id pointing to root_id."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.*, j.name as job_name FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.coalesced_id = ?""",
        (root_id,)
    )
    return [dict(r) for r in rows]


async def merge_tasks(task_ids: list[int]) -> int:
    """Merge pending same-job tasks. Returns root task ID."""
    if len(task_ids) < 2:
        raise ValueError("Need at least 2 tasks to merge")

    db = await get_db()
    placeholders = ",".join("?" * len(task_ids))
    rows = await db.execute_fetchall(
        f"""SELECT id, job_id, approval, coalesced_id, created_at
            FROM tasks WHERE id IN ({placeholders})
            ORDER BY created_at ASC""",
        task_ids,
    )

    if len(rows) != len(task_ids):
        raise ValueError("Some task IDs not found")

    events_by_task = await get_task_events_batch(task_ids, db)

    job_ids = set()
    for r in rows:
        task_status = status_from_events(events_by_task.get(r["id"], []))
        if task_status != "pending":
            raise ValueError(f"Task #{r['id']} is not pending — merge is a pending-column operation")
        if r["approval"] == "pending":
            raise ValueError(f"Task #{r['id']} is pending approval")
        if r["coalesced_id"] is not None:
            raise ValueError(f"Task #{r['id']} is already a subordinate")
        job_ids.add(r["job_id"])

    if len(job_ids) > 1:
        raise ValueError("Cannot merge tasks from different jobs")

    root_id = rows[0]["id"]
    sub_ids = [r["id"] for r in rows[1:]]
    sub_placeholders = ",".join("?" * len(sub_ids))
    await db.execute(
        f"UPDATE tasks SET coalesced_id = ? WHERE id IN ({sub_placeholders})",
        [root_id] + sub_ids,
    )
    # Flatten: any tasks that were subordinates of the newly-merged tasks
    # should now point directly to the new root
    for sid in sub_ids:
        await _flatten_coalesce(db, sid, root_id)

    from backend.state import utcnow

    await db.commit()
    return root_id


async def split_task(root_id: int) -> list[int]:
    """Split a root task — make all subordinates independent again."""
    db = await get_db()

    root_rows = await db.execute_fetchall(
        "SELECT id, job_id, coalesced_id FROM tasks WHERE id = ?",
        (root_id,)
    )
    if not root_rows:
        raise ValueError("Task not found")
    root = root_rows[0]
    if root["coalesced_id"] is not None:
        raise ValueError("Task is a subordinate, not a root")
    root_status = await get_task_status_from_events(root_id, db)
    if root_status != "pending":
        raise ValueError("Can only split pending tasks — split is a pending-column operation")

    sub_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE coalesced_id = ?", (root_id,)
    )
    if not sub_rows:
        raise ValueError("No subordinate tasks to split")

    sub_ids = [r["id"] for r in sub_rows]

    # Look up the job's current require_approval setting
    prop_rows = await db.execute_fetchall(
        "SELECT value FROM job_properties WHERE job_id = ? AND key = 'require_approval'",
        (root["job_id"],)
    )
    require_approval = prop_rows[0]["value"] == "true" if prop_rows else False
    approval_val = "pending" if require_approval else None

    from backend.state import utcnow
    now = utcnow()
    # Split-off tasks always land in pending (split is a pending-column operation)
    placeholders = ",".join("?" * len(sub_ids))
    await db.execute(
        f"""UPDATE tasks
            SET coalesced_id = NULL, sort_order = NULL, created_at = ?,
                status = 'pending', approval = ?, queued_at = NULL
            WHERE id IN ({placeholders})""",
        [now, approval_val] + sub_ids,
    )

    # Lifecycle event: freed tasks reset to pending
    await db.executemany(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, 'restored', ?)",
        [(sid, now) for sid in sub_ids]
    )

    await db.commit()
    return sub_ids


async def uncoalesce_task(task_id: int) -> int:
    """Remove a single subordinate from its coalesce group, making it independent."""
    db = await get_db()

    rows = await db.execute_fetchall(
        "SELECT id, job_id, coalesced_id FROM tasks WHERE id = ?",
        (task_id,)
    )
    if not rows:
        raise ValueError("Task not found")
    task = rows[0]
    if not task["coalesced_id"]:
        raise ValueError("Task is not a subordinate — use split on the root task")
    task_status = await get_task_status_from_events(task_id, db)
    if task_status not in ("pending", "queued"):
        raise ValueError("Can only uncoalesce pre-execution tasks")

    # Verify root is in pending (uncoalesce is a pending-column operation)
    root_status = await get_task_status_from_events(task["coalesced_id"], db)
    if root_status is not None and root_status != "pending":
        raise ValueError("Can only uncoalesce from a pending root — split is a pending-column operation")

    # Look up the job's current require_approval setting
    prop_rows = await db.execute_fetchall(
        "SELECT value FROM job_properties WHERE job_id = ? AND key = 'require_approval'",
        (task["job_id"],)
    )
    require_approval = prop_rows[0]["value"] == "true" if prop_rows else False
    approval_val = "pending" if require_approval else None

    from backend.state import utcnow
    now = utcnow()
    # Freed task always lands in pending (uncoalesce is a pending-column operation)
    await db.execute(
        "UPDATE tasks SET coalesced_id = NULL, sort_order = NULL, created_at = ?, status = 'pending', approval = ?, queued_at = NULL WHERE id = ?",
        (now, approval_val, task_id),
    )

    # Lifecycle event: freed task resets to pending
    await db.execute(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, 'restored', ?)",
        (task_id, now)
    )

    await db.commit()
    return task_id


async def sweep_stale_tasks(now: str):
    """Mark any in-flight tasks as interrupted (e.g. after restart).

    Uses transition_task for each stale task so the event log records
    the interruption — status is never updated without an event.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE status = 'active'"
    )
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    for task_id in ids:
        await transition_task(task_id, "interrupted", error="interrupted", completed_at=now)
    return ids


async def approve_task(task_id: int) -> bool:
    """Approve a pending-approval task (and its subordinates)."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE tasks SET approval = 'approved'"
        " WHERE (id = ? OR coalesced_id = ?) AND approval = 'pending'",
        (task_id, task_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def reject_task(task_id: int) -> bool:
    """Reject a pending-approval task (and its subordinates).

    Uses transition_task so the event log records each rejection.
    """
    db = await get_db()
    # Find all tasks in the coalesce group with pending approval
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE (id = ? OR coalesced_id = ?) AND approval = 'pending'",
        (task_id, task_id)
    )
    if not rows:
        return False
    for row in rows:
        tid = row["id"]
        await db.execute(
            "UPDATE tasks SET approval = 'rejected' WHERE id = ?", (tid,)
        )
        await transition_task(tid, "rejected", error="rejected")
    return True


async def find_session_by_cli_session(cli_session_id: str) -> str | None:
    """Find a chat session ID by its CLI session ID (for task resume)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM chat_sessions WHERE cli_session_id = ? LIMIT 1",
        (cli_session_id,)
    )
    return rows[0]["id"] if rows else None


# ── Chat ────────────────────────────────────────────────────

async def create_chat_session(job_id: int | None = None, title: str | None = None,
                               task_id: int | None = None) -> dict:
    session_id = str(uuid.uuid4())
    db = await get_db()
    await db.execute(
        "INSERT INTO chat_sessions (id, job_id, task_id, title) VALUES (?, ?, ?, ?)",
        (session_id, job_id, task_id, title)
    )
    await db.commit()
    return {"id": session_id, "job_id": job_id, "task_id": task_id, "title": title}


async def add_chat_message(session_id: str, role: str, content: str):
    db = await get_db()
    await db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, role, content)
    )
    await db.commit()


async def get_chat_session(session_id: str) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
    )
    return dict(rows[0]) if rows else None


async def get_chat_sessions() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM chat_sessions WHERE task_id IS NULL ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def get_chat_messages(session_id: str) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at",
        (session_id,)
    )
    return [dict(r) for r in rows]


async def reconstruct_output_from_events(session_id: str) -> list[dict]:
    """Reconstruct assistant messages from the raw chat_events log.

    chat_events is the durable, incrementally-persisted store — every raw
    NDJSON line is written as it arrives during streaming.  chat_messages
    is a one-shot materialization written after the CLI exits, which can
    be empty if the worker's post-processing is skipped or fails.

    This function parses 'assistant' type events to extract text blocks,
    providing a reliable fallback when chat_messages has no content.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT raw_json FROM chat_events "
        "WHERE session_id = ? AND event_type = 'assistant' ORDER BY id ASC",
        (session_id,),
    )
    text_parts = []
    for row in rows:
        try:
            data = json.loads(row["raw_json"])
            blocks = (
                data.get("message", {}).get("content", [])
                or data.get("content", [])
            )
            for block in blocks:
                if block.get("type") == "text" and block.get("text"):
                    text_parts.append(block["text"])
        except Exception:
            continue
    if not text_parts:
        return []
    return [{"role": "assistant", "content": "\n\n".join(text_parts)}]


async def update_chat_session(session_id: str, **kwargs):
    db = await get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    await db.execute(f"UPDATE chat_sessions SET {sets} WHERE id = ?", vals)
    await db.commit()


async def delete_chat_session(session_id: str):
    db = await get_db()
    await db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    await db.commit()


async def add_chat_event(session_id: str, event_type: str, raw_json: str):
    """Insert a single chat event — used for incremental persistence during streaming."""
    db = await get_db()
    await db.execute(
        "INSERT INTO chat_events (session_id, event_type, raw_json) VALUES (?, ?, ?)",
        (session_id, event_type, raw_json),
    )
    await db.commit()


async def add_chat_events_batch(session_id: str, events: list[tuple[str, str]]):
    """Insert multiple chat events in a single transaction."""
    if not events:
        return
    db = await get_db()
    await db.executemany(
        "INSERT INTO chat_events (session_id, event_type, raw_json) VALUES (?, ?, ?)",
        [(session_id, et, rj) for et, rj in events],
    )
    await db.commit()


# ── MCP Servers ────────────────────────────────────────────

async def list_mcp_servers() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM mcp_servers")
    return [dict(r) for r in rows]


async def create_mcp_server(name: str, command: str, args: list | None = None, env: dict | None = None):
    db = await get_db()
    await db.execute(
        "INSERT INTO mcp_servers (name, command, args, env) VALUES (?, ?, ?, ?)",
        (name, command, json.dumps(args or []), json.dumps(env or {}))
    )
    await db.commit()


async def update_mcp_server_enabled(name: str, enabled: bool):
    db = await get_db()
    await db.execute(
        "UPDATE mcp_servers SET enabled = ? WHERE name = ?",
        (1 if enabled else 0, name),
    )
    await db.commit()


async def update_mcp_server_fields(name: str, command: str | None, args: list | None, env: dict | None):
    db = await get_db()
    sets, vals = [], []
    if command is not None:
        sets.append("command = ?")
        vals.append(command)
    if args is not None:
        sets.append("args = ?")
        vals.append(json.dumps(args))
    if env is not None:
        sets.append("env = ?")
        vals.append(json.dumps(env))
    if sets:
        vals.append(name)
        await db.execute(f"UPDATE mcp_servers SET {', '.join(sets)} WHERE name = ?", vals)
        await db.commit()


async def delete_mcp_server(name: str):
    db = await get_db()
    await db.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
    await db.commit()


async def get_jobs_referencing_mcp_server(server_name: str) -> list[dict]:
    """Return jobs whose mcp_servers property contains the given server name."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT jp.job_id, jp.value, j.name FROM job_properties jp JOIN jobs j ON j.id = jp.job_id "
        "WHERE jp.key = 'mcp_servers'",
    )
    result = []
    for row in rows:
        try:
            servers = json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            continue
        if server_name in servers:
            result.append({"id": row["job_id"], "name": row["name"]})
    return result


async def delete_mcp_server_cascade(name: str):
    """Delete an MCP server and remove it from all jobs' mcp_servers properties."""
    conn = await get_db()
    # Find and update all jobs referencing this server
    rows = await conn.execute_fetchall(
        "SELECT job_id, value FROM job_properties WHERE key = 'mcp_servers'",
    )
    for row in rows:
        try:
            servers = json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            continue
        if name in servers:
            servers = [s for s in servers if s != name]
            await conn.execute(
                "UPDATE job_properties SET value = ? WHERE job_id = ? AND key = 'mcp_servers'",
                (json.dumps(servers), row["job_id"]),
            )
    await conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
    await conn.commit()


# ── Config ──────────────────────────────────────────────────

async def get_config(key: str | None = None) -> dict | str | None:
    db = await get_db()
    if key:
        rows = await db.execute_fetchall("SELECT value FROM config WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None
    rows = await db.execute_fetchall("SELECT key, value FROM config")
    return {r["key"]: r["value"] for r in rows}


async def set_config(key: str, value: str):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
    )
    await db.commit()


async def get_config_prefix(prefix: str) -> dict[str, str]:
    """Return all config entries whose key starts with prefix, as a dict."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT key, value FROM config WHERE key LIKE ?", (prefix + "%",)
    )
    return {r["key"]: r["value"] for r in rows}


# ── Dashboard Aggregation ──────────────────────────────────

async def dashboard_health(window_days: int) -> list[dict]:
    """Per-job task counts by terminal state within a time window.

    Returns current window stats and previous-window stats for trend calculation.
    """
    db = await get_db()
    sql = """
        SELECT
            t.job_id,
            j.name AS job_name,
            COUNT(*) AS total,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN t.status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out,
            SUM(CASE WHEN t.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
            SUM(CASE WHEN t.status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted,
            SUM(CASE WHEN t.status = 'rejected' THEN 1 ELSE 0 END) AS rejected
        FROM tasks t
        JOIN jobs j ON j.id = t.job_id
        WHERE t.status IN ('completed','failed','timed_out','cancelled','interrupted','rejected')
          AND t.completed_at >= datetime('now', ?)
        GROUP BY t.job_id, j.name
    """
    current = await db.execute_fetchall(sql, (f"-{window_days} days",))

    # Previous equivalent window for trend comparison
    prev_sql = """
        SELECT
            t.job_id,
            COUNT(*) AS total,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed
        FROM tasks t
        WHERE t.status IN ('completed','failed','timed_out','cancelled','interrupted','rejected')
          AND t.completed_at >= datetime('now', ?)
          AND t.completed_at < datetime('now', ?)
        GROUP BY t.job_id
    """
    prev = await db.execute_fetchall(
        prev_sql, (f"-{window_days * 2} days", f"-{window_days} days")
    )
    prev_by_job = {r["job_id"]: dict(r) for r in prev}

    results = []
    for r in current:
        row = dict(r)
        p = prev_by_job.get(row["job_id"])
        if p and p["total"] > 0:
            row["prev_success_rate"] = p["completed"] / p["total"]
        else:
            row["prev_success_rate"] = None
        results.append(row)
    return results


async def dashboard_timeline(window_days: int) -> list[dict]:
    """Tasks with start/end times for timeline visualization.

    Derives start and end timestamps from task_events (active/terminal pairs)
    rather than the transitional timestamp columns on the task record.
    """
    db = await get_db()
    # Join task_events to get the 'active' event as start time and the latest
    # terminal event as end time per task.
    rows = await db.execute_fetchall(
        """SELECT t.id, t.job_id, j.name AS job_name,
                  te_start.created_at AS started_at,
                  te_end.created_at AS completed_at,
                  t.error, t.status
           FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           JOIN task_events te_start ON te_start.task_id = t.id AND te_start.event IN ('activated', 'active')
           LEFT JOIN task_events te_end ON te_end.task_id = t.id
             AND te_end.event IN ('completed', 'failed', 'cancelled', 'timed_out', 'interrupted')
             AND te_end.id = (
               SELECT MAX(e2.id) FROM task_events e2
               WHERE e2.task_id = t.id
                 AND e2.event IN ('completed', 'failed', 'cancelled', 'timed_out', 'interrupted')
             )
           WHERE te_start.created_at >= datetime('now', ?)
           ORDER BY te_start.created_at""",
        (f"-{window_days} days",)
    )
    return [dict(r) for r in rows]


async def dashboard_chains(window_days: int) -> list[dict]:
    """Agent-triggered tasks for dispatch chain visualization."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.id, t.job_id, j.name AS job_name,
                  t.trigger_detail, t.error, t.completed_at
           FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.trigger = 'agent'
             AND t.completed_at IS NOT NULL
             AND t.completed_at >= datetime('now', ?)""",
        (f"-{window_days} days",)
    )
    return [dict(r) for r in rows]


async def dashboard_tool_usage(window_days: int) -> list[dict]:
    """Per-job tool frequency and error rates from MCP tool use events."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.job_id, j.name AS job_name, ce.raw_json
           FROM chat_events ce
           JOIN chat_sessions cs ON cs.id = ce.session_id
           JOIN tasks t ON t.id = cs.task_id
           JOIN jobs j ON j.id = t.job_id
           WHERE ce.event_type = 'mcp_tool_use'
             AND ce.created_at >= datetime('now', ?)""",
        (f"-{window_days} days",)
    )

    # Aggregate: per-job tool counts and error counts
    from collections import defaultdict
    job_tools: dict[int, dict] = {}  # job_id -> {job_name, tools: {tool -> {count, errors}}}
    for r in rows:
        jid = r["job_id"]
        if jid not in job_tools:
            job_tools[jid] = {"job_id": jid, "job_name": r["job_name"], "tools": defaultdict(lambda: {"count": 0, "errors": 0})}
        try:
            data = json.loads(r["raw_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        tool_name = data.get("tool", "unknown")
        job_tools[jid]["tools"][tool_name]["count"] += 1
        result = data.get("result")
        if isinstance(result, str) and ("error" in result.lower() or "Error" in result):
            job_tools[jid]["tools"][tool_name]["errors"] += 1
        elif isinstance(result, dict) and result.get("isError"):
            job_tools[jid]["tools"][tool_name]["errors"] += 1

    # Convert defaultdicts to plain dicts for JSON serialization
    return [
        {
            "job_id": v["job_id"],
            "job_name": v["job_name"],
            "tools": {k: dict(c) for k, c in v["tools"].items()},
        }
        for v in job_tools.values()
    ]


# ── Helpers ─────────────────────────────────────────────────

def _cast_property(value: str, type_: str):
    if type_ == "json":
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if type_ == "integer":
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    if type_ == "boolean":
        return value.lower() in ("true", "1", "yes")
    return value


_INT_LIST_PROPS = {"cascades_from", "allowed_dispatch_targets"}

def _normalize_int_list_props(props: dict, slug_to_id: dict[str, int]) -> None:
    """Coerce any slug strings in integer-list properties to int IDs in-place."""
    for key in _INT_LIST_PROPS:
        val = props.get(key)
        if not isinstance(val, list):
            continue
        normalized = []
        for item in val:
            if isinstance(item, int):
                normalized.append(item)
            else:
                try:
                    normalized.append(int(item))
                except (ValueError, TypeError):
                    resolved = slug_to_id.get(str(item))
                    if resolved is not None:
                        normalized.append(resolved)
        props[key] = normalized
