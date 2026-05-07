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
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db, db_learnings
from backend.state import require_project

router = APIRouter(tags=["learnings"])


class CreateLearningRequest(BaseModel):
    body: str


class UpdateLearningRequest(BaseModel):
    body: str | None = None
    enabled: bool | None = None


class ReorderLearningsRequest(BaseModel):
    learning_ids: list[int]


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
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(400, "body is required")
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return await db_learnings.create_learning(job_id, body, source="human")


@router.patch("/api/learnings/{learning_id}")
async def update_job_learning(learning_id: int, req: UpdateLearningRequest):
    require_project()
    if req.body is None and req.enabled is None:
        raise HTTPException(400, "No updates provided")
    body = req.body.strip() if req.body is not None else None
    if body is not None and not body:
        raise HTTPException(400, "body cannot be empty")
    learning = await db_learnings.update_learning(
        learning_id, body=body, enabled=req.enabled,
    )
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


# ── Agent write surface ────────────────────────────────────
# Called by the internal MCP server (mcp_server.py) when the dispatching
# job has allow_learning_self_modification=true. All writes here stamp
# source='agent' and are constrained to rows the agent itself authored.

class AgentAddLearningRequest(BaseModel):
    job_id: int
    body: str


class AgentUpdateLearningRequest(BaseModel):
    body: str


@router.post("/api/learnings/agent-add")
async def agent_add_learning(req: AgentAddLearningRequest):
    require_project()
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(400, "body is required")
    job = await db.get_job(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return await db_learnings.create_learning(req.job_id, body, source="agent")


@router.patch("/api/learnings/{learning_id}/agent-update")
async def agent_update_learning(learning_id: int, req: AgentUpdateLearningRequest):
    require_project()
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(400, "body is required")
    learning = await db_learnings.update_learning(
        learning_id, body=body, require_source="agent",
    )
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
