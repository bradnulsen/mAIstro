"""Per-job learnings — discrete, addressable, individually-toggleable rules.

Learnings complement the prose `description` property: prose carries
narrative framing (mission, scope, voice); learnings carry the kind of
guidance that benefits from per-row toggle, reorder, source provenance,
and (when permitted) agent write-back. See
architecture/proposals/job-learnings-decomposition.md.

`list_enabled_learnings` is the read path used by prompt assembly. The
remaining helpers back the operator CRUD surface (and, when the
self-modification permission lands, the agent write tools — which pass
``source='agent'`` and respect the asymmetric edit/delete rules).
"""

from backend.db_core import get_db


async def list_enabled_learnings(job_id: int) -> list[str]:
    """Return enabled learning bodies for a job, ordered by position.

    Used by ``dispatch.build_user_prompt`` to append the ``## Learnings``
    section. Disabled rows are excluded; jobs with no enabled learnings
    return an empty list and the section is omitted from the prompt.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT body FROM job_learnings "
        "WHERE job_id = ? AND enabled = 1 "
        "ORDER BY position, id",
        (job_id,),
    )
    return [r["body"] for r in rows]


async def list_learnings(job_id: int) -> list[dict]:
    """Return every learning for a job (enabled and disabled), ordered.

    Used by the operator UI; includes the fields the operator needs to
    render and edit each row.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, job_id, body, enabled, position, source, created_at, updated_at "
        "FROM job_learnings "
        "WHERE job_id = ? "
        "ORDER BY position, id",
        (job_id,),
    )
    return [
        {
            "id": r["id"],
            "job_id": r["job_id"],
            "body": r["body"],
            "enabled": bool(r["enabled"]),
            "position": r["position"],
            "source": r["source"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


async def create_learning(job_id: int, body: str, source: str = "human") -> dict:
    """Insert a new learning at the end of the job's position list."""
    db = await get_db()
    cur = await db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
        "FROM job_learnings WHERE job_id = ?",
        (job_id,),
    )
    row = await cur.fetchone()
    next_pos = row["next_pos"] if row else 0

    cur = await db.execute(
        "INSERT INTO job_learnings (job_id, body, position, source) "
        "VALUES (?, ?, ?, ?)",
        (job_id, body, next_pos, source),
    )
    await db.commit()
    learning_id = cur.lastrowid
    return await get_learning(learning_id)


async def get_learning(learning_id: int) -> dict | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, job_id, body, enabled, position, source, created_at, updated_at "
        "FROM job_learnings WHERE id = ?",
        (learning_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "body": row["body"],
        "enabled": bool(row["enabled"]),
        "position": row["position"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def update_learning(
    learning_id: int,
    *,
    body: str | None = None,
    enabled: bool | None = None,
) -> dict | None:
    """Update body and/or enabled flag; bumps updated_at.

    Toggling ``enabled`` is intentionally distinct from agent self-edits
    of the body — the operator UI can do both, but the agent MCP tools
    will only touch ``body`` (the proposal forbids agents from flipping
    ``enabled``).
    """
    sets = []
    params: list = []
    if body is not None:
        sets.append("body = ?")
        params.append(body)
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not sets:
        return await get_learning(learning_id)

    sets.append("updated_at = datetime('now')")
    params.append(learning_id)

    db = await get_db()
    await db.execute(
        f"UPDATE job_learnings SET {', '.join(sets)} WHERE id = ?",
        tuple(params),
    )
    await db.commit()
    return await get_learning(learning_id)


async def delete_learning(learning_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM job_learnings WHERE id = ?", (learning_id,))
    await db.commit()
    return cur.rowcount > 0


async def reorder_learnings(job_id: int, learning_ids: list[int]) -> None:
    """Apply the given order as positions 0..N-1 for the job's learnings.

    Ids not belonging to the job are silently ignored — the UPDATE's
    job_id predicate guards against accidental cross-job moves.
    """
    db = await get_db()
    for pos, learning_id in enumerate(learning_ids):
        await db.execute(
            "UPDATE job_learnings SET position = ?, updated_at = datetime('now') "
            "WHERE id = ? AND job_id = ?",
            (pos, learning_id, job_id),
        )
    await db.commit()
