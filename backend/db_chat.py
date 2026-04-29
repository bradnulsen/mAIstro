"""Chat sessions, messages, and the raw event log.

Each task creates a chat session which holds the durable record of the run.
`chat_events` is the source of truth — every NDJSON line from the CLI is
persisted there incrementally during streaming. `chat_messages` is the
materialized assistant-text view written after the run; if it ends up empty
(post-processing skipped or failed), `reconstruct_output_from_events` parses
`chat_events` to recover the user-visible response.
"""

import json
import logging
import uuid

from backend.db_core import get_db

log = logging.getLogger("maistro.database")


async def find_session_by_cli_session(cli_session_id: str) -> str | None:
    """Find a chat session ID by its CLI session ID (for task resume)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM chat_sessions WHERE cli_session_id = ? LIMIT 1",
        (cli_session_id,)
    )
    return rows[0]["id"] if rows else None


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
        except Exception as e:
            log.warning("[database] Failed to parse chat event for session %s: %s", session_id, e)
            continue
    if not text_parts:
        return []
    return [{
        "id": None,
        "session_id": session_id,
        "role": "assistant",
        "content": "\n\n".join(text_parts),
        "created_at": None,
    }]


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
