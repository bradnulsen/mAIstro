"""SQLite database layer for mAistro — schema, init, CRUD helpers."""

import aiosqlite
import json
import os
import re
import uuid

DB_PATH: str | None = None
_conn: aiosqlite.Connection | None = None


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
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


async def close_db():
    """Close the persistent connection. Call from lifespan teardown."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def init_db(project_dir: str):
    global DB_PATH
    # Close previous connection if switching projects
    await close_db()
    DB_PATH = get_db_path(project_dir)
    db = await get_db()
    await db.executescript(SCHEMA_SQL)
    await db.executescript(SEED_SQL)
    await db.commit()
    await _migrate_db(db)


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
    context TEXT,
    session_id TEXT,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    result_commit TEXT,
    error TEXT
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
"""

SEED_SQL = """
INSERT OR IGNORE INTO task_property_defs (key, default_value, type) VALUES
    ('description', '', 'string'),
    ('instructions', '', 'string'),
    ('model', 'sonnet', 'string'),
    ('base_tools', '[]', 'json'),
    ('disallowed_tools', '[]', 'json'),
    ('mcp_servers', '[]', 'json'),
    ('subscriptions', '[]', 'json'),
    ('coalesce_dispatches', 'false', 'boolean'),
    ('sort_order', '0', 'integer'),
    ('schedule', '', 'string'),
    ('timeout', '900', 'integer');

INSERT OR IGNORE INTO config (key, value) VALUES ('queue_auto_dispatch', 'false');
"""


async def _migrate_db(db: aiosqlite.Connection):
    """Idempotent schema migrations for existing databases."""
    # Add session_id to dispatch_queue
    cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(dispatch_queue)")}
    if "session_id" not in cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN session_id TEXT")

    # Add dispatch_id to chat_sessions
    cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(chat_sessions)")}
    if "dispatch_id" not in cols:
        await db.execute("ALTER TABLE chat_sessions ADD COLUMN dispatch_id INTEGER")

    # Migrate mode=watch tasks to watch_enabled=true (historical)
    await db.execute("""
        INSERT OR IGNORE INTO task_properties (task_id, key, value)
        SELECT task_id, 'watch_enabled', 'true'
        FROM task_properties
        WHERE key = 'mode' AND value = 'watch'
    """)

    # Remove legacy mode, running, and watch_enabled properties
    # watch_enabled is superseded by subscriptions-as-watch-enable: having subscriptions implies watching
    await db.execute("DELETE FROM task_properties WHERE key IN ('mode', 'running', 'watch_enabled')")
    await db.execute("DELETE FROM task_property_defs WHERE key IN ('mode', 'running', 'watch_enabled')")

    # Add dispatch_method to dispatch_queue
    dq_cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(dispatch_queue)")}
    if "dispatch_method" not in dq_cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN dispatch_method TEXT")

    # Add triggers JSON array column for enqueue-time coalescing
    if "triggers" not in dq_cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN triggers TEXT")

    # Add resume_session_id for dispatch continuity (resume from CLI session)
    if "resume_session_id" not in dq_cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN resume_session_id TEXT")

    # Add start_commit for dispatch diff view (HEAD at dispatch start)
    if "start_commit" not in dq_cols:
        await db.execute("ALTER TABLE dispatch_queue ADD COLUMN start_commit TEXT")

    await db.commit()


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


async def get_task(task_id: str, running_ids: set | None = None) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id, name, created_at FROM tasks WHERE id = ?", (task_id,)
    )
    if not row:
        return None
    task = dict(row[0])

    # Get all property defs with defaults, override with task-specific values
    props = {}
    defs = await db.execute_fetchall("SELECT key, default_value, type FROM task_property_defs")
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
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id FROM tasks")
    # Fetch running task IDs in one query to avoid N+1
    running_rows = await db.execute_fetchall(
        "SELECT DISTINCT task_id FROM dispatch_queue WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    running_ids = {r["task_id"] for r in running_rows}

    tasks = []
    for row in rows:
        task = await get_task(row["id"], running_ids=running_ids)
        if task:
            tasks.append(task)
    tasks.sort(key=lambda t: t["properties"].get("sort_order", 0))
    return tasks


async def update_task(task_id: str, updates: dict) -> dict | None:
    db = await get_db()
    existing = await get_task(task_id)
    if not existing:
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
    cursor = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await db.commit()
    return cursor.rowcount > 0


# ── Dispatch Queue ──────────────────────────────────────────

async def enqueue_dispatch(task_id: str, trigger: str,
                           trigger_detail: str | None = None,
                           context: str | None = None) -> int:
    """Enqueue a dispatch, coalescing into an existing pending record if applicable.

    Coalescing rules:
    - 'schedule' always coalesces globally — repeated fires are identical signals
    - 'commit' always coalesces with other pending 'commit' dispatches — multiple commits
      while a task is busy = one catch-up run covering all of them
    - coalesce_dispatches=true coalesces globally — never more than one pending dispatch
      regardless of trigger type (useful for tasks that just need "run when things change")
    - All other triggers (manual, task_queue, resume, retry) never coalesce — each
      represents a distinct explicit intent
    """
    new_entry = {"trigger": trigger, "detail": trigger_detail, "context": context}
    db = await get_db()

    task = await get_task(task_id)
    coalesce_global = (trigger == "schedule") or (task and task["properties"].get("coalesce_dispatches"))
    coalesce_same_type = (trigger == "commit")

    if coalesce_global or coalesce_same_type:
        query = """SELECT id, trigger, trigger_detail, context, triggers
                   FROM dispatch_queue
                   WHERE task_id = ? AND started_at IS NULL AND error IS NULL"""
        params: list = [task_id]
        if coalesce_same_type and not coalesce_global:
            # Commit coalescing: only merge with another pending commit dispatch
            query += " AND trigger = ?"
            params.append(trigger)
        query += " ORDER BY created_at ASC LIMIT 1"
        rows = await db.execute_fetchall(query, params)
        if rows:
            existing = dict(rows[0])
            # Build triggers array from existing record
            if existing["triggers"]:
                triggers = json.loads(existing["triggers"])
            else:
                # Migrate: first entry from the original scalar fields
                triggers = [{"trigger": existing["trigger"],
                             "detail": existing["trigger_detail"],
                             "context": existing["context"]}]
            triggers.append(new_entry)
            await db.execute(
                "UPDATE dispatch_queue SET triggers = ? WHERE id = ?",
                (json.dumps(triggers), existing["id"])
            )
            await db.commit()
            return existing["id"]

    # No coalescing — insert new record
    triggers_json = json.dumps([new_entry])
    cursor = await db.execute(
        """INSERT INTO dispatch_queue (task_id, trigger, trigger_detail, context, triggers)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, trigger, trigger_detail, context, triggers_json)
    )
    await db.commit()
    return cursor.lastrowid


def _parse_dispatch_row(row) -> dict:
    """Convert a dispatch row, parsing the triggers JSON column."""
    d = dict(row)
    if d.get("triggers"):
        try:
            d["triggers"] = json.loads(d["triggers"])
        except (json.JSONDecodeError, TypeError):
            d["triggers"] = None
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
        """SELECT dq.*, t.name as task_name FROM dispatch_queue dq
           JOIN tasks t ON t.id = dq.task_id
           ORDER BY dq.created_at DESC LIMIT ?""",
        (limit,)
    )
    return [_parse_dispatch_row(r) for r in rows]


async def get_oldest_pending_dispatch() -> dict | None:
    """Get the oldest dispatch that hasn't started yet."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT dq.*, t.name as task_name FROM dispatch_queue dq
           JOIN tasks t ON t.id = dq.task_id
           WHERE dq.started_at IS NULL AND dq.error IS NULL
           ORDER BY dq.created_at ASC LIMIT 1"""
    )
    return _parse_dispatch_row(rows[0]) if rows else None


async def has_pending_dispatch(task_id: str) -> bool:
    """Check if a task has any pending (not started) dispatches."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT 1 FROM dispatch_queue WHERE task_id = ? AND started_at IS NULL AND error IS NULL LIMIT 1",
        (task_id,),
    )
    return bool(rows)


async def update_dispatch(dispatch_id: int, **kwargs):
    db = await get_db()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [dispatch_id]
    await db.execute(f"UPDATE dispatch_queue SET {sets} WHERE id = ?", vals)
    await db.commit()


async def sweep_stale_dispatches(now: str):
    """Mark any in-flight dispatches as interrupted (e.g. after restart)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM dispatch_queue WHERE started_at IS NOT NULL AND completed_at IS NULL"
    )
    for row in rows:
        await db.execute(
            "UPDATE dispatch_queue SET completed_at = ?, error = ? WHERE id = ?",
            (now, "interrupted", row["id"])
        )
    await db.commit()
    return [row["id"] for row in rows]


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
