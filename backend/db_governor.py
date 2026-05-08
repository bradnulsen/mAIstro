"""Governor persistence — counters, runs, and findings.

The Governor is the autonomous meta-analysis agent that periodically reviews
recent task activity and emits findings (suggestions or observations). This
module owns the counter that gates run frequency, the `governor_runs`
audit trail, and the `governor_findings` user-facing feed.
"""

from backend.db_core import get_db
from backend.db_config import get_config, set_config
from backend.db_triggers import TERMINAL_STATUSES_SQL


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


async def create_governor_run(trigger: str, task_count: int | None = None) -> int:
    from backend.state import utcnow
    conn = await get_db()
    cursor = await conn.execute(
        "INSERT INTO governor_runs (trigger, task_count_at_trigger, started_at) VALUES (?, ?, ?)",
        (trigger, task_count, utcnow()),
    )
    await conn.commit()
    return cursor.lastrowid


async def complete_governor_run(run_id: int, findings_count: int = 0, error: str | None = None):
    from backend.state import utcnow
    conn = await get_db()
    await conn.execute(
        "UPDATE governor_runs SET completed_at = ?, findings_count = ?, error = ? WHERE id = ?",
        (utcnow(), findings_count, error, run_id),
    )
    await conn.commit()


async def create_governor_finding(run_id: int, type_: str, title: str, body: str) -> int:
    from backend.state import utcnow
    status = "pending" if type_ == "suggestion" else "unread"
    conn = await get_db()
    now = utcnow()
    cursor = await conn.execute(
        "INSERT INTO governor_findings (type, status, title, body, governor_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (type_, status, title, body, run_id, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def update_governor_finding(finding_id: int, **kwargs):
    from backend.state import utcnow
    conn = await get_db()
    kwargs["updated_at"] = utcnow()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [finding_id]
    await conn.execute(f"UPDATE governor_findings SET {sets} WHERE id = ?", vals)
    await conn.commit()


async def get_governor_findings(status: str | None = None, type_: str | None = None, limit: int = 50) -> list[dict]:
    conn = await get_db()
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if type_:
        clauses.append("type = ?")
        params.append(type_)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = await conn.execute_fetchall(
        f"SELECT * FROM governor_findings {where} ORDER BY created_at DESC LIMIT ?", params,
    )
    return [dict(r) for r in rows]


async def get_governor_finding(finding_id: int) -> dict | None:
    conn = await get_db()
    rows = await conn.execute_fetchall("SELECT * FROM governor_findings WHERE id = ?", (finding_id,))
    return dict(rows[0]) if rows else None


async def get_governor_runs(limit: int = 20) -> list[dict]:
    conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT * FROM governor_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


async def get_governor_status() -> dict:
    conn = await get_db()
    counter = await get_governor_counter()
    # Check if a run is active (started but not completed)
    active = await conn.execute_fetchall(
        "SELECT id FROM governor_runs WHERE completed_at IS NULL LIMIT 1"
    )
    running = len(active) > 0
    # Last completed run
    last = await conn.execute_fetchall(
        "SELECT completed_at FROM governor_runs WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
    )
    last_run = last[0]["completed_at"] if last else None
    # Counts for badge
    pending = await conn.execute_fetchall(
        "SELECT COUNT(*) as c FROM governor_findings WHERE status = 'pending'"
    )
    unread = await conn.execute_fetchall(
        "SELECT COUNT(*) as c FROM governor_findings WHERE status = 'unread'"
    )
    return {
        "running": running,
        "counter": counter,
        "last_run": last_run,
        "pending_suggestions": pending[0]["c"],
        "unread_observations": unread[0]["c"],
    }


async def get_recent_tasks_for_governor(limit: int = 50) -> list[dict]:
    """Recent terminal tasks with execution metadata for Governor context.

    Reads through tasks_resolved so coalesced subordinates inherit their
    root's execution metrics — otherwise the Governor sees null
    num_turns/cost_usd/stop_reason on every subordinate and cannot tell
    which contexts were actually run vs. folded into another run.
    """
    conn = await get_db()
    rows = await conn.execute_fetchall(
        f"""SELECT t.id, t.job_id, j.name AS job_name, t.status, t.trigger,
                  t.trigger_detail, t.context, t.error,
                  t.stop_reason, t.num_turns, t.cost_usd,
                  t.started_at, t.completed_at,
                  t.is_subordinate, t.effective_root_id
           FROM tasks_resolved t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.status IN {TERMINAL_STATUSES_SQL}
           ORDER BY t.completed_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]
