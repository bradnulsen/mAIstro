"""Background queue worker — processes tasks one at a time.

The worker is the only code path that invokes run_task.
All feeders (manual, watch, timer) just create task records.
"""

import asyncio
import logging

from backend import database as db, git, state
from backend.dispatch import run_task, build_trigger_context
from backend.state import utcnow

log = logging.getLogger("maistro.worker")

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event = asyncio.Event()
_lock: asyncio.Lock = asyncio.Lock()
_active_task_id: int | None = None
_cancel_event: asyncio.Event | None = None

# ── Live event broadcast ───────────────────────────────────
# Subscribers keyed by task_id → set of asyncio.Queue.
_subscribers: dict[int, set[asyncio.Queue]] = {}


def subscribe(task_id: int) -> asyncio.Queue:
    """Subscribe to live events for a task. Returns a queue to read from."""
    q = asyncio.Queue()
    _subscribers.setdefault(task_id, set()).add(q)
    log.debug("[worker] Subscriber added for task #%d (total=%d)", task_id, len(_subscribers[task_id]))
    return q


def unsubscribe(task_id: int, q: asyncio.Queue):
    """Remove a subscriber queue."""
    subs = _subscribers.get(task_id)
    if subs:
        subs.discard(q)
        if not subs:
            del _subscribers[task_id]


def _broadcast(task_id: int, event: dict):
    """Push an event to all subscribers of a task."""
    subs = _subscribers.get(task_id)
    if not subs:
        return
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def get_active_task_id() -> int | None:
    """Return the task ID currently being processed, or None."""
    return _active_task_id


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


def cancel(task_id: int) -> bool:
    """Cancel the currently running task. Returns True if it was active."""
    if _active_task_id == task_id and _cancel_event:
        _cancel_event.set()
        return True
    return False


async def process_one(task_id: int) -> dict | None:
    """Manually process a specific pending task by ID."""
    async with _lock:
        task = await db.get_task(task_id)
        if not task:
            return None
        if task.get("status") not in ("pending", "queued"):
            return None
        if task.get("approval") == "pending":
            await db.approve_task(task_id)
        # Ensure task is queued before processing (transition validates legality)
        if task.get("status") == "pending":
            await db.transition_task(task_id, "queued")
        await _process_task(task)
        return task



# ── Internal ────────────────────────────────────────────────

async def _sweep_stale():
    """On startup, mark any in-flight tasks as interrupted."""
    if not db.DB_PATH:
        return
    stale_ids = await db.sweep_stale_tasks(utcnow())
    for tid in stale_ids:
        log.warning("[worker] Marked stale task #%d as interrupted", tid)


async def _loop():
    """Main worker loop — poll for pending tasks."""
    _swept_project = None
    while True:
        try:
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            _wake_event.clear()

            if not state.PROJECT_DIR:
                continue

            if _swept_project != state.PROJECT_DIR:
                await _sweep_stale()
                _swept_project = state.PROJECT_DIR

            # Guard the queue poll so close_db() waits for us.
            # Task execution itself is protected by the active-task check
            # at the HTTP layer — this covers the poll gap.
            async with db.db_read_guard():
                async with _lock:
                    task = await db.get_oldest_queued_task()
                    if task:
                        await _process_task(task)

        except asyncio.CancelledError:
            raise
        except RuntimeError as e:
            if "closing" in str(e).lower():
                log.info("[worker] Skipping poll — project switch in progress")
            else:
                log.exception("[worker] Error in loop")
                await asyncio.sleep(5)
        except Exception:
            log.exception("[worker] Error in loop")
            await asyncio.sleep(5)


async def _process_task(task: dict):
    """Execute a single task — create session, run CLI, store output."""
    task_id = task["id"]
    job_id = task["job_id"]

    if not state.PROJECT_DIR:
        try:
            await db.transition_task(task_id, "active")
        except ValueError:
            pass
        await db.transition_task(task_id, "failed", error="no project open")
        return

    job = await db.get_job(job_id)
    if not job:
        try:
            await db.transition_task(task_id, "active")
        except ValueError:
            pass
        await db.transition_task(task_id, "failed", error=f"job '{job_id}' not found")
        return

    # Collect subordinate tasks (coalesced) and pass to queue context builder
    subordinates = await db.get_subordinate_tasks(task_id)

    # For resume tasks, reuse the original chat session; otherwise create a new one
    resume_session_id = task.get("resume_session_id")
    if resume_session_id:
        existing_session = await db.find_session_by_cli_session(resume_session_id)
        if existing_session:
            session_id = existing_session
            log.info("[worker] Resuming into existing chat session %s", session_id)
        else:
            session = await db.create_chat_session(
                job_id=job_id, task_id=task_id,
                title=f"{job['name']} #{task_id} (resume)",
            )
            session_id = session["id"]
    else:
        session = await db.create_chat_session(
            job_id=job_id, task_id=task_id,
            title=f"{job['name']} #{task_id}",
        )
        session_id = session["id"]

    global _active_task_id, _cancel_event
    _cancel_event = asyncio.Event()
    _active_task_id = task_id
    start_commit = git.head_hash(state.PROJECT_DIR)
    await db.transition_task(task_id, "active",
                             session_id=session_id,
                             start_commit=start_commit)

    log.info("[worker] Processing task #%d (job=%s)", task_id, job_id)

    timeout_seconds = job["properties"].get("timeout", 900)
    _timed_out = False
    local_cancel = _cancel_event

    async def _timeout_watchdog():
        nonlocal _timed_out
        await asyncio.sleep(timeout_seconds)
        if not local_cancel.is_set():
            _timed_out = True
            log.warning("[worker] Task #%d timed out after %ds", task_id, timeout_seconds)
            local_cancel.set()

    watchdog = asyncio.create_task(_timeout_watchdog()) if timeout_seconds > 0 else None

    full_response = []
    streaming_text = []
    raw_event_buffer: list[tuple[str, str]] = []

    try:
        task_with_session = {**task, "session_id": session_id}
        async for event in run_task(task_id, job, state.PROJECT_DIR,
                                    cancel_event=_cancel_event,
                                    task=task_with_session,
                                    subordinates=subordinates):
            etype = event.get("type")

            if etype == "_raw":
                raw_event_buffer.append((event["event_type"], event["raw_json"]))
                continue

            _broadcast(task_id, event)

            if etype == "assistant_complete":
                full_response.append(event.get("content", ""))
                continue

            if etype == "session_id":
                cli_sid = event.get("cli_session_id")
                if cli_sid:
                    await db.update_chat_session(session_id, cli_session_id=cli_sid)
            elif etype == "text":
                streaming_text.append(event.get("content", ""))
            elif etype == "error":
                await db.add_chat_message(session_id, "system", event.get("message", "error"))

        log.info("[worker] Task #%d CLI finished — starting post-processing", task_id)

        try:
            await db.add_chat_events_batch(session_id, raw_event_buffer)
        except Exception:
            log.exception("[worker] Task #%d failed to write chat events", task_id)

        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            try:
                await db.add_chat_message(session_id, "assistant", response_text)
            except Exception:
                log.exception("[worker] Task #%d failed to write response", task_id)

        _cancelled = local_cancel.is_set() and not _timed_out
        head = git.head_hash(state.PROJECT_DIR)

        if _timed_out:
            await db.transition_task(task_id, "timed_out",
                                     result_commit=head, error="timed out")
            fresh_subs = await db.get_subordinate_tasks(task_id)
            sub_ids = [s["id"] for s in fresh_subs if s.get("status") not in db.TERMINAL_STATUSES]
            await db.transition_tasks_batch(sub_ids, "timed_out",
                                            result_commit=head, error="timed out")
            log.info("[worker] Task #%d timed out (partial commit=%s, dependents skipped)", task_id, head[:8])
        elif _cancelled:
            await db.transition_task(task_id, "cancelled",
                                     result_commit=head, error="cancelled")
            fresh_subs = await db.get_subordinate_tasks(task_id)
            sub_ids = [s["id"] for s in fresh_subs if s.get("status") not in db.TERMINAL_STATUSES]
            await db.transition_tasks_batch(sub_ids, "cancelled",
                                            result_commit=head, error="cancelled")
            log.info("[worker] Task #%d cancelled (dependents skipped)", task_id)
        else:
            await db.transition_task(task_id, "completed",
                                     result_commit=head)
            sub_ids = [s["id"] for s in subordinates]
            await db.transition_tasks_batch(sub_ids, "completed",
                                            result_commit=head)
            log.info("[worker] Task #%d completed (commit=%s)", task_id, head[:8])

            await _enqueue_dependents(job_id, task_id,
                                      job_name=job["name"],
                                      start_commit=start_commit,
                                      result_commit=head)

    except Exception as e:
        log.exception("[worker] Task #%d failed: %s", task_id, e)
        try:
            await db.add_chat_events_batch(session_id, raw_event_buffer)
        except Exception:
            pass
        try:
            response_text = "".join(full_response) or "".join(streaming_text)
            if response_text:
                await db.add_chat_message(session_id, "assistant", response_text)
            await db.add_chat_message(session_id, "system", f"Error: {e}")
        except Exception:
            pass
        await db.transition_task(task_id, "failed", error=str(e))
        try:
            fresh_subs = await db.get_subordinate_tasks(task_id)
            sub_ids = [s["id"] for s in fresh_subs if s.get("status") not in db.TERMINAL_STATUSES]
            await db.transition_tasks_batch(sub_ids, "failed", error=str(e))
        except Exception:
            pass
    finally:
        # Last-resort: if status is still 'active' (both try and except
        # crashed), force it to failed so tasks can never get stuck.
        try:
            task_row = await db.get_task(task_id)
            if task_row and task_row.get("status") == "active":
                log.error("[worker] Task #%d still active in finally — forcing to failed", task_id)
                await db.transition_task(task_id, "failed",
                                         error="internal error: post-processing failed")
        except Exception:
            log.exception("[worker] Task #%d CRITICAL: could not transition to failed", task_id)
        if watchdog and not watchdog.done():
            watchdog.cancel()
        _broadcast(task_id, {"type": "_done"})
        _subscribers.pop(task_id, None)
        _active_task_id = None
        _cancel_event = None


async def _enqueue_dependents(completed_job_id: str, task_id: int,
                              job_name: str | None = None,
                              start_commit: str | None = None,
                              result_commit: str | None = None):
    """Enqueue tasks for jobs that declare a dependency on the completed job."""
    dependent_jobs = await db.get_jobs_depending_on(completed_job_id)
    if not dependent_jobs:
        return

    upstream_name = job_name or completed_job_id

    for job in dependent_jobs:
        context = build_trigger_context(
            "dependency",
            project_dir=state.PROJECT_DIR,
            upstream_name=upstream_name,
            upstream_task_id=task_id,
            start_commit=start_commit,
            result_commit=result_commit,
        )

        new_id = await db.enqueue_task(
            job["id"], "dependency",
            trigger_detail=completed_job_id,
            context=context,
        )
        log.info("[worker] Dependency trigger: enqueued #%d for '%s' (upstream: '%s' #%d)",
                 new_id, job["id"], completed_job_id, task_id)
        notify()
