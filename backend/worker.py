"""Background queue worker — processes tasks one at a time.

The worker is the only code path that invokes run_task.
All feeders (manual, watch, timer) just create task records.
"""

import asyncio
import logging

from backend import database as db, git, state
from backend.dispatch import run_task, _build_queue_context
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
        if task.get("started_at") or task.get("error"):
            return None
        if task.get("approval") == "pending":
            await db.approve_task(task_id)
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

            # Worker always processes queued tasks — auto-queueing only
            # controls where new tasks land, not whether the worker runs.
            async with _lock:
                task = await db.get_oldest_queued_task()
                if task:
                    await _process_task(task)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[worker] Error in loop")
            await asyncio.sleep(5)


async def _process_task(task: dict):
    """Execute a single task — create session, run CLI, store output."""
    task_id = task["id"]
    job_id = task["job_id"]

    if not state.PROJECT_DIR:
        await db.update_task(task_id, completed_at=utcnow(), error="no project open")
        return

    job = await db.get_job(job_id)
    if not job:
        await db.update_task(task_id, completed_at=utcnow(), error=f"job '{job_id}' not found")
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
    await db.update_task(task_id, started_at=utcnow(), session_id=session_id,
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

        await db.add_chat_events_batch(session_id, raw_event_buffer)

        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)

        if _timed_out:
            head = git.head_hash(state.PROJECT_DIR)
            now = utcnow()
            await db.update_task(task_id, completed_at=now,
                                 result_commit=head, error="timed out")
            sub_ids = [s["id"] for s in subordinates if not s.get("completed_at")]
            await db.update_tasks_batch(sub_ids, completed_at=now,
                                        result_commit=head, error="timed out")
            log.info("[worker] Task #%d timed out (partial commit=%s, dependents skipped)", task_id, head[:8])
        else:
            head = git.head_hash(state.PROJECT_DIR)
            now = utcnow()
            await db.update_task(task_id, completed_at=now, result_commit=head)
            sub_ids = [s["id"] for s in subordinates]
            await db.update_tasks_batch(sub_ids, completed_at=now, result_commit=head)
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
        response_text = "".join(full_response) or "".join(streaming_text)
        if response_text:
            await db.add_chat_message(session_id, "assistant", response_text)
        await db.add_chat_message(session_id, "system", f"Error: {e}")
        now = utcnow()
        await db.update_task(task_id, completed_at=now, error=str(e))
        sub_ids = [s["id"] for s in subordinates if not s.get("completed_at")]
        await db.update_tasks_batch(sub_ids, completed_at=now, error=str(e))
    finally:
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

    commit_context = ""
    if result_commit and state.PROJECT_DIR:
        summary = git.commit_oneline(state.PROJECT_DIR, result_commit) or result_commit[:8]
        commit_context = f"\n  Upstream result commit `{result_commit[:8]}`: {summary}"
        if start_commit and start_commit != result_commit:
            commit_context += f"\n  Upstream commit range: `{start_commit[:8]}..{result_commit[:8]}`"
            outcome = git.outcome_summary(state.PROJECT_DIR, start_commit, result_commit)
            if outcome:
                commit_context += f"\n  Outcome:\n{outcome}"

    for job in dependent_jobs:
        context = f"**Dependency** — triggered by completion of {upstream_name} (task #{task_id}){commit_context}"

        new_id = await db.enqueue_task(
            job["id"], "dependency",
            trigger_detail=completed_job_id,
            context=context,
        )
        log.info("[worker] Dependency trigger: enqueued #%d for '%s' (upstream: '%s' #%d)",
                 new_id, job["id"], completed_job_id, task_id)
        notify()
