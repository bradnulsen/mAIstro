"""Background queue worker — processes tasks one at a time.

The worker is the only code path that invokes run_task.
All feeders (manual, watch, timer) just create task records.
"""

import asyncio
import logging

from backend import database as db, events, git, pubsub, state
from backend.dispatch import run_task, build_trigger_context
from backend.mcp_probe import probe_server
from backend.state import utcnow

log = logging.getLogger("maistro.worker")

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event = asyncio.Event()
_lock: asyncio.Lock = asyncio.Lock()
_active_task_id: int | None = None
_cancel_event: asyncio.Event | None = None



def get_active_task_id() -> int | None:
    """Return the task ID currently being processed, or None."""
    return _active_task_id


async def _maybe_stash_orphan(task_id: int, start_commit: str | None,
                              result_commit: str | None, terminal: str) -> str | None:
    """Stash agent-orphaned working-tree changes on a non-success terminal.

    Phase 2 of task workspace isolation: when a task ends without committing
    (start == result) but left a dirty tree, capture those changes into a
    labeled stash so the next task doesn't inherit them and the operator
    has a recoverable handle. Phase 3 (per-task worktrees) supersedes this.

    Caveat: a dirty tree at terminal time can also contain the operator's
    in-progress edits made concurrently. We sweep everything dirty rather
    than try to attribute — the stash is recoverable and the alternative
    (silently leaking agent work into the next task's context) is worse.
    """
    if not start_commit or not result_commit or start_commit != result_commit:
        return None
    if not git.is_dirty(state.PROJECT_DIR):
        return None
    stash_ref = git.stash_push_orphan(
        state.PROJECT_DIR, f"task-{task_id} orphan @ {terminal}"
    )
    if stash_ref:
        log.warning("[worker] Task #%d stashed orphan changes (ref=%s, terminal=%s)",
                    task_id, stash_ref[:8], terminal)
    return stash_ref


async def _check_governor_trigger(task_id: int):
    """Increment Governor counter and trigger a run if threshold reached.

    Called after any executed terminal transition (completed, exhausted,
    failed, timed_out) so the Governor sees a representative sample of
    activity — including patterns of failure. Cancelled tasks are excluded
    (user action, not system signal).

    Failures are logged and swallowed so a counter glitch never breaks
    terminal handling.
    """
    try:
        from backend import governor
        count = await db.increment_governor_counter()
        if count >= 10:
            await db.reset_governor_counter()
            governor.spawn(governor.run_governor("auto", task_count=count))
    except Exception:
        log.warning("[worker] Governor counter check failed for task #%d", task_id)


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
    pubsub.notify_queue_changed()


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
        if task.get("status") not in db.PRE_EXECUTION_STATUSES:
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
    pubsub.notify_queue_changed()

    log.info("[worker] Processing task #%d (job=%s)", task_id, job_id)

    # ── Pre-dispatch health check: probe external MCP servers ──
    mcp_error = await _check_external_mcp_servers(job)
    if mcp_error:
        log.warning("[worker] Task #%d failed MCP health check: %s", task_id, mcp_error)
        await db.transition_task(task_id, "failed", error=mcp_error)
        await db.cascade_completion(task_id, "failed", error=mcp_error)
        pubsub.broadcast(task_id, events.error(mcp_error))
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_task(task_id)
        pubsub.notify_queue_changed()
        _active_task_id = None
        _cancel_event = None
        return

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
    # Execution metadata captured from CLI result event
    _result_meta: dict = {}

    try:
        task_with_session = {**task, "session_id": session_id}
        async for event in run_task(task_id, job, state.PROJECT_DIR,
                                    cancel_event=_cancel_event,
                                    task=task_with_session,
                                    subordinates=subordinates):
            etype = event.get("type")

            # Incremental persistence: write each raw NDJSON event immediately
            if etype == "_raw":
                try:
                    await db.add_chat_event(session_id, event["event_type"], event["raw_json"])
                except Exception:
                    log.warning("[worker] Task #%d failed to write chat event (type=%s)",
                                task_id, event.get("event_type"))
                continue

            pubsub.broadcast(task_id, event)

            if etype == "assistant_complete":
                full_response.append(event.get("content", ""))
                continue

            if etype == "result_meta":
                cli_sid = event.get("cli_session_id")
                if cli_sid:
                    await db.update_chat_session(session_id, cli_session_id=cli_sid)
                _result_meta = {
                    "stop_reason": event.get("stop_reason"),
                    "num_turns": event.get("num_turns"),
                    "cost_usd": event.get("cost_usd"),
                }
            elif etype == "error":
                await db.add_chat_message(session_id, "system", event.get("message", "error"))

        log.info("[worker] Task #%d CLI finished — starting post-processing", task_id)

        response_text = "".join(full_response)
        if response_text:
            try:
                await db.add_chat_message(session_id, "assistant", response_text)
            except Exception:
                log.exception("[worker] Task #%d failed to write response", task_id)

        _cancelled = local_cancel.is_set() and not _timed_out
        head = git.head_hash(state.PROJECT_DIR)

        # Build execution metadata fields for task record
        meta_fields = {}
        if _result_meta.get("stop_reason"):
            meta_fields["stop_reason"] = _result_meta["stop_reason"]
        if _result_meta.get("num_turns") is not None:
            meta_fields["num_turns"] = _result_meta["num_turns"]
        if _result_meta.get("cost_usd") is not None:
            meta_fields["cost_usd"] = _result_meta["cost_usd"]

        if _timed_out:
            stash_ref = await _maybe_stash_orphan(task_id, start_commit, head, "timed_out")
            extra = {"orphan_stash_ref": stash_ref} if stash_ref else {}
            await db.transition_task(task_id, "timed_out",
                                     result_commit=head, error="timed out",
                                     **meta_fields, **extra)
            await db.cascade_completion(task_id, "timed_out",
                                        result_commit=head, error="timed out")
            log.info("[worker] Task #%d timed out (partial commit=%s, dependents skipped)", task_id, head[:8])
            await _check_governor_trigger(task_id)
        elif _cancelled:
            stash_ref = await _maybe_stash_orphan(task_id, start_commit, head, "cancelled")
            extra = {"orphan_stash_ref": stash_ref} if stash_ref else {}
            await db.transition_task(task_id, "cancelled",
                                     result_commit=head, error="cancelled",
                                     **meta_fields, **extra)
            await db.cascade_completion(task_id, "cancelled",
                                        result_commit=head, error="cancelled")
            log.info("[worker] Task #%d cancelled (dependents skipped)", task_id)
        elif meta_fields.get("stop_reason") == "max_turns":
            # Agent hit turn limit — exhausted, not completed
            max_turns = job["properties"].get("max_turns", 50)
            turns_used = meta_fields.get("num_turns", "?")
            err = f"hit turn limit ({turns_used}/{max_turns})"
            stash_ref = await _maybe_stash_orphan(task_id, start_commit, head, "exhausted")
            extra = {"orphan_stash_ref": stash_ref} if stash_ref else {}
            await db.transition_task(task_id, "exhausted",
                                     result_commit=head, error=err,
                                     **meta_fields, **extra)
            await db.cascade_completion(task_id, "exhausted",
                                        result_commit=head, error=err)
            log.info("[worker] Task #%d exhausted (%s/%s turns, commit=%s)",
                     task_id, turns_used, max_turns, head[:8])
            await _check_governor_trigger(task_id)
        else:
            await db.transition_task(task_id, "completed",
                                     result_commit=head,
                                     **meta_fields)
            await db.cascade_completion(task_id, "completed",
                                        result_commit=head)
            log.info("[worker] Task #%d completed (commit=%s)", task_id, head[:8])

            await _enqueue_cascades(job_id, task_id,
                                    job_name=job["name"],
                                    start_commit=start_commit,
                                    result_commit=head)

            await _check_governor_trigger(task_id)

    except Exception as e:
        log.exception("[worker] Task #%d failed: %s", task_id, e)
        # Raw events already persisted incrementally — just write response
        try:
            response_text = "".join(full_response)
            if response_text:
                await db.add_chat_message(session_id, "assistant", response_text)
            await db.add_chat_message(session_id, "system", f"Error: {e}")
        except Exception as write_err:
            log.warning("[worker] Task #%d failed to write error response: %s", task_id, write_err)
        head = git.head_hash(state.PROJECT_DIR)
        stash_ref = await _maybe_stash_orphan(task_id, start_commit, head, "failed")
        extra = {"orphan_stash_ref": stash_ref} if stash_ref else {}
        await db.transition_task(task_id, "failed", error=str(e), **extra)
        try:
            await db.cascade_completion(task_id, "failed", error=str(e))
        except Exception as cascade_err:
            log.warning("[worker] Task #%d failed to cascade failure to subordinates: %s", task_id, cascade_err)
        await _check_governor_trigger(task_id)
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
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_task(task_id)
        pubsub.notify_queue_changed()
        _active_task_id = None
        _cancel_event = None


async def _check_external_mcp_servers(job: dict) -> str | None:
    """Probe external MCP servers assigned to this job. Returns error string or None."""
    import json
    job_mcp_names = set(job["properties"].get("mcp_servers") or [])
    if not job_mcp_names:
        return None

    all_servers = await db.list_mcp_servers()
    server_map = {s["name"]: s for s in all_servers}

    for name in sorted(job_mcp_names):
        server = server_map.get(name)
        if not server:
            return f"External MCP server '{name}' not found — it may have been deleted"
        if not server.get("enabled", True):
            return f"External MCP server '{name}' is disabled"

        args = json.loads(server.get("args") or "[]")
        env = json.loads(server.get("env") or "{}")
        result = await probe_server(server["command"], args, env)
        if result["status"] != "ok":
            error_detail = result.get("error", "unknown error")
            return f"External MCP server '{name}' health check failed: {error_detail}"

    return None


async def _enqueue_cascades(completed_job_id: int, task_id: int,
                            job_name: str | None = None,
                            start_commit: str | None = None,
                            result_commit: str | None = None):
    """Enqueue tasks for jobs that cascade from the completed job."""
    cascade_jobs = await db.get_cascade_targets(completed_job_id)
    if not cascade_jobs:
        return

    upstream_name = job_name or completed_job_id

    for job in cascade_jobs:
        context = build_trigger_context(
            "cascade",
            project_dir=state.PROJECT_DIR,
            upstream_name=upstream_name,
            upstream_task_id=task_id,
            start_commit=start_commit,
            result_commit=result_commit,
        )

        new_id = await db.enqueue_task(
            job["id"], "cascade",
            trigger_detail=str(completed_job_id),
            context=context,
        )
        log.info("[worker] Cascade: enqueued #%d for job %d (upstream: %d #%d)",
                 new_id, job["id"], completed_job_id, task_id)
        notify()
