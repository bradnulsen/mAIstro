"""Governor persistence — counters, runs, threads, and messages.

The Governor is the autonomous meta-analysis agent. Its surface is a
two-way thread-based message board (replacing the earlier one-way
findings feed). This module owns:

- the auto-trigger counter (10 terminal tasks → survey)
- ``governor_runs`` audit trail (every invocation, survey or reply)
- ``governor_threads`` and ``governor_messages`` (the conversation)
- read-side helpers used by the survey context packet

Threads are append-only at the message level. Status changes (close,
reopen) are human-only — the route layer is the only caller of
``set_thread_status``, and no MCP write tool touches it.
"""

import json

from backend.db_core import get_db
from backend.db_config import get_config, set_config
from backend.db_triggers import TERMINAL_STATUSES_SQL


# ── Counter ────────────────────────────────────────────────


async def increment_governor_counter() -> int:
    conn = await get_db()
    await conn.execute(
        "UPDATE config SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'governor_task_counter'"
    )
    await conn.commit()
    rows = await conn.execute_fetchall("SELECT value FROM config WHERE key = 'governor_task_counter'")
    return int(rows[0]["value"]) if rows else 0


async def reset_governor_counter():
    await set_config("governor_task_counter", "0")


async def get_governor_counter() -> int:
    val = await get_config("governor_task_counter")
    return int(val) if val else 0


# ── Runs ───────────────────────────────────────────────────


async def create_governor_run(trigger: str, task_count: int | None = None) -> int:
    from backend.state import utcnow
    conn = await get_db()
    cursor = await conn.execute(
        "INSERT INTO governor_runs (trigger, task_count_at_trigger, started_at) VALUES (?, ?, ?)",
        (trigger, task_count, utcnow()),
    )
    await conn.commit()
    return cursor.lastrowid


async def complete_governor_run(run_id: int, message_count: int = 0, error: str | None = None):
    from backend.state import utcnow
    conn = await get_db()
    await conn.execute(
        "UPDATE governor_runs SET completed_at = ?, message_count = ?, error = ? WHERE id = ?",
        (utcnow(), message_count, error, run_id),
    )
    await conn.commit()


async def get_governor_runs(limit: int = 20) -> list[dict]:
    conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT * FROM governor_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


# ── Threads ────────────────────────────────────────────────


def _thread_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "opener": row["opener"],
        "unread_for_human": bool(row["unread_for_human"]),
        "created_at": row["created_at"],
        "last_activity_at": row["last_activity_at"],
        "closed_at": row["closed_at"],
    }


def _message_to_dict(row) -> dict:
    payload = row["action_payload"]
    if payload:
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "author": row["author"],
        "body": row["body"],
        "action_payload": payload,
        "run_id": row["run_id"],
        "created_at": row["created_at"],
    }


async def create_thread(title: str, opener: str) -> int:
    """Create a thread. ``opener`` is ``'governor'`` (survey opened it)
    or ``'human'`` (operator opened it via the UI)."""
    from backend.state import utcnow
    if opener not in ("governor", "human"):
        raise ValueError(f"invalid opener: {opener!r}")
    conn = await get_db()
    now = utcnow()
    cursor = await conn.execute(
        "INSERT INTO governor_threads (title, status, opener, created_at, last_activity_at) "
        "VALUES (?, 'open', ?, ?, ?)",
        (title, opener, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_thread(thread_id: int) -> dict | None:
    """Return a thread row + its full message list (oldest first)."""
    conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT * FROM governor_threads WHERE id = ?", (thread_id,)
    )
    if not rows:
        return None
    thread = _thread_to_dict(rows[0])
    msgs = await conn.execute_fetchall(
        "SELECT * FROM governor_messages WHERE thread_id = ? ORDER BY id ASC",
        (thread_id,),
    )
    thread["messages"] = [_message_to_dict(m) for m in msgs]
    return thread


async def list_threads(status: str | None = None, limit: int = 100) -> list[dict]:
    """List threads ordered by ``last_activity_at`` desc.

    Each row includes a ``last_message`` preview (body + author) so the
    UI's left pane can render without a follow-up fetch per thread.
    """
    conn = await get_db()
    where = ""
    params: list = []
    if status:
        where = "WHERE t.status = ?"
        params.append(status)
    params.append(limit)
    rows = await conn.execute_fetchall(
        f"""SELECT t.*,
                  (SELECT body FROM governor_messages
                    WHERE thread_id = t.id ORDER BY id DESC LIMIT 1) AS last_body,
                  (SELECT author FROM governor_messages
                    WHERE thread_id = t.id ORDER BY id DESC LIMIT 1) AS last_author,
                  (SELECT COUNT(*) FROM governor_messages
                    WHERE thread_id = t.id) AS message_count
             FROM governor_threads t
             {where}
             ORDER BY t.last_activity_at DESC
             LIMIT ?""",
        params,
    )
    out = []
    for r in rows:
        d = _thread_to_dict(r)
        d["message_count"] = r["message_count"] or 0
        d["last_message"] = (
            {"body": r["last_body"], "author": r["last_author"]}
            if r["last_body"] is not None
            else None
        )
        out.append(d)
    return out


async def list_open_threads_thin(limit: int = 100) -> list[dict]:
    """Survey-context view: id, title, opener, last_activity_at,
    plus the last 1–2 message bodies for context. No closed threads."""
    conn = await get_db()
    rows = await conn.execute_fetchall(
        """SELECT id, title, opener, last_activity_at
             FROM governor_threads
             WHERE status = 'open'
             ORDER BY last_activity_at DESC
             LIMIT ?""",
        (limit,),
    )
    out = []
    for r in rows:
        msgs = await conn.execute_fetchall(
            "SELECT author, body FROM governor_messages "
            "WHERE thread_id = ? ORDER BY id DESC LIMIT 2",
            (r["id"],),
        )
        out.append({
            "id": r["id"],
            "title": r["title"],
            "opener": r["opener"],
            "last_activity_at": r["last_activity_at"],
            "recent_messages": [
                {"author": m["author"], "body": m["body"]}
                for m in reversed(list(msgs))
            ],
        })
    return out


async def set_thread_status(thread_id: int, status: str) -> bool:
    """Open ↔ close. Human-only — the route layer is the only caller.
    Returns False if the thread doesn't exist or status is unchanged."""
    from backend.state import utcnow
    if status not in ("open", "closed"):
        raise ValueError(f"invalid status: {status!r}")
    conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT status FROM governor_threads WHERE id = ?", (thread_id,)
    )
    if not rows:
        return False
    if rows[0]["status"] == status:
        return False
    closed_at = utcnow() if status == "closed" else None
    await conn.execute(
        "UPDATE governor_threads SET status = ?, closed_at = ? WHERE id = ?",
        (status, closed_at, thread_id),
    )
    await conn.commit()
    return True


async def mark_thread_read(thread_id: int) -> None:
    conn = await get_db()
    await conn.execute(
        "UPDATE governor_threads SET unread_for_human = 0 WHERE id = ?",
        (thread_id,),
    )
    await conn.commit()


# ── Messages ───────────────────────────────────────────────


async def add_message(
    thread_id: int,
    author: str,
    body: str,
    *,
    action_payload: dict | None = None,
    run_id: int | None = None,
) -> int:
    """Append a message to a thread. Bumps ``last_activity_at`` and
    sets ``unread_for_human`` when the author is the Governor."""
    from backend.state import utcnow
    if author not in ("governor", "human"):
        raise ValueError(f"invalid author: {author!r}")
    payload_json = json.dumps(action_payload) if action_payload is not None else None
    conn = await get_db()
    now = utcnow()
    cursor = await conn.execute(
        "INSERT INTO governor_messages (thread_id, author, body, action_payload, run_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, author, body, payload_json, run_id, now),
    )
    unread_clause = ", unread_for_human = 1" if author == "governor" else ""
    await conn.execute(
        f"UPDATE governor_threads SET last_activity_at = ?{unread_clause} WHERE id = ?",
        (now, thread_id),
    )
    await conn.commit()
    return cursor.lastrowid


async def count_unexecuted_proposals() -> int:
    """Open threads where the latest Governor message carries an
    unexecuted ``action_payload``.

    'Unexecuted' is determined narratively: if the latest Governor
    message in an open thread has a non-null ``action_payload`` AND no
    later human/governor message references it, count it. The simple
    proxy: count open threads whose newest message is a Governor
    message with a non-null ``action_payload``. A subsequent Governor
    "I applied this" message would be the latest, so the count
    drops naturally.
    """
    conn = await get_db()
    rows = await conn.execute_fetchall(
        """SELECT t.id FROM governor_threads t
             WHERE t.status = 'open'
               AND EXISTS (
                 SELECT 1 FROM governor_messages m
                  WHERE m.thread_id = t.id
                    AND m.author = 'governor'
                    AND m.action_payload IS NOT NULL
                    AND m.id = (
                      SELECT MAX(id) FROM governor_messages WHERE thread_id = t.id
                    )
               )"""
    )
    return len(rows)


# ── Status / aggregate ─────────────────────────────────────


async def get_governor_status() -> dict:
    """Conversation-surface status: unread threads + pending proposals.

    Internal state (counter, last_run, queue_depth, running flag) lives
    on the debug endpoint, not here.
    """
    conn = await get_db()
    unread_rows = await conn.execute_fetchall(
        "SELECT COUNT(*) AS c FROM governor_threads WHERE status = 'open' AND unread_for_human = 1"
    )
    unread = unread_rows[0]["c"] if unread_rows else 0
    pending = await count_unexecuted_proposals()
    return {
        "unread_threads": int(unread),
        "pending_proposals": int(pending),
    }


async def get_governor_debug() -> dict:
    """Operator-debug state: counter, last_run, queue depth, running flag.

    Backs the collapsed-by-default debug drawer in the Governor view.
    Queue depth and running flag are surfaced by ``backend.governor``;
    this helper returns the DB-derived parts only.
    """
    conn = await get_db()
    counter = await get_governor_counter()
    active = await conn.execute_fetchall(
        "SELECT id FROM governor_runs WHERE completed_at IS NULL LIMIT 1"
    )
    running_db = len(active) > 0
    last = await conn.execute_fetchall(
        "SELECT completed_at, trigger FROM governor_runs "
        "WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
    )
    last_run = last[0]["completed_at"] if last else None
    last_trigger = last[0]["trigger"] if last else None
    return {
        "counter": counter,
        "running_db": running_db,
        "last_run": last_run,
        "last_trigger": last_trigger,
    }


async def get_recent_tasks_for_governor(limit: int = 50) -> list[dict]:
    """Recent terminal tasks with execution metadata for Governor context.

    Reads through tasks_resolved so coalesced subordinates inherit their
    root's execution metrics.
    """
    conn = await get_db()
    rows = await conn.execute_fetchall(
        f"""SELECT t.id, t.job_id, j.name AS job_name, t.status, t.trigger,
                  t.trigger_detail, t.context, t.error,
                  t.stop_reason, t.num_turns, t.cost_usd,
                  t.started_at, t.completed_at,
                  t.start_commit, t.result_commit,
                  t.is_subordinate, t.effective_root_id
           FROM tasks_resolved t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.status IN {TERMINAL_STATUSES_SQL}
           ORDER BY t.completed_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]
