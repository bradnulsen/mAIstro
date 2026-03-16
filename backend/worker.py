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


async def process_one(dispatch_id: int) -> dict | None:
    """Manually process a specific pending dispatch by ID."""
    async with _lock:
        dispatch = await db.get_dispatch(dispatch_id)
        if not dispatch:
            return None
        # Must still be pending (not started, no error)
        if dispatch.get("started_at") or dispatch.get("error"):
            return None
        # Auto-approve if pending approval (manual processing = explicit intent)
        if dispatch.get("approval") == "pending":
            await db.approve_dispatch(dispatch_id)
        await _process_dispatch(dispatch)
        return dispatch



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

    if not state.PROJECT_DIR:
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error="no project open")
        return

    task = await db.get_task(task_id)
    if not task:
        await db.update_dispatch(dispatch_id, completed_at=utcnow(), error=f"task '{task_id}' not found")
        return

    # Collect subordinate tasks (merged dispatches) and unify triggers
    subordinates = await db.get_subordinate_dispatches(dispatch_id)
    if subordinates:
        all_triggers = list(dispatch.get("triggers") or [])
        for sub in subordinates:
            all_triggers.extend(sub.get("triggers") or [])
        dispatch["triggers"] = all_triggers

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
                             start_commit=start_commit)

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
    raw_event_buffer: list[tuple[str, str]] = []

    try:
        # Pass the dispatch dict (with session_id injected) so run_dispatch
        # doesn't need to re-fetch it from the database.
        dispatch_with_session = {**dispatch, "session_id": session_id}
        async for event in run_dispatch(dispatch_id, task, state.PROJECT_DIR,
                                        cancel_event=_cancel_event,
                                        dispatch=dispatch_with_session):
            etype = event.get("type")

            # Raw audit trail — buffer and flush in batch at end (avoids per-event commits)
            if etype == "_raw":
                raw_event_buffer.append((event["event_type"], event["raw_json"]))
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

        # Flush buffered audit events in one transaction
        await db.add_chat_events_batch(session_id, raw_event_buffer)

        # Save one clean assistant message — prefer assistant_complete, fall back to streamed text
        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)

        if _timed_out:
            # Timed-out dispatches do NOT trigger dependents — timeout means the
            # task didn't complete successfully, so downstream tasks shouldn't run
            head = git.head_hash(state.PROJECT_DIR)
            now = utcnow()
            await db.update_dispatch(dispatch_id, completed_at=now,
                                     result_commit=head, error="timed out")
            for sub in subordinates:
                await db.update_dispatch(sub["id"], completed_at=now,
                                         result_commit=head, error="timed out")
            log.info("[worker] Dispatch #%d timed out (partial commit=%s, dependents skipped)", dispatch_id, head[:8])
        else:
            head = git.head_hash(state.PROJECT_DIR)
            now = utcnow()
            await db.update_dispatch(dispatch_id, completed_at=now, result_commit=head)
            for sub in subordinates:
                await db.update_dispatch(sub["id"], completed_at=now, result_commit=head)
            log.info("[worker] Dispatch #%d completed (commit=%s)", dispatch_id, head[:8])

            # Trigger dependent tasks
            await _enqueue_dependents(task_id, dispatch_id,
                                      task_name=task["name"],
                                      start_commit=start_commit,
                                      result_commit=head)

    except Exception as e:
        log.exception("[worker] Dispatch #%d failed: %s", dispatch_id, e)
        # Flush any buffered audit events before recording the error
        try:
            await db.add_chat_events_batch(session_id, raw_event_buffer)
        except Exception:
            pass
        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)
        await db.add_chat_message(session_id, "system", f"Error: {e}")
        now = utcnow()
        await db.update_dispatch(dispatch_id, completed_at=now, error=str(e))
        for sub in subordinates:
            await db.update_dispatch(sub["id"], completed_at=now, error=str(e))
    finally:
        # Cancel the watchdog if still waiting
        if watchdog and not watchdog.done():
            watchdog.cancel()
        # Signal completion to any live subscribers, then clean up
        _broadcast(dispatch_id, {"type": "_done"})
        _subscribers.pop(dispatch_id, None)
        _active_dispatch_id = None
        _cancel_event = None


async def _enqueue_dependents(completed_task_id: str, dispatch_id: int,
                              task_name: str | None = None,
                              start_commit: str | None = None,
                              result_commit: str | None = None):
    """Enqueue tasks that declare a dependency on the completed task."""
    dependent_tasks = await db.get_tasks_depending_on(completed_task_id)
    if not dependent_tasks:
        return

    upstream_name = task_name or completed_task_id

    # Resolve commit summary and outcome once — shared across all dependents
    commit_context = ""
    if result_commit and state.PROJECT_DIR:
        summary = git.commit_oneline(state.PROJECT_DIR, result_commit) or result_commit[:8]
        commit_context = f"\n  Upstream result commit `{result_commit[:8]}`: {summary}"
        if start_commit and start_commit != result_commit:
            commit_context += f"\n  Upstream commit range: `{start_commit[:8]}..{result_commit[:8]}`"
            # Include outcome summary for downstream agents
            outcome = git.outcome_summary(state.PROJECT_DIR, start_commit, result_commit)
            if outcome:
                commit_context += f"\n  Outcome:\n{outcome}"

    for task in dependent_tasks:
        context = f"**Dependency** — triggered by completion of {upstream_name} (dispatch #{dispatch_id}){commit_context}"

        dep_id = await db.enqueue_dispatch(
            task["id"], "dependency",
            trigger_detail=completed_task_id,
            context=context,
        )
        log.info("[worker] Dependency trigger: enqueued #%d for '%s' (upstream: '%s' #%d)",
                 dep_id, task["id"], completed_task_id, dispatch_id)
        notify()


