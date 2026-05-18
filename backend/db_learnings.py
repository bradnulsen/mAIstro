"""Per-job learnings — discrete, addressable, individually-toggleable rules.

Learnings complement the prose `description` property: prose carries
narrative framing (mission, scope, voice); learnings carry the kind of
guidance that benefits from per-row toggle, reorder, source provenance,
and (when permitted) agent write-back. See
architecture/proposals/job-learnings-decomposition.md.

Learnings are no longer auto-injected into the prompt. Agents query them
on demand via the internal MCP tools — `list_learnings` returns a
breadth view (id + one-line ``summary``) and `read_learnings` fetches
the full bodies for ids the agent decides are relevant. The cap on
count and per-row size keeps the addressable surface bounded so the
agent's index call stays cheap.
"""

from backend.db_core import get_db


SUMMARY_MAX_CHARS = 120


class LearningCapacityError(Exception):
    """Raised by ``create_learning`` when the job is at its ``max_learnings`` cap."""


class LearningSizeError(Exception):
    """Raised by ``create_learning`` / ``update_learning`` when body exceeds
    ``max_learning_chars`` or summary exceeds ``SUMMARY_MAX_CHARS``."""


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "summary": row["summary"],
        "body": row["body"],
        "enabled": bool(row["enabled"]),
        "position": row["position"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def list_learnings(job_id: int) -> list[dict]:
    """Return every learning for a job (enabled and disabled), ordered.

    Used by the operator UI; includes the fields the operator needs to
    render and edit each row.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, job_id, summary, body, enabled, position, source, created_at, updated_at "
        "FROM job_learnings "
        "WHERE job_id = ? "
        "ORDER BY position, id",
        (job_id,),
    )
    return [_row_to_dict(r) for r in rows]


async def learning_counts_by_job() -> dict[int, dict]:
    """Per-job count of enabled / total learnings, in one query.

    Used by the Governor's context packet to surface that learnings exist
    for a job without pulling every body into the prompt up front. The
    Governor follows up with `get_job_learnings` when a job's count
    suggests there's something worth reading.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT job_id, "
        "SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled_count, "
        "COUNT(*) AS total_count "
        "FROM job_learnings GROUP BY job_id"
    )
    return {
        r["job_id"]: {"enabled": r["enabled_count"], "total": r["total_count"]}
        for r in rows
    }


async def list_learning_summaries(job_id: int) -> list[dict]:
    """Breadth view used by the agent's `list_learnings` MCP tool.

    Returns just enough for the agent to decide which rows to deep-dive:
    id, summary, enabled flag, source. Excludes disabled rows so the
    agent doesn't waste a turn fetching guidance the operator muted.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, summary, source FROM job_learnings "
        "WHERE job_id = ? AND enabled = 1 "
        "ORDER BY position, id",
        (job_id,),
    )
    return [
        {"id": r["id"], "summary": r["summary"], "source": r["source"]}
        for r in rows
    ]


async def get_learning(learning_id: int) -> dict | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT id, job_id, summary, body, enabled, position, source, created_at, updated_at "
        "FROM job_learnings WHERE id = ?",
        (learning_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return _row_to_dict(row)


async def get_learnings_by_ids(job_id: int, ids: list[int]) -> list[dict]:
    """Depth view for the agent's `read_learnings` MCP tool.

    Filters on ``job_id`` so the agent can't reach into other jobs'
    rows even if it guesses ids. Preserves request order so the agent
    can rely on the response shape.
    """
    if not ids:
        return []
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = await db.execute_fetchall(
        f"SELECT id, job_id, summary, body, enabled, position, source, "
        f"created_at, updated_at "
        f"FROM job_learnings WHERE job_id = ? AND id IN ({placeholders})",
        (job_id, *ids),
    )
    by_id = {r["id"]: _row_to_dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def _count_learnings(job_id: int) -> int:
    db = await get_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM job_learnings WHERE job_id = ?",
        (job_id,),
    )
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


def _validate_size(summary: str, body: str, max_body_chars: int) -> None:
    if len(summary) > SUMMARY_MAX_CHARS:
        raise LearningSizeError(
            f"summary exceeds {SUMMARY_MAX_CHARS} chars (got {len(summary)})"
        )
    if len(body) > max_body_chars:
        raise LearningSizeError(
            f"body exceeds {max_body_chars} chars (got {len(body)})"
        )


async def create_learning(
    job_id: int,
    body: str,
    summary: str,
    *,
    source: str = "human",
    max_count: int,
    max_body_chars: int,
) -> dict:
    """Insert a new learning at the end of the job's position list.

    ``max_count`` and ``max_body_chars`` come from the job's properties
    (``max_learnings`` and ``max_learning_chars``); routes resolve them
    before calling. The cap applies to all sources — operators raise
    the limit by editing the property, not by bypassing it.
    """
    _validate_size(summary, body, max_body_chars)

    current = await _count_learnings(job_id)
    if current >= max_count:
        raise LearningCapacityError(
            f"job {job_id} has {current} learnings (cap {max_count}); "
            f"update or delete an existing one before adding"
        )

    db = await get_db()
    cur = await db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
        "FROM job_learnings WHERE job_id = ?",
        (job_id,),
    )
    row = await cur.fetchone()
    next_pos = row["next_pos"] if row else 0

    cur = await db.execute(
        "INSERT INTO job_learnings (job_id, summary, body, position, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, summary, body, next_pos, source),
    )
    await db.commit()
    return await get_learning(cur.lastrowid)


async def update_learning(
    learning_id: int,
    *,
    summary: str | None = None,
    body: str | None = None,
    enabled: bool | None = None,
    require_source: str | None = None,
    max_body_chars: int | None = None,
) -> dict | None:
    """Update summary, body, and/or enabled flag; bumps updated_at.

    Toggling ``enabled`` is intentionally distinct from agent self-edits
    of content — the operator UI can do both, but the agent MCP tools
    only touch ``summary`` and ``body``.

    ``require_source`` enforces the asymmetric agent-write rule when set:
    the row must have a matching ``source`` or the update is rejected
    (returns ``None``). Operator routes pass ``None``; the agent MCP
    write path passes ``'agent'`` so agent tools can only edit
    learnings the agent itself authored.

    ``max_body_chars`` is required when ``body`` is supplied, and the
    summary length is always validated against ``SUMMARY_MAX_CHARS``.
    """
    if require_source is not None:
        existing = await get_learning(learning_id)
        if existing is None or existing["source"] != require_source:
            return None

    if summary is not None and len(summary) > SUMMARY_MAX_CHARS:
        raise LearningSizeError(
            f"summary exceeds {SUMMARY_MAX_CHARS} chars (got {len(summary)})"
        )
    if body is not None:
        if max_body_chars is None:
            raise ValueError("max_body_chars required when updating body")
        if len(body) > max_body_chars:
            raise LearningSizeError(
                f"body exceeds {max_body_chars} chars (got {len(body)})"
            )

    sets = []
    params: list = []
    if summary is not None:
        sets.append("summary = ?")
        params.append(summary)
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


async def delete_learning(learning_id: int, *, require_source: str | None = None) -> bool:
    """Hard-delete a learning. ``require_source`` mirrors ``update_learning``
    — when set, the row must have a matching ``source`` or the delete is
    refused (returns ``False``)."""
    if require_source is not None:
        existing = await get_learning(learning_id)
        if existing is None or existing["source"] != require_source:
            return False
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
