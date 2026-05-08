"""Governor routes — threads, messages, runs, status, debug.

The conversation surface is threads (open and closed) with append-only
messages. Approvals collapse into prose: a Governor message may carry a
structured ``action_payload``, but the only response affordance is the
thread's normal compose box. The next reply invocation reads the
conversation and decides whether to execute.

Close and reopen are human-only. Neither reaches any MCP write tool;
this route layer is the only path to thread-status changes.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db, governor
from backend.state import require_project

log = logging.getLogger("maistro.governor")

router = APIRouter(prefix="/api/governor", tags=["governor"])


# ── Request models ─────────────────────────────────────────


class CreateThreadRequest(BaseModel):
    title: str
    body: str


class ReplyRequest(BaseModel):
    body: str


# ── Threads ────────────────────────────────────────────────


@router.get("/threads")
async def list_threads(status: str | None = None, limit: int = 100):
    require_project()
    if status is not None and status not in ("open", "closed"):
        raise HTTPException(400, "status must be 'open' or 'closed'")
    return await db.list_threads(status=status, limit=limit)


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: int):
    require_project()
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    return thread


@router.post("/threads")
async def create_thread(req: CreateThreadRequest):
    """Human opens a thread. Auto-queues a reply invocation on the new
    thread so the Governor responds without waiting for the next survey."""
    require_project()
    title = (req.title or "").strip()
    body = (req.body or "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    if not body:
        raise HTTPException(400, "body is required")

    thread_id = await db.create_thread(title, opener="human")
    await db.add_message(thread_id, author="human", body=body)
    governor.enqueue_reply(thread_id)
    return {"thread_id": thread_id}


@router.post("/threads/{thread_id}/reply")
async def reply_to_thread(thread_id: int, req: ReplyRequest):
    """Human posts in an open thread. Spawns or coalesces into a reply
    invocation on this thread."""
    require_project()
    body = (req.body or "").strip()
    if not body:
        raise HTTPException(400, "body is required")
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(404, "Thread not found")
    if thread["status"] != "open":
        raise HTTPException(400, "Thread is closed; reopen it first")

    await db.add_message(thread_id, author="human", body=body)
    governor.enqueue_reply(thread_id)
    return {"status": "ok"}


@router.post("/threads/{thread_id}/close")
async def close_thread(thread_id: int):
    """Human-only. Mutes the thread from every future Governor invocation."""
    require_project()
    ok = await db.set_thread_status(thread_id, "closed")
    if not ok:
        raise HTTPException(404, "Thread not found, or already closed")
    return {"status": "closed"}


@router.post("/threads/{thread_id}/reopen")
async def reopen_thread(thread_id: int):
    """Human-only. Restores Governor visibility on the thread."""
    require_project()
    ok = await db.set_thread_status(thread_id, "open")
    if not ok:
        raise HTTPException(404, "Thread not found, or already open")
    return {"status": "open"}


@router.post("/threads/{thread_id}/mark-read")
async def mark_read(thread_id: int):
    require_project()
    await db.mark_thread_read(thread_id)
    return {"status": "ok"}


# ── Status / debug / runs ──────────────────────────────────


@router.get("/status")
async def governor_status():
    """Conversation-surface state: unread threads + pending proposals."""
    require_project()
    return await db.get_governor_status()


@router.get("/debug")
async def governor_debug():
    """Operator-debug state: counter, last_run, queue depth, running flag.

    Surfaces internals that are intentionally absent from /status — this
    backs the collapsed-by-default debug drawer in the Governor view.
    """
    require_project()
    db_state = await db.get_governor_debug()
    return {
        **db_state,
        "queue_depth": governor.queue_depth(),
        "running_lock": governor.is_running(),
    }


@router.get("/runs")
async def list_runs(limit: int = 20):
    require_project()
    return await db.get_governor_runs(limit=limit)


@router.get("/recent-tasks")
async def recent_tasks_for_governor(limit: int = 50):
    """Used by the Governor MCP server."""
    require_project()
    return await db.get_recent_tasks_for_governor(limit=limit)
