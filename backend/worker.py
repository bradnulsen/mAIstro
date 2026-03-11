"""Background queue worker — processes dispatch_queue records one at a time.

The worker is the only code path that invokes run_dispatch.
All feeders (manual, watch, timer) just create queue records.
"""

import asyncio
import logging

from backend import database as db, git
from backend.dispatch import run_dispatch, utcnow

log = logging.getLogger("maistro.worker")

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event = asyncio.Event()
_lock: asyncio.Lock = asyncio.Lock()
_active_dispatch_id: int | None = None
_cancel_event: asyncio.Event | None = None


# ── Public API ──────────────────────────────────────────────

async def start():
    """Start the worker loop. Call from FastAPI lifespan."""
    global _worker_task
    _worker_task = asyncio.create_task(_loop())
    log.info("[worker] Started")


async def stop():
    """Stop the worker loop. Call from FastAPI lifespan."""
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    log.info("[worker] Stopped")


def notify():
    """Wake the worker loop immediately (call after enqueue or settings change)."""
    _wake_event.set()


def cancel(dispatch_id: int) -> bool:
    """Cancel the currently running dispatch. Returns True if it was active."""
    if _active_dispatch_id == dispatch_id and _cancel_event:
        _cancel_event.set()
        return True
    return False


async def process_next() -> dict | None:
    """Manually process the next pending dispatch, ignoring auto_dispatch setting."""
    async with _lock:
        dispatch = await db.get_oldest_pending_dispatch()
        if dispatch:
            await _process_dispatch(dispatch)
        return dispatch


async def process_all() -> list[int]:
    """Manually process all pending dispatches sequentially."""
    processed = []
    async with _lock:
        while True:
            dispatch = await db.get_oldest_pending_dispatch()
            if not dispatch:
                break
            await _process_dispatch(dispatch)
            processed.append(dispatch["id"])
    return processed


# ── Internal ────────────────────────────────────────────────

async def _sweep_stale():
    """On startup, mark any in-flight dispatches as interrupted."""
    if not db.DB_PATH:
        return
    d = await db.get_db()
    try:
        rows = await d.execute_fetchall(
            "SELECT id FROM dispatch_queue WHERE started_at IS NOT NULL AND completed_at IS NULL"
        )
        for row in rows:
            await db.update_dispatch(row["id"], completed_at=utcnow(), error="interrupted")
            log.warning("[worker] Marked stale dispatch #%d as interrupted", row["id"])
        await d.commit()
    finally:
        await d.close()


async def _loop():
    """Main worker loop — poll for pending dispatches."""
    _swept_project = None
    while True:
        try:
            # Wait for notification or poll every 2s
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            _wake_event.clear()

            # Need a project to be open
            from backend.main import PROJECT_DIR
            if not PROJECT_DIR:
                continue

            # Sweep stale dispatches once per project open (re-sweeps on project switch)
            if _swept_project != PROJECT_DIR:
                await _sweep_stale()
                _swept_project = PROJECT_DIR

            # Check auto_dispatch setting
            auto = await db.get_config("queue_auto_dispatch")
            if auto != "true":
                continue

            # Process one at a time
            async with _lock:
                dispatch = await db.get_oldest_pending_dispatch()
                if dispatch:
                    await _process_dispatch(dispatch)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[worker] Error in loop")
            await asyncio.sleep(5)


async def _process_dispatch(dispatch: dict):
    """Execute a single dispatch — create session, run CLI, store output."""
    dispatch_id = dispatch["id"]
    task_id = dispatch["task_id"]

    from backend.main import PROJECT_DIR
    if not PROJECT_DIR:
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="no project open")
        return

    task = await db.get_task(task_id)
    if not task:
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error=f"task '{task_id}' not found")
        return

    # Coalesce: grab all pending dispatches for same task and merge context
    coalesced_ids = []
    context = dispatch.get("context")
    if task["properties"].get("coalesce_dispatches"):
        siblings = await db.get_pending_dispatches_for_task(task_id)
        # siblings includes the current dispatch (it's still pending); skip it
        extras = [s for s in siblings if s["id"] != dispatch_id]
        if extras:
            coalesced_ids = [s["id"] for s in extras]
            context = _merge_contexts([dispatch] + extras)
            log.info("[worker] Coalescing %d dispatches for task %s: %s",
                     len(extras), task_id, coalesced_ids)

    # Create a chat session for this dispatch's output
    session = await db.create_chat_session(
        task_id=task_id,
        dispatch_id=dispatch_id,
        title=f"{task['name']} #{dispatch_id}",
    )
    session_id = session["id"]

    # Mark dispatch as started, set up cancellation
    global _active_dispatch_id, _cancel_event
    _cancel_event = asyncio.Event()
    _active_dispatch_id = dispatch_id
    await db.update_dispatch(dispatch_id, started_at=utcnow(), session_id=session_id)

    # Mark coalesced siblings as completed (absorbed into this dispatch)
    now = utcnow()
    for cid in coalesced_ids:
        await db.update_dispatch(cid, started_at=now, completed_at=now,
                                 session_id=session_id, error=f"coalesced into #{dispatch_id}")

    log.info("[worker] Processing dispatch #%d (task=%s)", dispatch_id, task_id)

    full_response = []
    streaming_text = []

    try:
        async for event in run_dispatch(dispatch_id, task, PROJECT_DIR, context, cancel_event=_cancel_event):
            etype = event.get("type")

            # Raw audit trail — store every NDJSON event
            if etype == "_raw":
                await db.add_chat_event(session_id, event["event_type"], event["raw_json"])
                continue

            # Authoritative full response — use for DB storage
            if etype == "assistant_complete":
                full_response.append(event.get("content", ""))
                continue

            if etype == "text":
                streaming_text.append(event.get("content", ""))
            elif etype == "error":
                await db.add_chat_message(session_id, "system", event.get("message", "error"))

        # Save one clean assistant message — prefer assistant_complete, fall back to streamed text
        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)

        head = git.head_hash(PROJECT_DIR)
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), result_commit=head)
        log.info("[worker] Dispatch #%d completed (commit=%s)", dispatch_id, head[:8])

    except Exception as e:
        log.exception("[worker] Dispatch #%d failed: %s", dispatch_id, e)
        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)
        await db.add_chat_message(session_id, "system", f"Error: {e}")
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error=str(e))
    finally:
        _active_dispatch_id = None
        _cancel_event = None


def _merge_contexts(dispatches: list[dict]) -> str:
    """Merge context from multiple dispatches into a single prompt section."""
    parts = []
    for d in dispatches:
        trigger = d.get("trigger", "unknown")
        detail = d.get("trigger_detail", "")
        ctx = d.get("context", "")
        label = f"Dispatch #{d['id']} ({trigger}"
        if detail:
            label += f": {detail}"
        label += ")"
        body = ctx or "(no context)"
        parts.append(f"### {label}\n{body}")
    return "\n\n".join(parts)
