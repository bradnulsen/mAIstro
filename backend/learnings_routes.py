"""Per-job learnings — operator and agent CRUD surfaces.

The UI in Tasks.jsx renders the list under each job's description and
calls the operator routes for add / edit / toggle / delete / reorder
(all ``source='human'``). The internal MCP server's learning write tools
call the ``/agent/`` routes, which stamp ``source='agent'`` on creates
and refuse to mutate human-authored rows. Operators retain authority over
both sources via the operator routes.

The agent permission gate (``allow_learning_self_modification``) lives on
the MCP server side: the routes here always honour the ``source='agent'``
contract regardless of whether the job has writes enabled — the gate just
controls whether the write tools are presented to the agent at all.

The ``max_learnings`` and ``max_learning_chars`` job properties bound the
list. Cap and size violations from ``db_learnings`` surface here as 409
(capacity) and 413 (size) so callers can distinguish "list full" from
"this row is too big".
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db, db_learnings
from backend.db_learnings import LearningCapacityError, LearningSizeError
from backend.state import require_project

router = APIRouter(tags=["learnings"])


def _caps(job: dict) -> tuple[int, int]:
    props = job.get("properties") or {}
    max_count = int(props.get("max_learnings") or 10)
    max_body_chars = int(props.get("max_learning_chars") or 1000)
    return max_count, max_body_chars


class CreateLearningRequest(BaseModel):
    summary: str
    body: str


class UpdateLearningRequest(BaseModel):
    summary: str | None = None
    body: str | None = None
    enabled: bool | None = None


class ReorderLearningsRequest(BaseModel):
    learning_ids: list[int]


class ReadLearningsRequest(BaseModel):
    job_id: int
    ids: list[int]


@router.get("/api/jobs/{job_id}/learnings")
async def list_job_learnings(job_id: int):
    require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return await db_learnings.list_learnings(job_id)


@router.post("/api/jobs/{job_id}/learnings")
async def create_job_learning(job_id: int, req: CreateLearningRequest):
    require_project()
    summary = (req.summary or "").strip()
    body = (req.body or "").strip()
    if not summary:
        raise HTTPException(400, "summary is required")
    if not body:
        raise HTTPException(400, "body is required")
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    max_count, max_body_chars = _caps(job)
    try:
        return await db_learnings.create_learning(
            job_id, body, summary,
            source="human",
            max_count=max_count,
            max_body_chars=max_body_chars,
        )
    except LearningCapacityError as e:
        raise HTTPException(409, str(e))
    except LearningSizeError as e:
        raise HTTPException(413, str(e))


@router.patch("/api/learnings/{learning_id}")
async def update_job_learning(learning_id: int, req: UpdateLearningRequest):
    require_project()
    if req.summary is None and req.body is None and req.enabled is None:
        raise HTTPException(400, "No updates provided")
    summary = req.summary.strip() if req.summary is not None else None
    body = req.body.strip() if req.body is not None else None
    if summary is not None and not summary:
        raise HTTPException(400, "summary cannot be empty")
    if body is not None and not body:
        raise HTTPException(400, "body cannot be empty")

    existing = await db_learnings.get_learning(learning_id)
    if not existing:
        raise HTTPException(404, "Learning not found")
    job = await db.get_job(existing["job_id"])
    if not job:
        raise HTTPException(404, "Job not found")
    _, max_body_chars = _caps(job)

    try:
        learning = await db_learnings.update_learning(
            learning_id,
            summary=summary,
            body=body,
            enabled=req.enabled,
            max_body_chars=max_body_chars,
        )
    except LearningSizeError as e:
        raise HTTPException(413, str(e))
    if not learning:
        raise HTTPException(404, "Learning not found")
    return learning


@router.delete("/api/learnings/{learning_id}")
async def delete_job_learning(learning_id: int):
    require_project()
    ok = await db_learnings.delete_learning(learning_id)
    if not ok:
        raise HTTPException(404, "Learning not found")
    return {"status": "deleted"}


@router.post("/api/jobs/{job_id}/learnings/reorder")
async def reorder_job_learnings(job_id: int, req: ReorderLearningsRequest):
    require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    await db_learnings.reorder_learnings(job_id, req.learning_ids)
    return {"status": "ok"}


# ── Agent read/write surface ───────────────────────────────
# Called by the internal MCP server (mcp_server.py). Read routes
# (``/agent/list``, ``/agent/read``) are always accessible to the agent.
# Write routes stamp ``source='agent'`` and only mutate rows the agent
# itself authored; they're gated upstream by the
# ``allow_learning_self_modification`` job property.

class AgentAddLearningRequest(BaseModel):
    job_id: int
    summary: str
    body: str


class AgentUpdateLearningRequest(BaseModel):
    summary: str | None = None
    body: str | None = None


@router.get("/api/jobs/{job_id}/learnings/summaries")
async def agent_list_summaries(job_id: int):
    """Breadth view: id + summary for enabled rows. Backs `list_learnings`
    in the internal MCP server."""
    require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return await db_learnings.list_learning_summaries(job_id)


@router.post("/api/learnings/read")
async def agent_read_learnings(req: ReadLearningsRequest):
    """Depth view: full bodies for the requested ids, scoped to one job.
    Backs `read_learnings` in the internal MCP server."""
    require_project()
    job = await db.get_job(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return await db_learnings.get_learnings_by_ids(req.job_id, req.ids)


@router.post("/api/learnings/agent-add")
async def agent_add_learning(req: AgentAddLearningRequest):
    require_project()
    summary = (req.summary or "").strip()
    body = (req.body or "").strip()
    if not summary:
        raise HTTPException(400, "summary is required")
    if not body:
        raise HTTPException(400, "body is required")
    job = await db.get_job(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    max_count, max_body_chars = _caps(job)
    try:
        return await db_learnings.create_learning(
            req.job_id, body, summary,
            source="agent",
            max_count=max_count,
            max_body_chars=max_body_chars,
        )
    except LearningCapacityError as e:
        raise HTTPException(409, str(e))
    except LearningSizeError as e:
        raise HTTPException(413, str(e))


@router.patch("/api/learnings/{learning_id}/agent-update")
async def agent_update_learning(learning_id: int, req: AgentUpdateLearningRequest):
    require_project()
    if req.summary is None and req.body is None:
        raise HTTPException(400, "No updates provided")
    summary = req.summary.strip() if req.summary is not None else None
    body = req.body.strip() if req.body is not None else None
    if summary is not None and not summary:
        raise HTTPException(400, "summary cannot be empty")
    if body is not None and not body:
        raise HTTPException(400, "body cannot be empty")

    existing = await db_learnings.get_learning(learning_id)
    if not existing:
        raise HTTPException(404, "Learning not found, or not agent-authored")
    job = await db.get_job(existing["job_id"])
    if not job:
        raise HTTPException(404, "Job not found")
    _, max_body_chars = _caps(job)

    try:
        learning = await db_learnings.update_learning(
            learning_id,
            summary=summary,
            body=body,
            require_source="agent",
            max_body_chars=max_body_chars,
        )
    except LearningSizeError as e:
        raise HTTPException(413, str(e))
    if not learning:
        raise HTTPException(404, "Learning not found, or not agent-authored")
    return learning


@router.delete("/api/learnings/{learning_id}/agent-delete")
async def agent_delete_learning(learning_id: int):
    require_project()
    ok = await db_learnings.delete_learning(learning_id, require_source="agent")
    if not ok:
        raise HTTPException(404, "Learning not found, or not agent-authored")
    return {"status": "deleted"}
