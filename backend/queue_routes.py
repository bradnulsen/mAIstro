"""Dispatch and queue routes — extracted from main.py.

Handles dispatch lifecycle (enqueue, stream, output, cancel, resume, retry),
dispatch editing, and queue control (settings, process).
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import database as db, git, worker
from backend import state
from backend.state import utcnow, require_project

router = APIRouter(tags=["dispatch", "queue"])


# ── Pydantic Models ────────────────────────────────────────

class DispatchRequest(BaseModel):
    context: str | None = None

class UpdateDispatchRequest(BaseModel):
    context: str | None = None

class RetryRequest(BaseModel):
    context: str | None = None

class QueueSettingsRequest(BaseModel):
    auto_dispatch: bool = False


# ── Dispatch Routes ─────────────────────────────────────────

@router.post("/api/dispatch/{task_id}")
async def dispatch_task(task_id: str, req: DispatchRequest | None = None):
    """Enqueue a dispatch. The worker processes it."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["properties"].get("running"):
        raise HTTPException(409, "Task is already running")

    context = req.context if req else None
    head = git.head_hash(state.PROJECT_DIR)
    dispatch_id = await db.enqueue_dispatch(task_id, "manual", trigger_detail=head, context=context)
    worker.notify()
    return {"dispatch_id": dispatch_id}


@router.get("/api/dispatch/queue")
async def get_dispatch_queue():
    require_project()
    return await db.get_dispatch_queue()


@router.get("/api/dispatch/{dispatch_id}/stream")
async def stream_dispatch(dispatch_id: int):
    """SSE stream of live events for a running dispatch."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")

    # If already completed, return immediately with done
    if dispatch.get("completed_at"):
        async def done_stream():
            yield {"event": "done", "data": json.dumps({"status": "completed"})}
        return EventSourceResponse(done_stream())

    # Subscribe to live events from the worker
    q = worker.subscribe(dispatch_id)

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
            worker.unsubscribe(dispatch_id, q)

    return EventSourceResponse(stream())


@router.get("/api/dispatch/{dispatch_id}/output")
async def get_dispatch_output(dispatch_id: int):
    """Get stored output for a dispatch."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    session_id = dispatch.get("session_id")
    if not session_id:
        return {"messages": [], "status": "pending", "dispatch": dispatch}
    messages = await db.get_chat_messages(session_id)
    status = "running" if dispatch.get("started_at") and not dispatch.get("completed_at") else \
             "completed" if dispatch.get("completed_at") else "pending"
    return {"messages": messages, "status": status, "dispatch": dispatch}


@router.get("/api/dispatch/{dispatch_id}/diff")
async def get_dispatch_diff(dispatch_id: int):
    """Get the git diff for a completed dispatch (start_commit..result_commit)."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    start = dispatch.get("start_commit")
    end = dispatch.get("result_commit")
    if not start or not end:
        return {"files": [], "insertions": 0, "deletions": 0, "diff": ""}
    if start == end:
        return {"files": [], "insertions": 0, "deletions": 0, "diff": ""}
    return git.diff_range(state.PROJECT_DIR, start, end)


@router.patch("/api/dispatch/{dispatch_id}")
async def update_dispatch_route(dispatch_id: int, req: UpdateDispatchRequest):
    """Edit a pending dispatch (only before it starts running)."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    if dispatch.get("started_at"):
        raise HTTPException(409, "Cannot edit a dispatch that has already started")

    updates = {}
    if req.context is not None:
        updates["context"] = req.context
        # Also update the triggers JSON array
        triggers = dispatch.get("triggers") or []
        if triggers:
            triggers[-1]["context"] = req.context
        else:
            triggers = [{"trigger": dispatch["trigger"], "detail": dispatch.get("trigger_detail"), "context": req.context}]
        updates["triggers"] = json.dumps(triggers)

    if updates:
        await db.update_dispatch(dispatch_id, **updates)
    return {"status": "ok"}


@router.post("/api/dispatch/cancel/{dispatch_id}")
async def cancel_dispatch(dispatch_id: int):
    require_project()
    # Kill the running process if this dispatch is active
    was_running = worker.cancel(dispatch_id)
    # Mark as cancelled in DB (worker will also mark it, but this covers pending dispatches)
    await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="cancelled")
    return {"status": "cancelled", "was_running": was_running}


@router.post("/api/dispatch/{dispatch_id}/resume")
async def resume_dispatch(dispatch_id: int):
    """Resume a failed/timed-out dispatch using its CLI session ID."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    if not dispatch.get("completed_at"):
        raise HTTPException(409, "Dispatch is not completed")

    # Find the CLI session ID from the chat session
    session_id = dispatch.get("session_id")
    if not session_id:
        raise HTTPException(409, "No session found for this dispatch")
    chat_session = await db.get_chat_session(session_id)
    cli_session_id = chat_session.get("cli_session_id") if chat_session else None
    if not cli_session_id:
        raise HTTPException(409, "No CLI session ID available — cannot resume")

    # Enqueue a new dispatch with resume_session_id
    head = git.head_hash(state.PROJECT_DIR)
    new_id = await db.enqueue_dispatch(
        dispatch["task_id"], "resume",
        trigger_detail=head,
        context=f"Resuming dispatch #{dispatch_id}",
    )
    await db.update_dispatch(new_id, resume_session_id=cli_session_id)
    worker.notify()
    return {"dispatch_id": new_id, "resuming_from": dispatch_id}


@router.post("/api/dispatch/{dispatch_id}/retry")
async def retry_dispatch(dispatch_id: int, req: RetryRequest | None = None):
    """Retry a completed dispatch from scratch, optionally with new context."""
    require_project()
    dispatch = await db.get_dispatch(dispatch_id)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    if not dispatch.get("completed_at"):
        raise HTTPException(409, "Dispatch is not completed")

    # Use provided context override, or fall back to original
    context = (req.context if req and req.context is not None else None)
    if context is None:
        context = dispatch.get("context") or f"Retrying dispatch #{dispatch_id}"
    head = git.head_hash(state.PROJECT_DIR)
    new_id = await db.enqueue_dispatch(
        dispatch["task_id"], "retry",
        trigger_detail=head,
        context=context,
    )
    worker.notify()
    return {"dispatch_id": new_id, "retrying_from": dispatch_id}


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


@router.post("/api/queue/process/{dispatch_id}")
async def queue_process_one(dispatch_id: int):
    """Manually process a specific pending dispatch by ID."""
    require_project()
    dispatch = await worker.process_one(dispatch_id)
    if dispatch is None:
        raise HTTPException(404, "Dispatch not found or not pending")
    return {"processed": [dispatch["id"]]}


@router.post("/api/queue/process")
async def queue_process(all: bool = False):    # noqa: A002 — matches frontend query param
    """Manually crank the queue — process next or all pending dispatches."""
    require_project()
    if all:
        processed = await worker.process_all()
        return {"processed": processed}
    else:
        dispatch = await worker.process_next()
        return {"processed": [dispatch["id"]] if dispatch else []}
