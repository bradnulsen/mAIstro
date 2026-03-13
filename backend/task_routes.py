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

    # Validate depends_on: no self-dependency, no cycles
    if "depends_on" in updates:
        new_deps = updates["depends_on"]
        if task_id in new_deps:
            raise HTTPException(400, "A task cannot depend on itself")
        cycle = await _detect_dependency_cycle(task_id, new_deps)
        if cycle:
            raise HTTPException(400, f"Circular dependency: {' → '.join(cycle)}")

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


# ── Helpers ────────────────────────────────────────────────

async def _detect_dependency_cycle(task_id: str, new_deps: list[str]) -> list[str] | None:
    """DFS cycle detection. Returns the cycle path if found, else None."""
    all_tasks = await db.list_tasks()
    dep_graph = {t["id"]: list(t["properties"].get("depends_on") or []) for t in all_tasks}
    dep_graph[task_id] = new_deps  # apply proposed change

    visited = set()
    path = []

    def dfs(node):
        if node in path:
            cycle_start = path.index(node)
            return path[cycle_start:] + [node]
        if node in visited:
            return None
        visited.add(node)
        path.append(node)
        for dep in dep_graph.get(node, []):
            result = dfs(dep)
            if result:
                return result
        path.pop()
        return None

    # Check from the task being updated — follow its new deps
    for dep in new_deps:
        result = dfs(dep)
        if result:
            # Prepend the task itself to show the full cycle
            return [task_id] + result
    return None
