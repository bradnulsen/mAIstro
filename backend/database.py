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
    # Lightweight migrations for columns added after initial schema
    await _migrate(db)
    await db.commit()


async def _migrate(db):
    """Add columns that CREATE TABLE IF NOT EXISTS won't retroactively add."""
    cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(dispatch_queue)")}
    if "sort_order" not in cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN sort_order INTEGER")
    if "rating" not in cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN rating TEXT")
    if "coalesced_id" not in cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN coalesced_id INTEGER REFERENCES dispatch_queue(id)")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_property_defs (
    key TEXT PRIMARY KEY,
    default_value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string'
);

CREATE TABLE IF NOT EXISTS task_properties (
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    key TEXT REFERENCES task_property_defs(key),
    value TEXT NOT NULL,
    PRIMARY KEY (task_id, key)
);

CREATE TABLE IF NOT EXISTS dispatch_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    trigger TEXT NOT NULL,
    trigger_detail TEXT,
    triggers TEXT,
    session_id TEXT,
    resume_session_id TEXT,
    approval TEXT,
    start_commit TEXT,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    result_commit TEXT,
    error TEXT,
    sort_order INTEGER,
    rating TEXT,
    coalesced_id INTEGER REFERENCES dispatch_queue(id)
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    dispatch_id INTEGER REFERENCES dispatch_queue(id),
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
CREATE INDEX IF NOT EXISTS idx_dispatch_queue_pending
    ON dispatch_queue (started_at, error, sort_order, created_at);

CREATE INDEX IF NOT EXISTS idx_dispatch_queue_task_coalesce
    ON dispatch_queue (task_id, started_at, error);

CREATE INDEX IF NOT EXISTS idx_dispatch_queue_running
    ON dispatch_queue (started_at, completed_at);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages (session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_chat_events_session
    ON chat_events (session_id);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_cli_session
    ON chat_sessions (cli_session_id);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_dispatch
    ON chat_sessions (dispatch_id);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_task_id
    ON chat_sessions (task_id);

CREATE INDEX IF NOT EXISTS idx_task_properties_key
    ON task_properties (key);

CREATE INDEX IF NOT EXISTS idx_dispatch_queue_coalesced
    ON dispatch_queue (coalesced_id);
"""

SEED_SQL = """
INSERT OR IGNORE INTO task_property_defs (key, default_value, type) VALUES
    ('description', '', 'string'),
    ('instructions', '', 'string'),
    ('model', 'sonnet', 'string'),
    ('allowed_tools', '[]', 'json'),
    ('mcp_servers', '[]', 'json'),
    ('subscriptions', '[]', 'json'),
    ('coalesce_dispatches', 'false', 'boolean'),
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


# ── Task CRUD ──────────────────────────────────────────────

async def create_task(name: str, properties: dict | None = None) -> dict:
    task_id = slugify(name)
    db = await get_db()
    await db.execute("INSERT INTO tasks (id, name) VALUES (?, ?)", (task_id, name))
    if properties:
        for key, value in properties.items():
            val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            await db.execute(
                "INSERT OR REPLACE INTO task_properties (task_id, key, value) VALUES (?, ?, ?)",
                (task_id, key, val)
            )
    await db.commit()
    return await get_task(task_id)


async def _get_property_defs() -> list:
    """Return task_property_defs rows, using a module-level cache.

    Property defs are seeded once and never mutated at runtime, so caching
    them avoids one redundant SELECT per get_task() call — most visible in
    list_tasks() which calls get_task() N times.
    """
    global _property_defs_cache
    if _property_defs_cache is None:
        conn = await get_db()
        rows = await conn.execute_fetchall("SELECT key, default_value, type FROM task_property_defs")
        _property_defs_cache = [dict(r) for r in rows]
    return _property_defs_cache


async def get_task(task_id: str, running_ids: set | None = None) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id, name, created_at FROM tasks WHERE id = ?", (task_id,)
    )
    if not row:
        return None
    task = dict(row[0])

    # Get all property defs with defaults, override with task-specific values
    defs = await _get_property_defs()
    props = {}
    for d in defs:
        props[d["key"]] = _cast_property(d["default_value"], d["type"])

    task_props = await db.execute_fetchall(
        "SELECT key, value FROM task_properties WHERE task_id = ?", (task_id,)
    )
    def_types = {d["key"]: d["type"] for d in defs}
    for p in task_props:
        props[p["key"]] = _cast_property(p["value"], def_types.get(p["key"], "string"))

    # Derive running status from dispatch_queue (not stored as task property)
    if running_ids is not None:
        props["running"] = task_id in running_ids
    else:
        r = await db.execute_fetchall(
            "SELECT 1 FROM dispatch_queue WHERE task_id = ? AND started_at IS NOT NULL AND completed_at IS NULL LIMIT 1",
            (task_id,)
        )
        props["running"] = bool(r)

    task["properties"] = props
    return task


async def list_tasks() -> list[dict]:
    """Return all tasks. Uses 3 batch queries instead of 2N+2 (N+1 avoided)."""
    conn = await get_db()
    task_rows = await conn.execute_fetchall("SELECT id, name, created_at FROM tasks")
    if not task_rows:
        return []

    running_rows = await conn.execute_fetchall(
        "SELECT DISTINCT task_id FROM dispatch_queue WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    running_ids = {r["task_id"] for r in running_rows}

    prop_rows = await conn.execute_fetchall("SELECT task_id, key, value FROM task_properties")
    props_by_task: dict[str, dict] = {}
    for p in prop_rows:
        props_by_task.setdefault(p["task_id"], {})[p["key"]] = p["value"]

    defs = await _get_property_defs()
    def_defaults = {d["key"]: d["default_value"] for d in defs}
    def_types = {d["key"]: d["type"] for d in defs}

    # Pre-compute defaults once — avoids N × len(defs) _cast_property calls
    default_props = {k: _cast_property(v, def_types[k]) for k, v in def_defaults.items()}

    tasks = []
    for row in task_rows:
        task = dict(row)
        props = default_props.copy()
        for key, value in props_by_task.get(task["id"], {}).items():
            props[key] = _cast_property(value, def_types.get(key, "string"))
        props["running"] = task["id"] in running_ids
        task["properties"] = props
        tasks.append(task)
    tasks.sort(key=lambda t: t["properties"].get("sort_order", 0))
    return tasks


async def update_task(task_id: str, updates: dict) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
    if not row:
        return None

    if "name" in updates:
        await db.execute("UPDATE tasks SET name = ? WHERE id = ?", (updates.pop("name"), task_id))

    for key, value in updates.items():
        val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        await db.execute(
            "INSERT OR REPLACE INTO task_properties (task_id, key, value) VALUES (?, ?, ?)",
            (task_id, key, val)
        )
    await db.commit()
    return await get_task(task_id)


async def reorder_tasks(task_ids: list[str]):
    """Set sort_order for multiple tasks in a single transaction."""
    db = await get_db()
    for i, task_id in enumerate(task_ids):
        await db.execute(
            "INSERT OR REPLACE INTO task_properties (task_id, key, value) VALUES (?, 'sort_order', ?)",
            (task_id, str(i))
        )
    await db.commit()


async def delete_task(task_id: str) -> bool:
    db = await get_db()
    # Clean up referencing rows not covered by ON DELETE CASCADE
    await db.execute("DELETE FROM chat_sessions WHERE task_id = ?", (task_id,))
    await db.execute("DELETE FROM dispatch_queue WHERE task_id = ?", (task_id,))
    cursor = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await db.commit()
    return cursor.rowcount > 0


async def get_tasks_depending_on(task_id: str) -> list[dict]:
    """Return tasks whose depends_on property includes task_id.

    Uses a LIKE pre-filter on the JSON text (indexed on key) to find candidates,
    then batch-loads them in 3 queries total instead of 2N+1.
    JSON array serialization guarantees task IDs appear as quoted strings, so
    the pattern ``%"<id>"%`` won't match partial IDs.
    """
    conn = await get_db()
    rows = await conn.execute_fetchall(
        'SELECT task_id FROM task_properties WHERE key = "depends_on" AND value LIKE ?',
        (f'%"{task_id}"%',)
    )
    if not rows:
        return []

    candidate_ids = [r["task_id"] for r in rows]
    placeholders = ",".join("?" * len(candidate_ids))

    task_rows = await conn.execute_fetchall(
        f"SELECT id, name, created_at FROM tasks WHERE id IN ({placeholders})",
        candidate_ids,
    )
    if not task_rows:
        return []

    running_rows = await conn.execute_fetchall(
        "SELECT DISTINCT task_id FROM dispatch_queue WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    running_ids = {r["task_id"] for r in running_rows}

    prop_rows = await conn.execute_fetchall(
        f"SELECT task_id, key, value FROM task_properties WHERE task_id IN ({placeholders})",
        candidate_ids,
    )
    props_by_task: dict[str, dict] = {}
    for p in prop_rows:
        props_by_task.setdefault(p["task_id"], {})[p["key"]] = p["value"]

    defs = await _get_property_defs()
    def_defaults = {d["key"]: d["default_value"] for d in defs}
    def_types = {d["key"]: d["type"] for d in defs}
    default_props = {k: _cast_property(v, def_types[k]) for k, v in def_defaults.items()}

    tasks = []
    for row in task_rows:
        task = dict(row)
        props = default_props.copy()
        for key, value in props_by_task.get(task["id"], {}).items():
            props[key] = _cast_property(value, def_types.get(key, "string"))
        props["running"] = task["id"] in running_ids
        task["properties"] = props
        # Verify the parsed array actually contains task_id (guards against
        # substring false-positives in the LIKE pre-filter)
        if task_id in (props.get("depends_on") or []):
            tasks.append(task)
    return tasks


# ── Dispatch Queue ──────────────────────────────────────────

async def enqueue_dispatch(task_id: str, trigger: str,
                           trigger_detail: str | None = None,
                           context: str | None = None) -> int:
    """Enqueue a dispatch, coalescing into an existing pending record if applicable.

    Coalescing rules:
    - 'schedule' always coalesces globally — repeated fires are identical signals
    - 'commit' and 'dependency' coalesce with other pending dispatches of the same type —
      multiple commits while a task is busy = one catch-up run; multiple upstream completions
      while a dependent is pending = one run covering all of them
    - coalesce_dispatches=true coalesces globally — never more than one pending dispatch
      regardless of trigger type (useful for tasks that just need "run when things change")
    - All other triggers (manual, resume, retry) never coalesce — each
      represents a distinct explicit intent
    """
    new_entry = {"trigger": trigger, "detail": trigger_detail, "context": context}
    db = await get_db()

    # Fetch only the two properties needed — avoids a full get_task() load (all props + running check)
    prop_rows = await db.execute_fetchall(
        "SELECT key, value FROM task_properties WHERE task_id = ? AND key IN ('coalesce_dispatches', 'require_approval')",
        (task_id,)
    )
    task_props = {r["key"]: r["value"] for r in prop_rows}
    coalesce_global = (trigger == "schedule") or (task_props.get("coalesce_dispatches") == "true")
    coalesce_same_type = trigger in ("commit", "dependency")

    if coalesce_global or coalesce_same_type:
        query = """SELECT id, triggers
                   FROM dispatch_queue
                   WHERE task_id = ? AND started_at IS NULL AND error IS NULL"""
        params: list = [task_id]
        if coalesce_same_type and not coalesce_global:
            query += " AND trigger = ?"
            params.append(trigger)
        query += " ORDER BY created_at ASC LIMIT 1"
        rows = await db.execute_fetchall(query, params)
        if rows:
            existing = dict(rows[0])
            triggers = json.loads(existing["triggers"])
            triggers.append(new_entry)
            await db.execute(
                "UPDATE dispatch_queue SET triggers = ? WHERE id = ?",
                (json.dumps(triggers), existing["id"])
            )
            await db.commit()
            return existing["id"]

    # No coalescing — insert new record
    triggers_json = json.dumps([new_entry])

    # Approval gate: manual dispatches bypass (explicit intent), others check task property
    approval = None
    if trigger != "manual" and task_props.get("require_approval") == "true":
        approval = "pending"

    cursor = await db.execute(
        """INSERT INTO dispatch_queue (task_id, trigger, trigger_detail, triggers, approval)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, trigger, trigger_detail, triggers_json, approval)
    )
    await db.commit()
    return cursor.lastrowid


def _parse_dispatch_row(row) -> dict:
    """Convert a dispatch row, parsing the triggers JSON column."""
    d = dict(row)
    raw = d.get("triggers")
    d["triggers"] = json.loads(raw) if raw else []
    return d


async def get_dispatch(dispatch_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT dq.*, t.name as task_name FROM dispatch_queue dq JOIN tasks t ON t.id = dq.task_id WHERE dq.id = ?",
        (dispatch_id,)
    )
    return _parse_dispatch_row(rows[0]) if rows else None


async def get_dispatch_queue(limit: int = 50) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT dq.*, t.name as task_name,
                  (SELECT COUNT(*) FROM dispatch_queue sub WHERE sub.coalesced_id = dq.id) as subordinate_count
           FROM dispatch_queue dq
           JOIN tasks t ON t.id = dq.task_id
           WHERE dq.coalesced_id IS NULL
           ORDER BY dq.created_at DESC LIMIT ?""",
        (limit,)
    )
    return [_parse_dispatch_row(r) for r in rows]


async def get_oldest_pending_dispatch() -> dict | None:
    """Get the highest-priority pending dispatch (skips approval-pending and subordinates).

    Priority: explicit sort_order first (NULL last), then created_at ASC.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT dq.*, t.name as task_name FROM dispatch_queue dq
           JOIN tasks t ON t.id = dq.task_id
           WHERE dq.started_at IS NULL AND dq.error IS NULL
             AND (dq.approval IS NULL OR dq.approval = 'approved')
             AND dq.coalesced_id IS NULL
           ORDER BY dq.sort_order IS NULL, dq.sort_order ASC, dq.created_at ASC
           LIMIT 1"""
    )
    return _parse_dispatch_row(rows[0]) if rows else None


async def update_dispatch(dispatch_id: int, **kwargs):
    db = await get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [dispatch_id]
    await db.execute(f"UPDATE dispatch_queue SET {sets} WHERE id = ?", vals)
    await db.commit()


async def reorder_dispatches(dispatch_ids: list[int]):
    """Set explicit sort_order on pending dispatches to control execution priority."""
    db = await get_db()
    for i, did in enumerate(dispatch_ids):
        await db.execute(
            "UPDATE dispatch_queue SET sort_order = ? WHERE id = ? AND started_at IS NULL AND error IS NULL",
            (i, did),
        )
    await db.commit()


async def get_subordinate_dispatches(root_id: int) -> list[dict]:
    """Return all dispatches with coalesced_id pointing to root_id."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT dq.*, t.name as task_name FROM dispatch_queue dq
           JOIN tasks t ON t.id = dq.task_id
           WHERE dq.coalesced_id = ?""",
        (root_id,)
    )
    return [_parse_dispatch_row(r) for r in rows]


async def merge_dispatches(dispatch_ids: list[int]) -> int:
    """Merge pending same-job dispatches. Returns root dispatch ID.

    The oldest task becomes the root; others become subordinates via coalesced_id.
    """
    if len(dispatch_ids) < 2:
        raise ValueError("Need at least 2 dispatches to merge")

    db = await get_db()
    placeholders = ",".join("?" * len(dispatch_ids))
    rows = await db.execute_fetchall(
        f"""SELECT id, task_id, started_at, error, approval, coalesced_id, created_at
            FROM dispatch_queue WHERE id IN ({placeholders})
            ORDER BY created_at ASC""",
        dispatch_ids,
    )

    if len(rows) != len(dispatch_ids):
        raise ValueError("Some dispatch IDs not found")

    # Validate: all pending, same job, no subordinates
    task_ids = set()
    for r in rows:
        if r["started_at"] is not None:
            raise ValueError(f"Dispatch #{r['id']} has already started")
        if r["error"] is not None:
            raise ValueError(f"Dispatch #{r['id']} has an error")
        if r["approval"] == "pending":
            raise ValueError(f"Dispatch #{r['id']} is pending approval")
        if r["coalesced_id"] is not None:
            raise ValueError(f"Dispatch #{r['id']} is already a subordinate")
        task_ids.add(r["task_id"])

    if len(task_ids) > 1:
        raise ValueError("Cannot merge dispatches from different jobs")

    root_id = rows[0]["id"]
    sub_ids = [r["id"] for r in rows[1:]]
    sub_placeholders = ",".join("?" * len(sub_ids))
    await db.execute(
        f"UPDATE dispatch_queue SET coalesced_id = ? WHERE id IN ({sub_placeholders})",
        [root_id] + sub_ids,
    )
    await db.commit()
    return root_id


async def split_dispatch(root_id: int) -> list[int]:
    """Split a root dispatch — make all subordinates independent again.

    Returns list of newly independent dispatch IDs.
    """
    db = await get_db()

    # Validate root is pending
    root_rows = await db.execute_fetchall(
        "SELECT id, task_id, started_at, error FROM dispatch_queue WHERE id = ?",
        (root_id,)
    )
    if not root_rows:
        raise ValueError("Dispatch not found")
    root = root_rows[0]
    if root["started_at"] is not None or root["error"] is not None:
        raise ValueError("Can only split pending dispatches")

    # Find subordinates
    sub_rows = await db.execute_fetchall(
        "SELECT id FROM dispatch_queue WHERE coalesced_id = ?", (root_id,)
    )
    if not sub_rows:
        raise ValueError("No subordinate dispatches to split")

    sub_ids = [r["id"] for r in sub_rows]

    # Look up the job's current require_approval setting
    prop_rows = await db.execute_fetchall(
        "SELECT value FROM task_properties WHERE task_id = ? AND key = 'require_approval'",
        (root["task_id"],)
    )
    require_approval = prop_rows[0]["value"] == "true" if prop_rows else False
    approval_val = "pending" if require_approval else None

    # Clear coalesced_id, append to end of queue, apply approval setting
    from backend.state import utcnow
    now = utcnow()
    placeholders = ",".join("?" * len(sub_ids))
    await db.execute(
        f"""UPDATE dispatch_queue
            SET coalesced_id = NULL, sort_order = NULL, created_at = ?, approval = ?
            WHERE id IN ({placeholders})""",
        [now, approval_val] + sub_ids,
    )
    await db.commit()
    return sub_ids


async def rate_dispatch(dispatch_id: int, rating: str | None) -> bool:
    """Set or clear a rating on a completed dispatch. Rating: 'positive', 'negative', or None."""
    if rating is not None and rating not in ("positive", "negative"):
        raise ValueError("Rating must be 'positive', 'negative', or null")
    db = await get_db()
    cursor = await db.execute(
        "UPDATE dispatch_queue SET rating = ? WHERE id = ? AND completed_at IS NOT NULL",
        (rating, dispatch_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def sweep_stale_dispatches(now: str):
    """Mark any in-flight dispatches as interrupted (e.g. after restart)."""
    db = await get_db()
    # Fetch IDs first so we can return them, then update in one statement
    rows = await db.execute_fetchall(
        "SELECT id FROM dispatch_queue WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    if rows:
        await db.execute(
            "UPDATE dispatch_queue SET completed_at = ?, error = 'interrupted'"
            " WHERE started_at IS NOT NULL AND completed_at IS NULL",
            (now,)
        )
        await db.commit()
    return [row["id"] for row in rows]


async def approve_dispatch(dispatch_id: int) -> bool:
    """Approve a pending-approval dispatch (and its subordinates). Returns True if updated."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE dispatch_queue SET approval = 'approved' WHERE id = ? AND approval = 'pending'",
        (dispatch_id,)
    )
    # Propagate to subordinates
    await db.execute(
        "UPDATE dispatch_queue SET approval = 'approved' WHERE coalesced_id = ? AND approval = 'pending'",
        (dispatch_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def reject_dispatch(dispatch_id: int) -> bool:
    """Reject a pending-approval dispatch (and its subordinates). Marks as skipped."""
    db = await get_db()
    from backend.state import utcnow
    now = utcnow()
    cursor = await db.execute(
        "UPDATE dispatch_queue SET approval = 'rejected', completed_at = ?, error = 'rejected' WHERE id = ? AND approval = 'pending'",
        (now, dispatch_id)
    )
    # Propagate to subordinates
    await db.execute(
        "UPDATE dispatch_queue SET approval = 'rejected', completed_at = ?, error = 'rejected' WHERE coalesced_id = ? AND approval = 'pending'",
        (now, dispatch_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def find_session_by_cli_session(cli_session_id: str) -> str | None:
    """Find a chat session ID by its CLI session ID (for dispatch resume)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM chat_sessions WHERE cli_session_id = ? LIMIT 1",
        (cli_session_id,)
    )
    return rows[0]["id"] if rows else None


# ── Chat ────────────────────────────────────────────────────

async def create_chat_session(task_id: str | None = None, title: str | None = None,
                               dispatch_id: int | None = None) -> dict:
    session_id = str(uuid.uuid4())
    db = await get_db()
    await db.execute(
        "INSERT INTO chat_sessions (id, task_id, dispatch_id, title) VALUES (?, ?, ?, ?)",
        (session_id, task_id, dispatch_id, title)
    )
    await db.commit()
    return {"id": session_id, "task_id": task_id, "dispatch_id": dispatch_id, "title": title}


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
        "SELECT * FROM chat_sessions WHERE dispatch_id IS NULL ORDER BY created_at DESC"
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
    """Insert multiple chat events in a single transaction.

    Each entry is (event_type, raw_json). Used by the worker to flush
    buffered raw audit events at dispatch completion rather than committing
    once per event.
    """
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
