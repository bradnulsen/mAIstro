"""Background queue worker — processes tasks one at a time.

The worker is the only code path that invokes run_task.
All feeders (manual, watch, timer) just create task records.
"""

import asyncio
import logging
import os
import shutil

from backend import database as db, events, git, pubsub, state
from backend.dispatch import run_task, build_trigger_context
from backend.mcp_probe import probe_server
from backend.state import utcnow


# ── Per-task workspace (Phase 3 of workspace isolation) ────

def _worktree_path_for(project_dir: str, task_id: int) -> str:
    """Stable per-task worktree path under the project's .maistro/."""
    return os.path.join(project_dir, ".maistro", "worktrees", f"task-{task_id}")


def _task_branch_for(job_slug: str, task_id: int) -> str:
    """Branch naming convention from the workspace-isolation proposal."""
    return f"{job_slug}/task-{task_id}"


def _detect_main_branch(project_dir: str) -> str:
    """Return the operator's integration branch name.

    Looks at PROJECT_DIR's current HEAD branch — that's the branch the
    operator works on. Falls back to 'main' if HEAD is detached or unknown.
    """
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", cwd=project_dir,
    )
    if result.returncode == 0:
        name = result.stdout.strip()
        if name and name != "HEAD":
            return name
    return "main"


def _force_remove_worktree(project_dir: str, path: str):
    """Remove a worktree whether or not git tracks it cleanly.

    Tries the proper `git worktree remove --force` first. If git refuses
    or the directory still exists afterward (Windows file-lock weirdness,
    interrupted prior removal), falls back to rmtree + prune.
    """
    git.worktree_remove(project_dir, path, force=True)
    if os.path.exists(path):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
    git.worktree_prune(project_dir)

log = logging.getLogger("maistro.worker")

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event = asyncio.Event()
_lock: asyncio.Lock = asyncio.Lock()
_active_task_id: int | None = None
_cancel_event: asyncio.Event | None = None



def get_active_task_id() -> int | None:
    """Return the task ID currently being processed, or None."""
    return _active_task_id


async def _integrate_or_fail(task_id: int, job: dict,
                             workspace_dir: str, task_branch: str,
                             worktree_head: str, start_commit: str | None,
                             meta_fields: dict):
    """Atomically gate `completed` on successful integration into main.

    The architect's note in the workspace-isolation proposal: integration
    is the gate to `completed`, not a follow-up to it. The terminal
    transition that records `completed` only fires after the merge succeeds;
    otherwise the target is `failed` with the conflict captured and the
    workspace preserved for inspection.
    """
    project_dir = state.PROJECT_DIR
    job_id = job["id"]

    # No commits → nothing to integrate. The agent ran but produced no
    # durable output. Treat as failed with a clear error so the operator
    # knows the agent didn't commit; preserve the workspace so they can
    # see what (if anything) was left dirty in the worktree.
    if not start_commit or worktree_head == start_commit:
        err = "agent finished without committing"
        await db.transition_task(task_id, "failed",
                                 result_commit=worktree_head,
                                 error=err, **meta_fields)
        await db.cascade_completion(task_id, "failed",
                                    result_commit=worktree_head, error=err)
        log.warning("[worker] Task #%d completed without commits — marked failed, workspace preserved",
                    task_id)
        await _check_governor_trigger(task_id)
        return

    main_branch = _detect_main_branch(project_dir)
    # Try fast-forward first (clean linear history when main hasn't moved).
    ok, out = git.merge_branch(project_dir, task_branch, ff_only=True)
    if not ok:
        # Main has moved during the task. Make a merge commit instead.
        msg = f"Merge task #{task_id} ({job['name']}) into {main_branch}"
        ok, out = git.merge_branch(project_dir, task_branch, no_ff=True, message=msg)
        if not ok:
            git.merge_abort(project_dir)
            err = f"integration failed: {out}"
            await db.transition_task(task_id, "failed",
                                     result_commit=worktree_head,
                                     error=err, **meta_fields)
            await db.cascade_completion(task_id, "failed",
                                        result_commit=worktree_head, error=err)
            log.warning("[worker] Task #%d integration failed (merge conflict?) — workspace preserved at %s",
                        task_id, workspace_dir)
            await _check_governor_trigger(task_id)
            return

    # Integration succeeded. The new main HEAD is the durable record.
    integrated_head = git.head_hash(project_dir) or worktree_head
    await db.transition_task(task_id, "completed",
                             result_commit=integrated_head,
                             **meta_fields)
    await db.cascade_completion(task_id, "completed",
                                result_commit=integrated_head)
    log.info("[worker] Task #%d completed and integrated (main=%s, branch=%s)",
             task_id, integrated_head[:8], task_branch)

    # Workspace + branch are no longer needed — discard them.
    _force_remove_worktree(project_dir, workspace_dir)
    git.branch_delete(project_dir, task_branch, force=True)
    await db.update_task(task_id, worktree_path=None, task_branch=None)

    await _enqueue_cascades(job_id, task_id,
                            job_name=job["name"],
                            start_commit=start_commit,
                            result_commit=integrated_head)
    await _check_governor_trigger(task_id)


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
    """On startup, mark any in-flight tasks as interrupted and reconcile worktrees.

    Worktrees belonging to interrupted tasks are preserved (operator can
    inspect what the agent left). `git worktree prune` clears registry
    entries for directories that were manually removed while the backend
    was down.
    """
    if not db.DB_PATH:
        return
    stale_ids = await db.sweep_stale_tasks(utcnow())
    for tid in stale_ids:
        log.warning("[worker] Marked stale task #%d as interrupted (workspace preserved)", tid)
    if state.PROJECT_DIR:
        try:
            git.worktree_prune(state.PROJECT_DIR)
        except Exception:
            log.warning("[worker] worktree prune on startup failed")


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

    # Phase 3b: per-task worktree. The CLI subprocess and MCP server's
    # write/read tools all resolve against this path. The operator's main
    # checkout (state.PROJECT_DIR) is structurally untouchable by the agent.
    workspace_dir = _worktree_path_for(state.PROJECT_DIR, task_id)
    task_branch = _task_branch_for(job["slug"], task_id)
    if not start_commit:
        await db.transition_task(task_id, "active",
                                 session_id=session_id,
                                 start_commit=start_commit)
        await db.transition_task(task_id, "failed",
                                 error="cannot create worktree: project has no commits yet")
        await db.cascade_completion(task_id, "failed",
                                    error="cannot create worktree: project has no commits yet")
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_task(task_id)
        pubsub.notify_queue_changed()
        _active_task_id = None
        _cancel_event = None
        return

    # Defensive cleanup: a stale worktree from a prior crashed attempt
    # would block creation. Task IDs are unique so this only collides on
    # leftover state, never on legitimate concurrent runs.
    if os.path.exists(workspace_dir):
        log.warning("[worker] Task #%d found stale workspace at %s — removing", task_id, workspace_dir)
        _force_remove_worktree(state.PROJECT_DIR, workspace_dir)
        # Drop any leftover branch from the prior attempt too
        git.branch_delete(state.PROJECT_DIR, task_branch, force=True)

    wt_ok, wt_err = git.worktree_add(state.PROJECT_DIR, workspace_dir, task_branch, start_commit)
    if not wt_ok:
        log.error("[worker] Task #%d worktree creation failed: %s", task_id, wt_err)
        await db.transition_task(task_id, "active",
                                 session_id=session_id,
                                 start_commit=start_commit)
        await db.transition_task(task_id, "failed",
                                 error=f"worktree creation failed: {wt_err}")
        await db.cascade_completion(task_id, "failed",
                                    error=f"worktree creation failed: {wt_err}")
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_task(task_id)
        pubsub.notify_queue_changed()
        _active_task_id = None
        _cancel_event = None
        return

    await db.transition_task(task_id, "active",
                             session_id=session_id,
                             start_commit=start_commit,
                             worktree_path=workspace_dir,
                             task_branch=task_branch)
    pubsub.notify_queue_changed()

    log.info("[worker] Processing task #%d (job=%s, worktree=%s, branch=%s)",
             task_id, job_id, workspace_dir, task_branch)

    # ── Pre-dispatch health check: probe external MCP servers ──
    mcp_error = await _check_external_mcp_servers(job)
    if mcp_error:
        log.warning("[worker] Task #%d failed MCP health check: %s", task_id, mcp_error)
        # Health-check failure means the agent never ran — discard the empty worktree.
        _force_remove_worktree(state.PROJECT_DIR, workspace_dir)
        git.branch_delete(state.PROJECT_DIR, task_branch, force=True)
        await db.transition_task(task_id, "failed", error=mcp_error,
                                 worktree_path=None, task_branch=None)
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
                                    subordinates=subordinates,
                                    workspace_dir=workspace_dir):
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
        # Per-task workspace: result_commit reflects the agent's branch tip,
        # not the operator's main HEAD (which the agent never touches).
        worktree_head = git.head_hash(workspace_dir) or start_commit

        # Build execution metadata fields for task record
        meta_fields = {}
        if _result_meta.get("stop_reason"):
            meta_fields["stop_reason"] = _result_meta["stop_reason"]
        if _result_meta.get("num_turns") is not None:
            meta_fields["num_turns"] = _result_meta["num_turns"]
        if _result_meta.get("cost_usd") is not None:
            meta_fields["cost_usd"] = _result_meta["cost_usd"]

        if _timed_out:
            await db.transition_task(task_id, "timed_out",
                                     result_commit=worktree_head, error="timed out",
                                     **meta_fields)
            await db.cascade_completion(task_id, "timed_out",
                                        result_commit=worktree_head, error="timed out")
            log.info("[worker] Task #%d timed out (worktree=%s, dependents skipped, workspace preserved)",
                     task_id, worktree_head[:8])
            await _check_governor_trigger(task_id)
        elif _cancelled:
            await db.transition_task(task_id, "cancelled",
                                     result_commit=worktree_head, error="cancelled",
                                     **meta_fields)
            await db.cascade_completion(task_id, "cancelled",
                                        result_commit=worktree_head, error="cancelled")
            log.info("[worker] Task #%d cancelled (worktree=%s, workspace preserved)",
                     task_id, worktree_head[:8])
        elif meta_fields.get("stop_reason") == "max_turns":
            # Agent hit turn limit — exhausted, not completed
            max_turns = job["properties"].get("max_turns", 50)
            turns_used = meta_fields.get("num_turns", "?")
            err = f"hit turn limit ({turns_used}/{max_turns})"
            await db.transition_task(task_id, "exhausted",
                                     result_commit=worktree_head, error=err,
                                     **meta_fields)
            await db.cascade_completion(task_id, "exhausted",
                                        result_commit=worktree_head, error=err)
            log.info("[worker] Task #%d exhausted (%s/%s turns, worktree=%s, workspace preserved)",
                     task_id, turns_used, max_turns, worktree_head[:8])
            await _check_governor_trigger(task_id)
        else:
            # Integration is the gate to `completed`. Try to fold the agent's
            # branch into main; on conflict, transition to `failed` and
            # preserve the workspace for the operator to resolve.
            await _integrate_or_fail(
                task_id=task_id, job=job,
                workspace_dir=workspace_dir, task_branch=task_branch,
                worktree_head=worktree_head, start_commit=start_commit,
                meta_fields=meta_fields,
            )

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
        worktree_head = git.head_hash(workspace_dir) or start_commit
        await db.transition_task(task_id, "failed",
                                 result_commit=worktree_head, error=str(e))
        try:
            await db.cascade_completion(task_id, "failed",
                                        result_commit=worktree_head, error=str(e))
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
