"""Task CRUD routes — extracted from main.py.

Handles task listing, creation, update, deletion, reordering,
and subscription resolution.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db, git
from backend import state
from backend.state import require_project

router = APIRouter(tags=["tasks"])


# ── Pydantic Models ────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    name: str
    properties: dict | None = None

class UpdateTaskRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    base_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    subscriptions: list[str] | None = None
    coalesce_dispatches: bool | None = None
    schedule: str | None = None
    timeout: int | None = None
    depends_on: list[str] | None = None
    sort_order: int | None = None

class ReorderRequest(BaseModel):
    task_ids: list[str]


# ── Routes ─────────────────────────────────────────────────

@router.get("/api/tasks/")
async def list_tasks():
    require_project()
    return await db.list_tasks()


@router.post("/api/tasks/")
async def create_task(req: CreateTaskRequest):
    require_project()
    return await db.create_task(req.name, req.properties or {})


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, req: UpdateTaskRequest):
    require_project()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No updates provided")
    task = await db.update_task(task_id, updates)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    require_project()
    ok = await db.delete_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found")
    return {"status": "deleted"}


@router.post("/api/tasks/reorder")
async def reorder_tasks(req: ReorderRequest):
    require_project()
    await db.reorder_tasks(req.task_ids)
    return {"status": "ok"}


@router.get("/api/tasks/{task_id}/subscriptions")
async def get_task_subscriptions(task_id: str):
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    props = task["properties"]
    subs = git.resolve_glob_files(state.PROJECT_DIR, props.get("subscriptions") or [])
    return {"subscriptions": subs}
