"""Job CRUD routes — persistent configuration for mAistro jobs.

Handles job listing, creation, update, deletion, reordering,
and subscription resolution.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db, git
from backend import state
from backend.state import require_project

router = APIRouter(tags=["jobs"])


# ── Pydantic Models ────────────────────────────────────────

class CreateJobRequest(BaseModel):
    name: str
    properties: dict | None = None

class UpdateJobRequest(BaseModel):
    name: str | None = None
    summary: str | None = None
    description: str | None = None
    model: str | None = None
    allowed_tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    subscriptions: list[str] | None = None
    coalesce_tasks: bool | None = None
    schedule: str | None = None
    timeout: int | None = None
    max_turns: int | None = None
    cascades_from: list[int] | None = None
    require_approval: bool | None = None
    sort_order: int | None = None
    allowed_internal_tools: list[str] | None = None
    allowed_dispatch_targets: list[int] | None = None
    allow_learning_self_modification: bool | None = None
    allow_self_requeue: bool | None = None
    auto_continue: bool | None = None

class ReorderRequest(BaseModel):
    job_ids: list[int]


# ── Routes ─────────────────────────────────────────────────

@router.get("/api/jobs/")
async def list_jobs():
    require_project()
    return await db.list_jobs()


@router.post("/api/jobs/")
async def create_job(req: CreateJobRequest):
    require_project()
    return await db.create_job(req.name, req.properties or {})


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: int):
    require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.patch("/api/jobs/{job_id}")
async def update_job(job_id: int, req: UpdateJobRequest):
    require_project()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No updates provided")

    if "cascades_from" in updates:
        upstreams = updates["cascades_from"]
        if job_id in upstreams:
            raise HTTPException(400, "A job cannot cascade from itself")

    job = await db.update_job(job_id, updates)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int):
    require_project()
    ok = await db.delete_job(job_id)
    if not ok:
        raise HTTPException(404, "Job not found")
    return {"status": "deleted"}


@router.post("/api/jobs/reorder")
async def reorder_jobs(req: ReorderRequest):
    require_project()
    await db.reorder_jobs(req.job_ids)
    return {"status": "ok"}


@router.get("/api/jobs/{job_id}/subscriptions")
async def get_job_subscriptions(job_id: int):
    project_dir = require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    props = job["properties"]
    subs = git.resolve_glob_files(project_dir, props.get("subscriptions") or [])
    return {"subscriptions": subs}
