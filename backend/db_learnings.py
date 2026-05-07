"""Per-job learnings — discrete, addressable, individually-toggleable rules.

Learnings complement the prose `description` property: prose carries
narrative framing (mission, scope, voice); learnings carry the kind of
guidance that benefits from per-row toggle, reorder, source provenance,
and (when permitted) agent write-back. See
architecture/proposals/job-learnings-decomposition.md.

Step 1 ships only the read path used by prompt assembly. CRUD helpers
and write-side MCP tools land in subsequent steps.
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
