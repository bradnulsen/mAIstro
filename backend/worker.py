"""Background queue worker — processes dispatch_queue records one at a time.

The worker is the only code path that invokes run_dispatch.
All feeders (manual, watch, timer) just create queue records.
"""

import asyncio
import logging

from backend import database as db, git, state
from backend.dispatch import run_dispatch
from backend.state import utcnow

log = logging.getLogger("maistro.worker")

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event = asyncio.Event()
_lock: asyncio.Lock = asyncio.Lock()
_active_dispatch_id: int | None = None
_cancel_event: asyncio.Event | None = None

# ── Live event broadcast ───────────────────────────────────
# Subscribers keyed by dispatch_id → set of asyncio.Queue.
# The worker pushes every non-_raw event to all subscriber queues.
# SSE endpoint creates a queue, adds it here, removes on disconnect.
_subscribers: dict[int, set[asyncio.Queue]] = {}


def subscribe(dispatch_id: int) -> asyncio.Queue:
    """Subscribe to live events for a dispatch. Returns a queue to read from."""
    q = asyncio.Queue()
    _subscribers.setdefault(dispatch_id, set()).add(q)
    log.debug("[worker] Subscriber added for dispatch #%d (total=%d)", dispatch_id, len(_subscribers[dispatch_id]))
    return q


def unsubscribe(dispatch_id: int, q: asyncio.Queue):
    """Remove a subscriber queue."""
    subs = _subscribers.get(dispatch_id)
    if subs:
        subs.discard(q)
        if not subs:
            del _subscribers[dispatch_id]


def _broadcast(dispatch_id: int, event: dict):
    """Push an event to all subscribers of a dispatch."""
    subs = _subscribers.get(dispatch_id)
    if not subs:
        return
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop if subscriber is slow


def get_active_dispatch_id() -> int | None:
    """Return the dispatch ID currently being processed, or None."""
    return _active_dispatch_id


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
            await _process_dispatch(dispatch, dispatch_method="manual")
        return dispatch


async def process_one(dispatch_id: int) -> dict | None:
    """Manually process a specific pending dispatch by ID."""
    async with _lock:
        dispatch = await db.get_dispatch(dispatch_id)
        if not dispatch:
            return None
        # Must still be pending (not started, no error)
        if dispatch.get("started_at") or dispatch.get("error"):
            return None
        await _process_dispatch(dispatch, dispatch_method="manual")
        return dispatch


async def process_all() -> list[int]:
    """Manually process all pending dispatches sequentially."""
    processed = []
    async with _lock:
        while True:
            dispatch = await db.get_oldest_pending_dispatch()
            if not dispatch:
                break
            await _process_dispatch(dispatch, dispatch_method="manual")
            processed.append(dispatch["id"])
    return processed


# ── Internal ────────────────────────────────────────────────

async def _sweep_stale():
    """On startup, mark any in-flight dispatches as interrupted."""
    if not db.DB_PATH:
        return
    stale_ids = await db.sweep_stale_dispatches(utcnow())
    for did in stale_ids:
        log.warning("[worker] Marked stale dispatch #%d as interrupted", did)


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
            if not state.PROJECT_DIR:
                continue

            # Sweep stale dispatches once per project open (re-sweeps on project switch)
            if _swept_project != state.PROJECT_DIR:
                await _sweep_stale()
                _swept_project = state.PROJECT_DIR

            # Check auto_dispatch setting
            auto = await db.get_config("queue_auto_dispatch")
            if auto != "true":
                continue

            # Process one at a time
            async with _lock:
                dispatch = await db.get_oldest_pending_dispatch()
                if dispatch:
                    await _process_dispatch(dispatch, dispatch_method="auto")

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[worker] Error in loop")
            await asyncio.sleep(5)


async def _process_dispatch(dispatch: dict, dispatch_method: str = "manual"):
    """Execute a single dispatch — create session, run CLI, store output."""
    dispatch_id = dispatch["id"]
    task_id = dispatch["task_id"]

    if not state.PROJECT_DIR:
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="no project open")
        return

    task = await db.get_task(task_id)
    if not task:
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error=f"task '{task_id}' not found")
        return

    # For resume dispatches, reuse the original chat session; otherwise create a new one
    resume_session_id = dispatch.get("resume_session_id")
    if resume_session_id:
        existing_session = await db.find_session_by_cli_session(resume_session_id)
        if existing_session:
            session_id = existing_session
            log.info("[worker] Resuming into existing chat session %s", session_id)
        else:
            session = await db.create_chat_session(
                task_id=task_id, dispatch_id=dispatch_id,
                title=f"{task['name']} #{dispatch_id} (resume)",
            )
            session_id = session["id"]
    else:
        session = await db.create_chat_session(
            task_id=task_id, dispatch_id=dispatch_id,
            title=f"{task['name']} #{dispatch_id}",
        )
        session_id = session["id"]

    # Mark dispatch as started, set up cancellation
    global _active_dispatch_id, _cancel_event
    _cancel_event = asyncio.Event()
    _active_dispatch_id = dispatch_id
    start_commit = git.head_hash(state.PROJECT_DIR)
    await db.update_dispatch(dispatch_id, started_at=utcnow(), session_id=session_id,
                             dispatch_method=dispatch_method, start_commit=start_commit)

    log.info("[worker] Processing dispatch #%d (task=%s)", dispatch_id, task_id)

    # Timeout enforcement — fire cancel_event after task's timeout
    timeout_seconds = task["properties"].get("timeout", 900)
    _timed_out = False
    local_cancel = _cancel_event  # close over local ref, not the mutable global

    async def _timeout_watchdog():
        nonlocal _timed_out
        await asyncio.sleep(timeout_seconds)
        if not local_cancel.is_set():
            _timed_out = True
            log.warning("[worker] Dispatch #%d timed out after %ds", dispatch_id, timeout_seconds)
            local_cancel.set()

    watchdog = asyncio.create_task(_timeout_watchdog()) if timeout_seconds > 0 else None

    full_response = []
    streaming_text = []

    try:
        async for event in run_dispatch(dispatch_id, task, state.PROJECT_DIR,
                                        cancel_event=_cancel_event, dispatch_method=dispatch_method):
            etype = event.get("type")

            # Raw audit trail — store every NDJSON event
            if etype == "_raw":
                await db.add_chat_event(session_id, event["event_type"], event["raw_json"])
                continue

            # Broadcast to live subscribers (skip _raw, already handled above)
            _broadcast(dispatch_id, event)

            # Authoritative full response — use for DB storage
            if etype == "assistant_complete":
                full_response.append(event.get("content", ""))
                continue

            if etype == "session_id":
                # Capture CLI session ID for future resume
                cli_sid = event.get("cli_session_id")
                if cli_sid:
                    await db.update_chat_session(session_id, cli_session_id=cli_sid)
            elif etype == "text":
                streaming_text.append(event.get("content", ""))
            elif etype == "error":
                await db.add_chat_message(session_id, "system", event.get("message", "error"))

        # Save one clean assistant message — prefer assistant_complete, fall back to streamed text
        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)

        if _timed_out:
            head = git.head_hash(state.PROJECT_DIR)
            await db.update_dispatch(dispatch_id, completed_at=utcnow(),
                                     result_commit=head, error="timed out")
            log.info("[worker] Dispatch #%d timed out (partial commit=%s)", dispatch_id, head[:8])
        else:
            head = git.head_hash(state.PROJECT_DIR)
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
        # Cancel the watchdog if still waiting
        if watchdog and not watchdog.done():
            watchdog.cancel()
        # Signal completion to any live subscribers, then clean up
        _broadcast(dispatch_id, {"type": "_done"})
        _subscribers.pop(dispatch_id, None)
        _active_dispatch_id = None
        _cancel_event = None


