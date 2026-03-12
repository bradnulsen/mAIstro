"""App-level state — lives in the application directory, not the project.

Stores recent projects and app-wide preferences that persist across sessions.
"""

import os
import sqlite3
from datetime import datetime, timezone

# App DB lives in the repo root, next to backend/ and frontend/
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DB_PATH = os.path.join(APP_DIR, "app.db")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    """Initialize the app-level database."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recent_projects (
            path TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            opened_at DATETIME NOT NULL
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
