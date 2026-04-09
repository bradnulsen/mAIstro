"""App-level state — lives in the application directory, not the project.

Stores recent projects and app-wide preferences that persist across sessions.
"""

import os
import sqlite3
from datetime import datetime, timezone

# App DB lives in .maistro/ at the repo root (gitignored)
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAISTRO_DIR = os.path.join(APP_DIR, ".maistro")
APP_DB_PATH = os.path.join(_MAISTRO_DIR, "app.db")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    """Initialize the app-level database."""
    os.makedirs(_MAISTRO_DIR, exist_ok=True)
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recent_projects (
            path TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            opened_at DATETIME NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            properties TEXT NOT NULL,
            created_at DATETIME NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def touch_project(path: str):
    """Record or update a project as recently opened."""
    name = os.path.basename(path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO recent_projects (path, name, opened_at) VALUES (?, ?, ?)",
        (path, name, now),
    )
    conn.commit()
    conn.close()


def list_recent(limit: int = 20) -> list[dict]:
    """Get recently opened projects, most recent first."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT path, name, opened_at FROM recent_projects ORDER BY opened_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_project(path: str):
    """Remove a project from the recent list."""
    conn = _get_db()
    conn.execute("DELETE FROM recent_projects WHERE path = ?", (path,))
    conn.commit()
    conn.close()


# ── Job Templates ─────────────────────────────────────────

# Properties that are portable across projects (the rest are project-scoped).
PORTABLE_PROPERTIES = {
    "summary", "description", "model", "allowed_tools", "allowed_internal_tools",
    "timeout", "max_turns", "coalesce_tasks", "require_approval", "schedule",
}


def save_template(name: str, properties: dict) -> int:
    """Save a job template. Returns the new template ID."""
    import json
    portable = {k: v for k, v in properties.items() if k in PORTABLE_PROPERTIES}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    cur = conn.execute(
        "INSERT INTO job_templates (name, properties, created_at) VALUES (?, ?, ?)",
        (name, json.dumps(portable), now),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def list_templates() -> list[dict]:
    """Get all job templates, ordered by name."""
    import json
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, name, properties, created_at FROM job_templates ORDER BY name"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["properties"] = json.loads(d["properties"])
        except Exception:
            d["properties"] = {}
        result.append(d)
    return result


def get_template(template_id: int) -> dict | None:
    """Get a single template by ID."""
    import json
    conn = _get_db()
    row = conn.execute(
        "SELECT id, name, properties, created_at FROM job_templates WHERE id = ?",
        (template_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["properties"] = json.loads(d["properties"])
    except Exception:
        d["properties"] = {}
    return d


def update_template(template_id: int, name: str, properties: dict):
    """Overwrite an existing template's name and properties."""
    import json
    portable = {k: v for k, v in properties.items() if k in PORTABLE_PROPERTIES}
    conn = _get_db()
    conn.execute(
        "UPDATE job_templates SET name = ?, properties = ? WHERE id = ?",
        (name, json.dumps(portable), template_id),
    )
    conn.commit()
    conn.close()


def delete_template(template_id: int):
    """Delete a job template."""
    conn = _get_db()
    conn.execute("DELETE FROM job_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
