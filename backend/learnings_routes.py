"""Per-job learnings — operator CRUD surface.

The UI in Tasks.jsx renders the list under each job's description; this
module backs add / edit / toggle / delete / reorder. All writes here are
``source='human'`` — agent-authored learnings flow through the internal
MCP tools (gated by the ``allow_learning_self_modification`` property in
a later step).
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
