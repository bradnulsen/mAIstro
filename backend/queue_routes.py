"""Task queue routes — atomic work item lifecycle and queue control.

Handles task enqueue, streaming, output, cancel, resume, retry,
editing, merge/split, and queue settings.
"""

import asyncio
import json

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import database as db, git, worker
from backend import state
from backend.dispatch import build_trigger_context
from backend.state import utcnow, require_project

router = APIRouter(tags=["tasks", "queue"])


# ── Pydantic Models ────────────────────────────────────────

class DispatchRequest(BaseModel):
    context: str | None = None

class UpdateTaskRequest(BaseModel):
    context: str | None = None

class RetryRequest(BaseModel):
    context: str | None = None

class ReorderTasksRequest(BaseModel):
    task_ids: list[int]

class MergeRequest(BaseModel):
    task_ids: list[int]

class McpEventRequest(BaseModel):
    tool: str
    input: dict
    result: str
    timestamp: str

class TransferRequest(BaseModel):
    to_queued: bool

class QueueSettingsRequest(BaseModel):
    auto_dispatch: bool = False

class AgentDispatchRequest(BaseModel):
    target_job_id: str
    message: str
    source_job_id: str
    source_task_id: int


# ── Task Routes ────────────────────────────────────────────

@router.get("/api/tasks/queue")
async def get_task_queue():
    require_project()
    return await db.get_task_queue()


@router.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: int):
    """SSE stream of live events for a running task."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    if task.get("completed_at"):
        async def done_stream():
            yield {"event": "done", "data": json.dumps({"status": "completed"})}
        return EventSourceResponse(done_stream())

    q = worker.subscribe(task_id)

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue

                etype = event.get("type", "")
                if etype == "_done":
                    yield {"event": "done", "data": json.dumps({"status": "completed"})}
                    break
                elif etype == "text":
                    yield {"event": "text", "data": json.dumps({"content": event.get("content", "")})}
                elif etype == "thinking":
                    yield {"event": "thinking", "data": json.dumps({"content": event.get("content", "")})}
                elif etype == "tool_use":
                    yield {"event": "tool_use", "data": json.dumps({"tool": event.get("tool", ""), "input": event.get("input", {})})}
                elif etype == "error":
                    yield {"event": "error", "data": json.dumps(event)}
                elif etype == "session_id":
                    yield {"event": "session_id", "data": json.dumps(event)}
        finally:
            worker.unsubscribe(task_id, q)

    return EventSourceResponse(stream())


@router.get("/api/tasks/{task_id}/output")
async def get_task_output(task_id: int):
    """Get stored output for a task."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    session_id = task.get("session_id")
    if not session_id:
        return {"messages": [], "status": "pending", "task": task}
    messages = await db.get_chat_messages(session_id)
    status = "running" if task.get("started_at") and not task.get("completed_at") else \
             "completed" if task.get("completed_at") else "pending"
    return {"messages": messages, "status": status, "task": task}


@router.get("/api/tasks/{task_id}/outcome")
async def get_task_outcome(task_id: int):
    """Get the outcome summary for a completed task (derived from git)."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    start = task.get("start_commit")
    end = task.get("result_commit")
    if not start or not end or start == end:
        return {"summary": None}
    summary = git.outcome_summary(state.PROJECT_DIR, start, end)
    return {"summary": summary}


@router.get("/api/tasks/{task_id}/diff")
async def get_task_diff(task_id: int):
    """Get the git diff for a completed task (start_commit..result_commit)."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    start = task.get("start_commit")
    end = task.get("result_commit")
    if not start or not end:
        return {"files": [], "insertions": 0, "deletions": 0, "diff": ""}
    if start == end:
        return {"files": [], "insertions": 0, "deletions": 0, "diff": ""}
    return git.diff_range(state.PROJECT_DIR, start, end)


@router.patch("/api/tasks/{task_id}")
async def update_task_route(task_id: int, req: UpdateTaskRequest):
    """Edit a pending task's context (only before it starts running)."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.get("started_at"):
        raise HTTPException(409, "Cannot edit a task that has already started")

    if req.context is not None:
        await db.update_task(task_id, context=req.context)
    return {"status": "ok"}


@router.post("/api/tasks/cancel/{task_id}")
async def cancel_task(task_id: int):
    require_project()
    was_running = worker.cancel(task_id)
    now = utcnow()
    await db.update_task(task_id, _commit=False, completed_at=now, error="cancelled")
    subs = await db.get_subordinate_tasks(task_id)
    sub_ids = [s["id"] for s in subs if not s.get("completed_at")]
    await db.update_tasks_batch(sub_ids, completed_at=now, error="cancelled")
    return {"status": "cancelled", "was_running": was_running}


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: int):
    """Resume a failed/timed-out task using its CLI session ID."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.get("completed_at"):
        raise HTTPException(409, "Task is not completed")

    session_id = task.get("session_id")
    if not session_id:
        raise HTTPException(409, "No session found for this task")
    chat_session = await db.get_chat_session(session_id)
    cli_session_id = chat_session.get("cli_session_id") if chat_session else None
    if not cli_session_id:
        raise HTTPException(409, "No CLI session ID available — cannot resume")

    new_id = await db.enqueue_task(
        task["job_id"], "resume",
        trigger_detail=str(task_id),
        context=build_trigger_context("resume", original_task_id=task_id),
    )
    await db.update_task(new_id, resume_session_id=cli_session_id)
    await db.update_task(task_id, coalesced_id=new_id)
    worker.notify()
    return {"task_id": new_id, "resuming_from": task_id}


@router.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int, req: RetryRequest | None = None):
    """Retry a completed task — creates a new task preserving original trigger info."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.get("completed_at"):
        raise HTTPException(409, "Task is not completed")

    user_notes = req.context if req and req.context else None

    new_id = await db.enqueue_task(
        task["job_id"], "retry",
        trigger_detail=str(task_id),
        context=build_trigger_context(
            "retry",
            project_dir=state.PROJECT_DIR,
            original_task_id=task_id,
            start_commit=task.get("start_commit"),
            result_commit=task.get("result_commit"),
            user_context=user_notes,
        ),
    )
    await db.update_task(task_id, coalesced_id=new_id)
    worker.notify()
    return {"task_id": new_id, "retrying": task_id}


@router.post("/api/tasks/merge")
async def merge_tasks_route(req: MergeRequest):
    """Merge pending same-job tasks into one logical unit."""
    require_project()
    try:
        root_id = await db.merge_tasks(req.task_ids)
        return {"root_id": root_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/tasks/{task_id}/subordinates")
async def get_subordinates(task_id: int):
    """Get all subordinate tasks coalesced under this root."""
    require_project()
    subs = await db.get_subordinate_tasks(task_id)
    return subs


@router.post("/api/tasks/{task_id}/uncoalesce")
async def uncoalesce_task_route(task_id: int):
    """Remove a single subordinate from its coalesce group."""
    require_project()
    try:
        freed_id = await db.uncoalesce_task(task_id)
        return {"task_id": freed_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/tasks/{task_id}/transfer")
async def transfer_task_route(task_id: int, req: TransferRequest):
    """Move a task between pending and queued columns."""
    require_project()
    try:
        await db.transfer_task(task_id, req.to_queued)
        if req.to_queued:
            worker.notify()
        return {"status": "ok", "to_queued": req.to_queued}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/tasks/{task_id}/split")
async def split_task_route(task_id: int):
    """Split a merged task — make subordinates independent again."""
    require_project()
    try:
        split_ids = await db.split_task(task_id)
        return {"split_ids": split_ids}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Queue Control ──────────────────────────────────────────

@router.get("/api/queue/settings")
async def get_queue_settings():
    require_project()
    auto = await db.get_config("queue_auto_dispatch")
    return {"auto_dispatch": auto == "true"}


@router.post("/api/queue/settings")
async def set_queue_settings(req: QueueSettingsRequest):
    require_project()
    value = "true" if req.auto_dispatch else "false"
    await db.set_config("queue_auto_dispatch", value)
    worker.notify()
    return {"status": "ok"}


@router.post("/api/tasks/{task_id}/approve")
async def approve_task_route(task_id: int):
    """Approve a pending-approval task so the worker can process it."""
    require_project()
    ok = await db.approve_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found or not pending approval")
    worker.notify()
    return {"status": "approved"}


@router.post("/api/tasks/{task_id}/reject")
async def reject_task_route(task_id: int):
    """Reject a pending-approval task (marks as skipped)."""
    require_project()
    ok = await db.reject_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found or not pending approval")
    return {"status": "rejected"}


@router.post("/api/queue/reorder")
async def reorder_tasks_route(req: ReorderTasksRequest):
    """Reorder pending tasks to control execution priority."""
    require_project()
    await db.reorder_tasks(req.task_ids)
    return {"status": "ok"}


@router.post("/api/queue/process/{task_id}")
async def queue_process_one(task_id: int):
    """Manually process a specific pending task by ID."""
    require_project()
    task = await worker.process_one(task_id)
    if task is None:
        raise HTTPException(404, "Task not found or not pending")
    return {"processed": [task["id"]]}


@router.post("/api/tasks/mcp-event")
async def log_mcp_event(req: McpEventRequest, x_session_id: str = Header(None)):
    """Log an MCP tool invocation to the task's chat session audit trail."""
    if not x_session_id:
        return {"status": "ignored"}
    raw = json.dumps({
        "tool": req.tool,
        "input": req.input,
        "result": req.result,
        "timestamp": req.timestamp,
    })
    await db.add_chat_event(x_session_id, "mcp_tool_use", raw)
    return {"status": "ok"}


@router.post("/api/tasks/agent-dispatch")
async def agent_dispatch(req: AgentDispatchRequest):
    """Enqueue a task via agent dispatch (dispatch_task MCP tool).

    Validates allowed_dispatch_targets and prohibits self-dispatch.
    """
    require_project()
    target_job = await db.get_job(req.target_job_id)
    if not target_job:
        raise HTTPException(404, f"Target job '{req.target_job_id}' not found")

    source_job = await db.get_job(req.source_job_id)
    if not source_job:
        raise HTTPException(404, f"Source job '{req.source_job_id}' not found")

    if req.source_job_id == req.target_job_id:
        raise HTTPException(400, "Self-dispatch is prohibited")

    allowed_targets = source_job["properties"].get("allowed_dispatch_targets") or []
    if not allowed_targets:
        raise HTTPException(403, f"Job '{req.source_job_id}' has no allowed dispatch targets")
    if req.target_job_id not in allowed_targets:
        raise HTTPException(403, f"Job '{req.source_job_id}' is not allowed to dispatch '{req.target_job_id}'")

    context = build_trigger_context(
        "agent",
        upstream_name=source_job["name"],
        upstream_task_id=req.source_task_id,
        user_context=req.message,
    )
    task_id = await db.enqueue_task(
        req.target_job_id, "agent",
        trigger_detail=f"{req.source_job_id}#{req.source_task_id}",
        context=context,
    )
    worker.notify()
    return {"task_id": task_id}


# ── Enqueue (must be last — {job_id} is str and would match static paths) ──

@router.post("/api/tasks/{job_id}")
async def enqueue_job(job_id: str, req: DispatchRequest | None = None):
    """Enqueue a task for a job. The worker processes it."""
    require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    user_context = req.context if req else None
    head = git.head_hash(state.PROJECT_DIR)
    context = build_trigger_context(
        "manual", commit_hash=head, user_context=user_context,
    )
    task_id = await db.enqueue_task(job_id, "manual", trigger_detail=head, context=context)
    worker.notify()
    return {"task_id": task_id}
