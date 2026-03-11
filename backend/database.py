"""SQLite database layer for mAistro — schema, init, CRUD helpers."""

import aiosqlite
import json
import os
import re
from pathlib import Path

DB_PATH: str | None = None


def get_db_path(project_dir: str) -> str:
    maistro_dir = os.path.join(project_dir, ".maistro")
    os.makedirs(maistro_dir, exist_ok=True)
    return os.path.join(maistro_dir, "maistro.db")


async def get_db() -> aiosqlite.Connection:
    assert DB_PATH, "Database not initialized — call init_db first"
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db(project_dir: str):
    global DB_PATH
    DB_PATH = get_db_path(project_dir)
    db = await get_db()
    try:
        await db.executescript(SCHEMA_SQL)
        await db.executescript(SEED_SQL)
        await db.commit()
    finally:
        await db.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_property_defs (
    key TEXT PRIMARY KEY,
    default_value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string'
);

CREATE TABLE IF NOT EXISTS agent_properties (
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    key TEXT REFERENCES agent_property_defs(key),
    value TEXT NOT NULL,
    PRIMARY KEY (agent_id, key)
);

CREATE TABLE IF NOT EXISTS dispatch_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    trigger TEXT NOT NULL,
    trigger_detail TEXT,
    instructions TEXT,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    result_commit TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
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
INSERT OR IGNORE INTO agent_property_defs (key, default_value, type) VALUES
    ('persona', '', 'string'),
    ('model', 'sonnet', 'string'),
    ('base_tools', '["Read","Write","Edit","Glob","Grep","Bash"]', 'json'),
    ('mcp_servers', '[]', 'json'),
    ('input_artifacts', '[]', 'json'),
    ('output_artifacts', '[]', 'json'),
    ('mode', 'manual', 'string'),
    ('cooldown_seconds', '30', 'integer'),
    ('running', 'false', 'boolean'),
    ('sort_order', '0', 'integer');
"""


def slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug


# ── Agent CRUD ──────────────────────────────────────────────

async def create_agent(name: str, properties: dict | None = None) -> dict:
    agent_id = slugify(name)
    db = await get_db()
    try:
        await db.execute("INSERT INTO agents (id, name) VALUES (?, ?)", (agent_id, name))
        if properties:
            for key, value in properties.items():
                val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
                await db.execute(
                    "INSERT OR REPLACE INTO agent_properties (agent_id, key, value) VALUES (?, ?, ?)",
                    (agent_id, key, val)
                )
        await db.commit()
        return await get_agent(agent_id, db=db)
    finally:
        await db.close()


async def get_agent(agent_id: str, db: aiosqlite.Connection | None = None) -> dict | None:
    close = db is None
    if db is None:
        db = await get_db()
    try:
        row = await db.execute_fetchall(
            "SELECT id, name, created_at FROM agents WHERE id = ?", (agent_id,)
        )
        if not row:
            return None
        agent = dict(row[0])

        # Get all property defs with defaults, override with agent-specific values
        props = {}
        defs = await db.execute_fetchall("SELECT key, default_value, type FROM agent_property_defs")
        for d in defs:
            props[d["key"]] = _cast_property(d["default_value"], d["type"])

        agent_props = await db.execute_fetchall(
            "SELECT key, value FROM agent_properties WHERE agent_id = ?", (agent_id,)
        )
        def_types = {d["key"]: d["type"] for d in defs}
        for p in agent_props:
            props[p["key"]] = _cast_property(p["value"], def_types.get(p["key"], "string"))

        agent["properties"] = props
        return agent
    finally:
        if close:
            await db.close()


async def list_agents() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id FROM agents ORDER BY id"
        )
        agents = []
        for row in rows:
            agent = await get_agent(row["id"], db=db)
            if agent:
                agents.append(agent)
        # Sort by sort_order property
        agents.sort(key=lambda a: a["properties"].get("sort_order", 0))
        return agents
    finally:
        await db.close()


async def update_agent(agent_id: str, updates: dict) -> dict | None:
    db = await get_db()
    try:
        existing = await get_agent(agent_id, db=db)
        if not existing:
            return None

        if "name" in updates:
            await db.execute("UPDATE agents SET name = ? WHERE id = ?", (updates.pop("name"), agent_id))

        for key, value in updates.items():
            val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            await db.execute(
                "INSERT OR REPLACE INTO agent_properties (agent_id, key, value) VALUES (?, ?, ?)",
                (agent_id, key, val)
            )
        await db.commit()
        return await get_agent(agent_id, db=db)
    finally:
        await db.close()


async def delete_agent(agent_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# ── Dispatch Queue ──────────────────────────────────────────

async def enqueue_dispatch(agent_id: str, trigger: str,
                           trigger_detail: str | None = None,
                           instructions: str | None = None) -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO dispatch_queue (agent_id, trigger, trigger_detail, instructions)
               VALUES (?, ?, ?, ?)""",
            (agent_id, trigger, trigger_detail, instructions)
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def get_dispatch(dispatch_id: int) -> dict | None:
    d = await get_db()
    try:
        rows = await d.execute_fetchall(
            "SELECT dq.*, a.name as agent_name FROM dispatch_queue dq JOIN agents a ON a.id = dq.agent_id WHERE dq.id = ?",
            (dispatch_id,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await d.close()


async def get_dispatch_queue(limit: int = 50) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT dq.*, a.name as agent_name FROM dispatch_queue dq
               JOIN agents a ON a.id = dq.agent_id
               ORDER BY dq.created_at DESC LIMIT ?""",
            (limit,)
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_dispatch(dispatch_id: int, **kwargs):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [dispatch_id]
        await db.execute(f"UPDATE dispatch_queue SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def set_agent_running(agent_id: str, running: bool):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO agent_properties (agent_id, key, value) VALUES (?, 'running', ?)",
            (agent_id, str(running).lower())
        )
        await db.commit()
    finally:
        await db.close()


# ── Chat ────────────────────────────────────────────────────

async def create_chat_session(agent_id: str, title: str | None = None) -> dict:
    import uuid
    session_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO chat_sessions (id, agent_id, title) VALUES (?, ?, ?)",
            (session_id, agent_id, title)
        )
        await db.commit()
        return {"id": session_id, "agent_id": agent_id, "title": title}
    finally:
        await db.close()


async def add_chat_message(session_id: str, role: str, content: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content)
        )
        await db.commit()
    finally:
        await db.close()


async def get_chat_sessions(agent_id: str | None = None) -> list[dict]:
    db = await get_db()
    try:
        if agent_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM chat_sessions WHERE agent_id = ? ORDER BY created_at DESC",
                (agent_id,)
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM chat_sessions ORDER BY created_at DESC"
            )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_chat_messages(session_id: str) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at",
            (session_id,)
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_chat_session(session_id: str, **kwargs):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [session_id]
        await db.execute(f"UPDATE chat_sessions SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


# ── Config ──────────────────────────────────────────────────

async def get_config(key: str | None = None) -> dict | str | None:
    db = await get_db()
    try:
        if key:
            rows = await db.execute_fetchall("SELECT value FROM config WHERE key = ?", (key,))
            return rows[0]["value"] if rows else None
        rows = await db.execute_fetchall("SELECT key, value FROM config")
        return {r["key"]: r["value"] for r in rows}
    finally:
        await db.close()


async def set_config(key: str, value: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()
    finally:
        await db.close()


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
