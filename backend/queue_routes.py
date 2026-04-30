"""Task queue routes — atomic work item lifecycle and queue control.

Handles task enqueue, streaming, output, cancel, resume, reply,
editing, merge/split, and queue settings.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import events, pubsub

log = logging.getLogger("maistro.queue_routes")

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

class ReplyRequest(BaseModel):
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
    target_job_id: int
    message: str
    source_job_id: int
    source_task_id: int


# ── Task Routes ────────────────────────────────────────────

@router.get("/api/tasks/queue")
async def get_task_queue():
    require_project()
    return await db.get_task_queue()


@router.get("/api/queue/stream")
async def stream_queue_changes():
    """SSE stream of global queue-change notifications.

    Sends a 'queue_changed' event whenever any task transitions state.
    Clients should re-fetch the task queue on each notification.
    """
    q = pubsub.subscribe_queue()
    log.info("[sse] Queue stream connected")

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": event["type"], "data": "{}"}
        finally:
            pubsub.unsubscribe_queue(q)
            log.info("[sse] Queue stream disconnected")

    return EventSourceResponse(stream())


@router.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: int):
    """SSE stream of live events for a running task.

    A coalesced subordinate has no events of its own — only its root runs.
    Subscribe to the root's channel so the subordinate's stream URL still
    works (otherwise it would hang forever on an empty channel).
    """
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    if task.get("status") in db.TERMINAL_STATUSES:
        async def done_stream():
            yield {"event": "done", "data": json.dumps({"status": "completed"})}
        return EventSourceResponse(done_stream())

    effective_id = task.get("coalesced_id") or task_id
    q = pubsub.subscribe(effective_id)
    log.info("[sse] Task #%d stream connected (channel=#%d)", task_id, effective_id)

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue

                if event.get("type") == "done":
                    yield events.to_sse(event)
                    break
                yield events.to_sse(event)
        finally:
            pubsub.unsubscribe(effective_id, q)
            log.info("[sse] Task #%d stream disconnected", task_id)

    return EventSourceResponse(stream())


@router.get("/api/tasks/{task_id}/output")
async def get_task_output(task_id: int):
    """Get stored output for a task.

    Reads through tasks_resolved so a coalesced subordinate returns its
    root's chat output (otherwise subordinates would always return an
    empty messages list since they never opened their own session).
    """
    require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    session_id = task.get("session_id")
    if not session_id:
        return {"messages": [], "status": "pending", "task": task}
    messages = await db.get_chat_messages(session_id)
    # Fallback: if chat_messages has no assistant content, reconstruct from
    # the durable chat_events log.  chat_messages is a cache written once
    # after the CLI exits; chat_events is the incrementally-persisted source.
    if not any(m.get("role") == "assistant" for m in messages):
        reconstructed = await db.reconstruct_output_from_events(session_id)
        if reconstructed:
            messages = messages + reconstructed
    status = task.get("status", "pending")
    # Map internal statuses to simpler API-level status for output endpoint
    if status == "active":
        status = "running"
    elif status in ("failed", "cancelled", "interrupted", "timed_out", "rejected"):
        status = "completed"
    # Include external MCP servers that were (or would be) included in dispatch
    mcp_server_names = []
    try:
        job = await db.get_job(task["job_id"])
        if job:
            job_mcp = job["properties"].get("mcp_servers") or []
            if job_mcp:
                all_servers = await db.list_mcp_servers()
                server_map = {s["name"]: s for s in all_servers}
                mcp_server_names = [
                    {"name": n, "enabled": server_map[n].get("enabled", False)}
                    for n in job_mcp if n in server_map
                ]
    except Exception as e:
        log.warning("[queue] Task #%d failed to resolve MCP server context: %s", task_id, e)
    return {"messages": messages, "status": status, "task": task, "mcp_servers": mcp_server_names}


@router.get("/api/tasks/{task_id}/outcome")
async def get_task_outcome(task_id: int):
    """Get the outcome summary for a completed task (derived from git).

    Reads through tasks_resolved so a coalesced subordinate inherits its
    root's start_commit/result_commit range.
    """
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    start = task.get("start_commit")
    end = task.get("result_commit")
    if not start or not end or start == end:
        return {"summary": None}
    summary = git.outcome_summary(project_dir, start, end)
    return {"summary": summary}


@router.get("/api/tasks/{task_id}/diff")
async def get_task_diff(task_id: int):
    """Get the git diff for a completed task (start_commit..result_commit).

    Reads through tasks_resolved so a coalesced subordinate inherits its
    root's commit range.
    """
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    start = task.get("start_commit")
    end = task.get("result_commit")
    if not start or not end:
        return {"files": [], "insertions": 0, "deletions": 0, "diff": ""}
    if start == end:
        return {"files": [], "insertions": 0, "deletions": 0, "diff": ""}
    return git.diff_range(project_dir, start, end)


@router.post("/api/tasks/{task_id}/workspace/integrate")
async def integrate_task_workspace(task_id: int):
    """Manually retry the merge of a preserved task branch into main.

    Same logic as the worker's automatic integration on `completed`:
    transparently stashes operator-dirty state, merges --ff-only with
    --no-ff fallback, restores the stash, with operator-wins rollback
    on any conflict. On success, the worktree and branch are removed
    and the task's worktree_path/task_branch columns are cleared so
    the banner stops showing.

    The task's status is unchanged. The historical fact that the
    automatic integration failed is preserved in the event log; this
    is a follow-up action, not a replay of the original execution.
    Cascading dependencies do not re-fire.

    Useful when the original auto-integration failed because of a
    transient state the operator has since resolved (cleaned their
    dirty main, manually rebased the task branch, etc.).
    """
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    wt_path = task.get("worktree_path")
    branch = task.get("task_branch")
    if not wt_path or not branch:
        raise HTTPException(404, "No preserved workspace for this task")
    if task.get("status") not in db.NON_SUCCESS_TERMINAL_STATUSES:
        raise HTTPException(409, "Workspace can only be integrated for a non-success terminal task")

    ok, err = git.integrate_branch(
        project_dir, branch,
        stash_label=f"maistro pre-integrate task-{task_id} (manual)",
    )
    if not ok:
        raise HTTPException(409, err)

    git.worktree_remove(project_dir, wt_path, force=True)
    git.worktree_prune(project_dir)
    git.branch_delete(project_dir, branch, force=True)
    owner_id = task.get("effective_root_id") or task["id"]
    await db.update_task(owner_id, worktree_path=None, task_branch=None)
    pubsub.notify_queue_changed()
    return {"status": "integrated", "result_commit": git.head_hash(project_dir)}


@router.post("/api/tasks/{task_id}/workspace/discard")
async def discard_task_workspace(task_id: int):
    """Remove a non-success task's preserved worktree and delete its branch.

    Use when the operator has reviewed the agent's work in the workspace
    and decided to throw it away. The task row keeps `result_commit` (commits
    are still in the reflog and can be recovered via `git fsck --lost-found`
    until git-gc runs), so the audit trail of what the agent produced isn't
    erased — only the live working copy is.
    """
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    wt_path = task.get("worktree_path")
    branch = task.get("task_branch")
    if not wt_path:
        raise HTTPException(404, "No preserved workspace for this task")
    # Discard is only safe for non-success terminals. Active tasks have
    # their worktree as the CLI subprocess's cwd; pulling it out from
    # under a running agent corrupts mid-task. Completed tasks already
    # cleared the worktree on integration; rejected/pending/queued never
    # had one. The view fall-through can also surface the root's path on
    # a subordinate, so check the resolved status (the root's, for subs).
    if task.get("status") not in db.NON_SUCCESS_TERMINAL_STATUSES:
        raise HTTPException(409, "Workspace can only be discarded for a non-success terminal task")
    if wt_path:
        git.worktree_remove(project_dir, wt_path, force=True)
        git.worktree_prune(project_dir)
    if branch:
        git.branch_delete(project_dir, branch, force=True)
    owner_id = task.get("effective_root_id") or task["id"]
    await db.update_task(owner_id, worktree_path=None, task_branch=None)
    pubsub.notify_queue_changed()
    return {"status": "discarded"}


@router.get("/api/tasks/{task_id}/orphan-stash")
async def get_orphan_stash(task_id: int):
    """Show the diff for a task's stashed orphan changes (Phase 2 safety net)."""
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    sha = task.get("orphan_stash_ref")
    if not sha:
        return {"diff": None, "ref": None}
    return {"diff": git.stash_show(project_dir, sha), "ref": sha}


@router.post("/api/tasks/{task_id}/orphan-stash/restore")
async def restore_orphan_stash(task_id: int):
    """Re-apply the stashed orphan changes to the working tree.

    The stash entry stays in `git stash list` after apply — operator can
    drop it manually once they've integrated the changes. The DB column is
    cleared so the banner stops showing.
    """
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    sha = task.get("orphan_stash_ref")
    if not sha:
        raise HTTPException(404, "No orphan stash for this task")
    if not git.stash_apply(project_dir, sha):
        raise HTTPException(500, "git stash apply failed (conflicts? stash pruned?)")
    # Stash column lives on the executing row (the root for coalesced groups);
    # update there so the view's fall-through stops surfacing the ref.
    owner_id = task.get("effective_root_id") or task["id"]
    await db.update_task(owner_id, orphan_stash_ref=None)
    pubsub.notify_queue_changed()
    return {"status": "restored"}


@router.post("/api/tasks/{task_id}/orphan-stash/discard")
async def discard_orphan_stash(task_id: int):
    """Drop the stash entry. Underlying commit object remains in the reflog
    until git-gc — recoverable via `git fsck --lost-found` if needed."""
    project_dir = require_project()
    task = await db.get_task_resolved(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    sha = task.get("orphan_stash_ref")
    if not sha:
        raise HTTPException(404, "No orphan stash for this task")
    git.stash_drop(project_dir, sha)
    owner_id = task.get("effective_root_id") or task["id"]
    await db.update_task(owner_id, orphan_stash_ref=None)
    pubsub.notify_queue_changed()
    return {"status": "discarded"}


@router.patch("/api/tasks/{task_id}")
async def update_task_route(task_id: int, req: UpdateTaskRequest):
    """Edit a pending task's context (only before it starts running)."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.get("status") not in db.PRE_EXECUTION_STATUSES:
        raise HTTPException(409, "Cannot edit a task that has already started")

    if req.context is not None:
        await db.update_task(task_id, context=req.context)
        pubsub.notify_queue_changed()
    return {"status": "ok"}


@router.post("/api/tasks/cancel/{task_id}")
async def cancel_task(task_id: int):
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    current = task.get("status")
    if current in db.TERMINAL_STATUSES:
        return {"status": "already_terminal", "was_running": False}
    was_running = worker.cancel(task_id)
    if was_running:
        # Active task: the worker owns the transition — it will detect the
        # cancel event, terminate the CLI, and transition to "cancelled" itself.
        # Transitioning here too causes a race (double-transition → ValueError).
        pubsub.notify_queue_changed()
        return {"status": "cancelling", "was_running": True}
    # Pending or queued: not being processed by the worker, transition directly.
    await db.transition_task(task_id, "cancelled", error="cancelled")
    await db.cascade_completion(task_id, "cancelled", error="cancelled")
    pubsub.notify_queue_changed()
    return {"status": "cancelled", "was_running": False}


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: int):
    """Resume a failed/timed-out task using its CLI session ID."""
    require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.get("status") not in db.TERMINAL_STATUSES:
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
    await db.coalesce_under(task_id, new_id)
    worker.notify()
    return {"task_id": new_id, "resuming_from": task_id}


@router.post("/api/tasks/{task_id}/reply")
async def reply_task(task_id: int, req: ReplyRequest | None = None):
    """Reply to a resolved task — creates a follow-up task with user context."""
    project_dir = require_project()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.get("status") not in db.TERMINAL_STATUSES:
        raise HTTPException(409, "Task is not completed")

    user_notes = req.context if req and req.context else None

    new_id = await db.enqueue_task(
        task["job_id"], "reply",
        trigger_detail=str(task_id),
        context=build_trigger_context(
            "reply",
            project_dir=project_dir,
            original_task_id=task_id,
            start_commit=task.get("start_commit"),
            result_commit=task.get("result_commit"),
            user_context=user_notes,
        ),
    )
    await db.coalesce_under(task_id, new_id)
    worker.notify()
    return {"task_id": new_id, "replying_to": task_id}


@router.post("/api/tasks/merge")
async def merge_tasks_route(req: MergeRequest):
    """Merge pending same-job tasks into one logical unit."""
    require_project()
    try:
        root_id = await db.merge_tasks(req.task_ids)
        pubsub.notify_queue_changed()
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
        pubsub.notify_queue_changed()
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
        else:
            pubsub.notify_queue_changed()
        return {"status": "ok", "to_queued": req.to_queued}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/tasks/{task_id}/split")
async def split_task_route(task_id: int):
    """Split a merged task — make subordinates independent again."""
    require_project()
    try:
        split_ids = await db.split_task(task_id)
        pubsub.notify_queue_changed()
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
    pubsub.notify_queue_changed()
    return {"status": "rejected"}


@router.post("/api/queue/queue-all")
async def queue_all():
    """Transfer all pending tasks to queued."""
    require_project()
    count = await db.transfer_all_tasks(to_queued=True)
    if count > 0:
        worker.notify()
    return {"transferred": count}


@router.post("/api/queue/shelve-all")
async def shelve_all():
    """Transfer all queued (non-active) tasks back to pending."""
    require_project()
    count = await db.transfer_all_tasks(to_queued=False)
    pubsub.notify_queue_changed()
    return {"transferred": count}


@router.post("/api/queue/reorder")
async def reorder_tasks_route(req: ReorderTasksRequest):
    """Reorder pending tasks to control execution priority."""
    require_project()
    await db.reorder_tasks(req.task_ids)
    pubsub.notify_queue_changed()
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

    # Depth limiting: prevent runaway agent dispatch cascades
    depth = await db.get_agent_dispatch_depth(req.source_task_id)
    limit_str = await db.get_config("agent_dispatch_depth_limit")
    depth_limit = int(limit_str) if limit_str else 5
    if depth >= depth_limit:
        raise HTTPException(
            429,
            f"Agent dispatch depth limit reached ({depth}/{depth_limit}). "
            f"Chain from task #{req.source_task_id} is too deep."
        )

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


# ── Enqueue ────────────────────────────────────────────────

@router.post("/api/tasks/{job_id}")
async def enqueue_job(job_id: int, req: DispatchRequest | None = None):
    """Enqueue a task for a job. The worker processes it."""
    project_dir = require_project()
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    user_context = req.context if req else None
    head = git.head_hash(project_dir)
    context = build_trigger_context(
        "manual", commit_hash=head, user_context=user_context,
    )
    task_id = await db.enqueue_task(job_id, "manual", trigger_detail=head, context=context)
    log.info("[dispatch] Manual enqueue: job #%d → task #%d", job_id, task_id)
    worker.notify()
    return {"task_id": task_id}
