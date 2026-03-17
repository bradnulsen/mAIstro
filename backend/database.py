"""SQLite database layer for mAistro — schema, init, CRUD helpers."""

import aiosqlite
import json
import os
import re
import uuid

DB_PATH: str | None = None
_conn: aiosqlite.Connection | None = None
_property_defs_cache: list | None = None


def get_db_path(project_dir: str) -> str:
    maistro_dir = os.path.join(project_dir, ".maistro")
    os.makedirs(maistro_dir, exist_ok=True)
    return os.path.join(maistro_dir, "maistro.db")


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
    return _conn


async def close_db():
    """Close the persistent connection and reset state. Call from lifespan teardown."""
    global _conn, DB_PATH, _property_defs_cache
    if _conn is not None:
        await _conn.close()
        _conn = None
    DB_PATH = None
    _property_defs_cache = None


async def init_db(project_dir: str):
    global DB_PATH
    # Close previous connection if switching projects
    await close_db()
    DB_PATH = get_db_path(project_dir)
    db = await get_db()
    await db.executescript(SCHEMA_SQL)
    await db.executescript(SEED_SQL)
    await db.commit()
    await _migrate(db)


async def _migrate(db: aiosqlite.Connection):
    """Run column-level migrations for existing databases."""
    # Add queued_at column (two-stage queue: pending → queued → active)
    try:
        await db.execute("ALTER TABLE tasks ADD COLUMN queued_at DATETIME")
        # Backfill: if auto_dispatch was enabled, existing pending tasks were
        # effectively queued — set queued_at so the worker can still reach them.
        rows = await db.execute_fetchall(
            "SELECT value FROM config WHERE key = 'queue_auto_dispatch'"
        )
        if rows and rows[0]["value"] == "true":
            await db.execute(
                "UPDATE tasks SET queued_at = created_at "
                "WHERE started_at IS NULL AND error IS NULL"
            )
        await db.commit()
    except Exception:
        pass  # Column already exists

    # Index for queued task lookup — safe to run unconditionally after column exists
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_queued "
        "ON tasks (queued_at, started_at, error, coalesced_id)"
    )
    await db.commit()

    # Drop rating column (removed from architecture — no longer tracked)
    try:
        await db.execute("ALTER TABLE tasks DROP COLUMN rating")
        await db.commit()
    except Exception:
        pass  # Column already removed or never existed


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_property_defs (
    key TEXT PRIMARY KEY,
    default_value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string'
);

CREATE TABLE IF NOT EXISTS job_properties (
    job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT REFERENCES job_property_defs(key),
    value TEXT NOT NULL,
    PRIMARY KEY (job_id, key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    trigger TEXT NOT NULL,
    trigger_detail TEXT,
    context TEXT,
    session_id TEXT,
    resume_session_id TEXT,
    approval TEXT,
    start_commit TEXT,
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
    job_id TEXT REFERENCES jobs(id),
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

-- Indices for hot query paths
CREATE INDEX IF NOT EXISTS idx_tasks_pending
    ON tasks (started_at, error, sort_order, created_at);

CREATE INDEX IF NOT EXISTS idx_tasks_job_coalesce
    ON tasks (job_id, started_at, error);

CREATE INDEX IF NOT EXISTS idx_tasks_running
    ON tasks (started_at, completed_at);

CREATE INDEX IF NOT EXISTS idx_tasks_coalesced
    ON tasks (coalesced_id);

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

CREATE INDEX IF NOT EXISTS idx_tasks_job_running
    ON tasks (job_id, started_at, completed_at);

CREATE INDEX IF NOT EXISTS idx_tasks_coalesce_lookup
    ON tasks (job_id, started_at, error, coalesced_id);

"""

SEED_SQL = """
INSERT OR IGNORE INTO job_property_defs (key, default_value, type) VALUES
    ('description', '', 'string'),
    ('instructions', '', 'string'),
    ('model', 'sonnet', 'string'),
    ('allowed_tools', '[]', 'json'),
    ('mcp_servers', '[]', 'json'),
    ('subscriptions', '[]', 'json'),
    ('coalesce_tasks', 'false', 'boolean'),
    ('sort_order', '0', 'integer'),
    ('schedule', '', 'string'),
    ('timeout', '900', 'integer'),
    ('depends_on', '[]', 'json'),
    ('require_approval', 'false', 'boolean');

INSERT OR IGNORE INTO config (key, value) VALUES ('queue_auto_dispatch', 'false');
"""


def slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug


# ── Job CRUD ──────────────────────────────────────────────

async def create_job(name: str, properties: dict | None = None) -> dict:
    job_id = slugify(name)
    db = await get_db()
    await db.execute("INSERT INTO jobs (id, name) VALUES (?, ?)", (job_id, name))
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


async def get_job(job_id: str, running_ids: set | None = None) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id, name, created_at FROM jobs WHERE id = ?", (job_id,)
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

    # Derive running status from tasks table
    if running_ids is not None:
        props["running"] = job_id in running_ids
    else:
        r = await db.execute_fetchall(
            "SELECT 1 FROM tasks WHERE job_id = ? AND started_at IS NOT NULL AND completed_at IS NULL LIMIT 1",
            (job_id,)
        )
        props["running"] = bool(r)

    job["properties"] = props
    return job


async def list_jobs() -> list[dict]:
    """Return all jobs. Uses 3 batch queries instead of 2N+2 (N+1 avoided)."""
    conn = await get_db()
    job_rows = await conn.execute_fetchall("SELECT id, name, created_at FROM jobs")
    if not job_rows:
        return []

    running_rows = await conn.execute_fetchall(
        "SELECT DISTINCT job_id FROM tasks WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    running_ids = {r["job_id"] for r in running_rows}

    prop_rows = await conn.execute_fetchall("SELECT job_id, key, value FROM job_properties")
    props_by_job: dict[str, dict] = {}
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
    jobs.sort(key=lambda j: j["properties"].get("sort_order", 0))
    return jobs


async def update_job(job_id: str, updates: dict) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None

    if "name" in updates:
        await db.execute("UPDATE jobs SET name = ? WHERE id = ?", (updates.pop("name"), job_id))

    for key, value in updates.items():
        val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        await db.execute(
            "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
            (job_id, key, val)
        )
    await db.commit()
    return await get_job(job_id)


async def reorder_jobs(job_ids: list[str]):
    """Set sort_order for multiple jobs in a single transaction."""
    db = await get_db()
    await db.executemany(
        "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, 'sort_order', ?)",
        [(job_id, str(i)) for i, job_id in enumerate(job_ids)],
    )
    await db.commit()


async def delete_job(job_id: str) -> bool:
    db = await get_db()
    await db.execute("DELETE FROM chat_sessions WHERE job_id = ?", (job_id,))
    await db.execute("DELETE FROM tasks WHERE job_id = ?", (job_id,))
    cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cursor.rowcount > 0


async def get_jobs_depending_on(job_id: str) -> list[dict]:
    """Return jobs whose depends_on property includes job_id."""
    conn = await get_db()
    rows = await conn.execute_fetchall(
        'SELECT job_id FROM job_properties WHERE key = "depends_on" AND value LIKE ?',
        (f'%"{job_id}"%',)
    )
    if not rows:
        return []

    candidate_ids = [r["job_id"] for r in rows]
    placeholders = ",".join("?" * len(candidate_ids))

    job_rows = await conn.execute_fetchall(
        f"SELECT id, name, created_at FROM jobs WHERE id IN ({placeholders})",
        candidate_ids,
    )
    if not job_rows:
        return []

    running_rows = await conn.execute_fetchall(
        "SELECT DISTINCT job_id FROM tasks WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    running_ids = {r["job_id"] for r in running_rows}

    prop_rows = await conn.execute_fetchall(
        f"SELECT job_id, key, value FROM job_properties WHERE job_id IN ({placeholders})",
        candidate_ids,
    )
    props_by_job: dict[str, dict] = {}
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
        if job_id in (props.get("depends_on") or []):
            jobs.append(job)
    return jobs


# ── Tasks (atomic work items) ─────────────────────────────

async def enqueue_task(job_id: str, trigger: str,
                       trigger_detail: str | None = None,
                       context: str | None = None) -> int:
    """Enqueue an atomic task. Coalescing links via coalesced_id instead of mutating data.

    Coalescing rules:
    - 'schedule' always coalesces globally
    - 'commit' and 'dependency' coalesce with other pending tasks of the same type
    - coalesce_tasks=true coalesces globally
    - All other triggers (manual, resume, retry) never coalesce
    """
    db = await get_db()

    # Fetch only the two properties needed
    prop_rows = await db.execute_fetchall(
        "SELECT key, value FROM job_properties WHERE job_id = ? AND key IN ('coalesce_tasks', 'require_approval')",
        (job_id,)
    )
    job_props = {r["key"]: r["value"] for r in prop_rows}
    coalesce_global = (trigger == "schedule") or (job_props.get("coalesce_tasks", "").lower() == "true")
    coalesce_same_type = trigger in ("commit", "dependency")

    # Approval gate: manual tasks bypass, others check job property
    approval = None
    if trigger != "manual" and job_props.get("require_approval", "").lower() == "true":
        approval = "pending"

    # Auto-queueing: when enabled, new tasks skip pending and go directly to queued
    from backend.state import utcnow
    queued_at = None
    auto_queue = await get_config("queue_auto_dispatch")
    if auto_queue == "true":
        queued_at = utcnow()

    # Insert the atomic task (not committed yet — coalesce update shares the transaction)
    cursor = await db.execute(
        """INSERT INTO tasks (job_id, trigger, trigger_detail, context, approval, queued_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (job_id, trigger, trigger_detail, context, approval, queued_at)
    )
    new_id = cursor.lastrowid

    # Coalesce: find an existing pending root and link this task to it
    root_id = None
    if coalesce_global or coalesce_same_type:
        query = """SELECT id FROM tasks
                   WHERE job_id = ? AND started_at IS NULL AND error IS NULL
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
    return dict(rows[0]) if rows else None


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
           WHERE t.queued_at IS NOT NULL AND t.started_at IS NULL AND t.error IS NULL
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


async def transfer_task(task_id: int, to_queued: bool):
    """Move a task between pending and queued columns. Sets or clears queued_at.
    Also transfers subordinate tasks to maintain group cohesion."""
    from backend.state import utcnow
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, started_at, error, coalesced_id FROM tasks WHERE id = ?",
        (task_id,)
    )
    if not rows:
        raise ValueError("Task not found")
    task = rows[0]
    if task["started_at"] is not None or task["error"] is not None:
        raise ValueError("Can only transfer pre-execution tasks")
    if task["coalesced_id"] is not None:
        raise ValueError("Cannot transfer a subordinate — transfer its root instead")

    queued_at = utcnow() if to_queued else None
    # Transfer root and all subordinates; clear sort_order (append to end of target column)
    await db.execute(
        "UPDATE tasks SET queued_at = ?, sort_order = NULL WHERE id = ?",
        (queued_at, task_id)
    )
    await db.execute(
        "UPDATE tasks SET queued_at = ? WHERE coalesced_id = ? AND started_at IS NULL AND error IS NULL",
        (queued_at, task_id)
    )
    await db.commit()
    return task_id


async def reorder_tasks(task_ids: list[int]):
    """Set explicit sort_order on pending tasks to control execution priority."""
    db = await get_db()
    await db.executemany(
        "UPDATE tasks SET sort_order = ? WHERE id = ? AND started_at IS NULL AND error IS NULL",
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
        f"""SELECT id, job_id, started_at, error, approval, coalesced_id, created_at, queued_at
            FROM tasks WHERE id IN ({placeholders})
            ORDER BY created_at ASC""",
        task_ids,
    )

    if len(rows) != len(task_ids):
        raise ValueError("Some task IDs not found")

    job_ids = set()
    for r in rows:
        if r["queued_at"] is not None:
            raise ValueError(f"Task #{r['id']} is queued — merge is a pending-column operation")
        if r["started_at"] is not None:
            raise ValueError(f"Task #{r['id']} has already started")
        if r["error"] is not None:
            raise ValueError(f"Task #{r['id']} has an error")
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
    await db.commit()
    return root_id


async def split_task(root_id: int) -> list[int]:
    """Split a root task — make all subordinates independent again."""
    db = await get_db()

    root_rows = await db.execute_fetchall(
        "SELECT id, job_id, started_at, error, queued_at, coalesced_id FROM tasks WHERE id = ?",
        (root_id,)
    )
    if not root_rows:
        raise ValueError("Task not found")
    root = root_rows[0]
    if root["coalesced_id"] is not None:
        raise ValueError("Task is a subordinate, not a root")
    if root["queued_at"] is not None:
        raise ValueError("Can only split pending tasks — split is a pending-column operation")
    if root["started_at"] is not None or root["error"] is not None:
        raise ValueError("Can only split pre-execution tasks")

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
                approval = ?, queued_at = NULL
            WHERE id IN ({placeholders})""",
        [now, approval_val] + sub_ids,
    )
    await db.commit()
    return sub_ids


async def uncoalesce_task(task_id: int) -> int:
    """Remove a single subordinate from its coalesce group, making it independent."""
    db = await get_db()

    rows = await db.execute_fetchall(
        "SELECT id, job_id, coalesced_id, started_at, error FROM tasks WHERE id = ?",
        (task_id,)
    )
    if not rows:
        raise ValueError("Task not found")
    task = rows[0]
    if not task["coalesced_id"]:
        raise ValueError("Task is not a subordinate — use split on the root task")
    if task["started_at"] is not None or task["error"] is not None:
        raise ValueError("Can only uncoalesce pre-execution tasks")

    # Verify root is in pending (uncoalesce is a pending-column operation)
    root_rows = await db.execute_fetchall(
        "SELECT queued_at FROM tasks WHERE id = ?", (task["coalesced_id"],)
    )
    if root_rows and root_rows[0]["queued_at"] is not None:
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
        "UPDATE tasks SET coalesced_id = NULL, sort_order = NULL, created_at = ?, approval = ?, queued_at = NULL WHERE id = ?",
        (now, approval_val, task_id),
    )
    await db.commit()
    return task_id


async def sweep_stale_tasks(now: str):
    """Mark any in-flight tasks as interrupted (e.g. after restart)."""
    db = await get_db()
    # Read stale IDs first, then update — avoids two full scans with identical predicates.
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    if not rows:
        return []
    await db.execute(
        "UPDATE tasks SET completed_at = ?, error = 'interrupted'"
        " WHERE started_at IS NOT NULL AND completed_at IS NULL",
        (now,)
    )
    await db.commit()
    return [row["id"] for row in rows]


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
    """Reject a pending-approval task (and its subordinates)."""
    db = await get_db()
    from backend.state import utcnow
    now = utcnow()
    cursor = await db.execute(
        "UPDATE tasks SET approval = 'rejected', completed_at = ?, error = 'rejected'"
        " WHERE (id = ? OR coalesced_id = ?) AND approval = 'pending'",
        (now, task_id, task_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def find_session_by_cli_session(cli_session_id: str) -> str | None:
    """Find a chat session ID by its CLI session ID (for task resume)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM chat_sessions WHERE cli_session_id = ? LIMIT 1",
        (cli_session_id,)
    )
    return rows[0]["id"] if rows else None


# ── Chat ────────────────────────────────────────────────────

async def create_chat_session(job_id: str | None = None, title: str | None = None,
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
    db = await get_db()
    await db.execute(
        "INSERT INTO chat_events (session_id, event_type, raw_json) VALUES (?, ?, ?)",
        (session_id, event_type, raw_json)
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


async def delete_mcp_server(name: str):
    db = await get_db()
    await db.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
    await db.commit()


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
