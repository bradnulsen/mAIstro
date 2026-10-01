"""Background queue worker — processes triggers one at a time.

The worker is the only code path that invokes run_task.
All feeders (manual, watch, timer) just create trigger records.

Stage 1 note: identifiers below use the trigger/dispatch vocabulary,
but SQL string literals continue to reference the legacy `tasks` /
`task_executions` table names and many internal Python variables
(`task_id`, `task_branch`) keep their old names. The schema rename
and full identifier polish are Stage 2+ of triggers-and-dispatches.
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


async def _force_remove_worktree(project_dir: str, path: str):
    """Remove a worktree whether or not git tracks it cleanly.

    Tries the proper `git worktree remove --force` first. If git refuses
    or the directory still exists afterward (Windows file-lock weirdness,
    interrupted prior removal), falls back to rmtree + prune.
    """
    await git.worktree_remove(project_dir, path, force=True)
    if os.path.exists(path):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
    await git.worktree_prune(project_dir)

log = logging.getLogger("maistro.worker")

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event = asyncio.Event()
_lock: asyncio.Lock = asyncio.Lock()
_active_task_id: int | None = None
_cancel_event: asyncio.Event | None = None

# ── Health state ────────────────────────────────────────────
#
# `_last_loop_tick_at` is bumped every iteration of `_loop()`; the
# system-health endpoint surfaces it so the frontend can detect a
# wedged worker. While a task is running the loop is blocked in
# `_process_trigger`, so the timestamp only refreshes between tasks —
# the frontend should treat it together with `_active_task_id`.
#
# `_silent_streak` counts consecutive completions where the CLI
# exited "successfully" but did no real model work (cost=0, ≤1 turn,
# no tool calls). That's the smoking-gun pattern for an upstream
# API/network failure that the CLI swallowed. Two in a row sets
# `_health_halt_reason`; the worker then refuses to pull queued
# work until cleared via `clear_health_halt()` (from the Resume
# button) or by a subsequent dispatch that does real work.
_last_loop_tick_at: str | None = None
_silent_streak: int = 0
_health_halt_reason: str | None = None
_health_halt_at: str | None = None

# Stop-reasons the CLI returns when it thinks the model finished
# normally. A $0.00 / ≤1-turn dispatch under one of these reasons is
# almost certainly silent: the model never actually responded.
_SUCCESSY_STOP_REASONS = {"success", "end_turn", "stop_sequence", "stop", None, ""}


def get_active_dispatch_id() -> int | None:
    """Return the task ID currently being processed, or None."""
    return _active_task_id


def get_health_snapshot() -> dict:
    """Return current worker-health signals for the /api/system/health route.

    Frontend-friendly shape. The route module decides overall ok/warn/fatal
    after combining these with its own checks (claude CLI on PATH, etc.).
    """
    return {
        "last_tick_at": _last_loop_tick_at,
        "active_dispatch_id": _active_task_id,
        "silent_streak": _silent_streak,
        "halt_reason": _health_halt_reason,
        "halt_at": _health_halt_at,
    }


def clear_health_halt() -> None:
    """Resume after an operator confirms the upstream issue is resolved.

    Resets the silent-failure streak and lets the worker pull again on
    the next loop iteration. The operator's `auto_dispatch` toggle is
    untouched — this is strictly an automatic-safety lift.
    """
    global _silent_streak, _health_halt_reason, _health_halt_at
    _silent_streak = 0
    _health_halt_reason = None
    _health_halt_at = None
    _wake_event.set()


def _is_silent_api_failure(meta: dict, has_tool_use: bool) -> bool:
    """Detect the (no-cost, no-turns, success-y stop) dispatch pattern.

    A real model response always costs money and reports ≥1 turn. Tool use
    is a stronger positive signal — if the agent invoked anything, the
    model definitely responded. Anything else with `cost_usd == 0` looks
    like the CLI gave up before the API answered.
    """
    if has_tool_use:
        return False
    cost = meta.get("cost_usd")
    if cost is None or cost > 0:
        return False
    turns = meta.get("num_turns")
    if turns is not None and turns > 1:
        return False
    return meta.get("stop_reason") in _SUCCESSY_STOP_REASONS


def _update_health_after_dispatch(meta_fields: dict, has_tool_use: bool,
                                  completion_state: str) -> None:
    """Record a dispatch outcome's effect on system health.

    Only success-like terminals participate in the silent-failure heuristic.
    A `failed` / `timed_out` / `cancelled` outcome is already loud — it
    shouldn't reset or extend the streak. `exhausted` did real work
    (consumed turns), so it resets the streak.
    """
    global _silent_streak, _health_halt_reason, _health_halt_at
    if completion_state not in ("completed", "exhausted"):
        return
    if _is_silent_api_failure(meta_fields, has_tool_use):
        _silent_streak += 1
        if _silent_streak >= 2 and not _health_halt_reason:
            _health_halt_reason = (
                f"{_silent_streak} consecutive dispatches finished at $0.00 "
                f"with <=1 turn and no tool use -- probable API or network "
                f"outage. Worker paused. Click Resume after you've verified "
                f"upstream."
            )
            _health_halt_at = utcnow()
            log.warning("[worker] HEALTH HALT: %s", _health_halt_reason)
    else:
        _silent_streak = 0


async def _integrate_or_fail(task_id: int, job: dict,
                             workspace_dir: str, task_branch: str,
                             worktree_head: str, start_commit: str | None,
                             meta_fields: dict):
    """Atomically gate `completed` on successful integration into main.

    Operator-wins policy: any integration conflict (agent's branch vs.
    main, or operator's dirty edits vs. the integrated result) marks the
    task `failed` with the conflict captured in `error`. Operator state
    is restored to exactly what it was before the integration step
    started; the agent's work stays on the preserved task branch for
    manual integration via the workspace banner's "Integrate" button or
    the operator's own git CLI.

    The actual stash/merge/unstash dance lives in `git.integrate_branch`
    so the manual `POST /workspace/integrate` route can reuse the same
    logic.
    """
    project_dir = state.PROJECT_DIR
    job_id = job["id"]

    # No commits → two cases, distinguished by worktree dirtiness:
    #   1. Clean worktree: agent intentionally did nothing. The dispatch
    #      system prompt explicitly invites this ("Doing nothing is a
    #      valid outcome"). Mark completed (no-op), clean up the empty
    #      worktree, fire cascades normally.
    #   2. Dirty worktree: agent edited files but didn't commit them.
    #      That's a real failure — the work is at risk and the operator
    #      should see it. Mark failed, preserve the workspace.
    if not start_commit or worktree_head == start_commit:
        if await git.is_dirty(workspace_dir):
            err = "agent left uncommitted changes in the worktree"
            await db.transition_trigger(task_id, "failed",
                                     result_commit=worktree_head,
                                     error=err, **meta_fields)
            await db.cascade_completion(task_id, "failed",
                                        result_commit=worktree_head, error=err)
            log.warning("[worker] Task #%d ended with uncommitted changes — marked failed, workspace preserved",
                        task_id)
            await _check_governor_trigger(task_id)
            return
        # No-op success: agent ran, decided nothing was needed. Cascades
        # are deliberately *not* fired here — propagating an empty commit
        # range to downstream cascade-trigger jobs would just produce a
        # chain of identical no-ops at real CLI cost. Cascades require
        # actual work to react to.
        await db.transition_trigger(task_id, "completed",
                                 result_commit=start_commit, **meta_fields)
        await db.cascade_completion(task_id, "completed",
                                    result_commit=start_commit)
        log.info("[worker] Task #%d completed with no commits (no-op outcome, cascades suppressed)", task_id)
        await _force_remove_worktree(project_dir, workspace_dir)
        await git.branch_delete(project_dir, task_branch, force=True)
        await db.clear_workspace_pointers(workspace_dir)
        await _check_governor_trigger(task_id)
        return

    merge_msg = f"Merge task #{task_id} ({job['name']}) into main"
    ok, err = await git.integrate_branch(
        project_dir, task_branch,
        stash_label=f"maistro pre-integrate task-{task_id}",
        merge_message=merge_msg,
        dedup_label=f"pre-integrate task-{task_id}",
    )
    if not ok:
        await db.transition_trigger(task_id, "failed",
                                 result_commit=worktree_head,
                                 error=err, **meta_fields)
        await db.cascade_completion(task_id, "failed",
                                    result_commit=worktree_head, error=err)
        log.warning("[worker] Task #%d integration failed — workspace preserved at %s "
                    "on branch '%s'", task_id, workspace_dir, task_branch)
        await _check_governor_trigger(task_id)
        return

    integrated_head = await git.head_hash(project_dir) or worktree_head
    await db.transition_trigger(task_id, "completed",
                             result_commit=integrated_head,
                             **meta_fields)
    await db.cascade_completion(task_id, "completed",
                                result_commit=integrated_head)
    log.info("[worker] Task #%d completed and integrated (main=%s, branch=%s)",
             task_id, integrated_head[:8], task_branch)

    # Workspace + branch are no longer needed — discard them. Clear pointers
    # on every dispatch row sharing this worktree (auto_continue / resume
    # chains can point at the same path).
    await _force_remove_worktree(project_dir, workspace_dir)
    await git.branch_delete(project_dir, task_branch, force=True)
    await db.clear_workspace_pointers(workspace_dir)

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
    if not await git.is_dirty(state.PROJECT_DIR):
        return None
    stash_ref = await git.stash_push_orphan(
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
        task = await db.get_trigger(task_id)
        if not task:
            return None
        if task.get("status") not in db.PRE_EXECUTION_STATUSES:
            return None
        if task.get("approval") == "pending":
            await db.approve_trigger(task_id)
        # Ensure task is queued before processing (transition validates legality)
        if task.get("status") == "pending":
            await db.transition_trigger(task_id, "queued")
        await _process_trigger(task)
        return task



# ── Internal ────────────────────────────────────────────────

async def _sweep_stale():
    """On startup, mark any in-flight tasks as interrupted and reconcile worktrees.

    Worktrees belonging to interrupted tasks are preserved (operator can
    inspect what the agent left). `git worktree prune` clears registry
    entries for directories that were manually removed while the backend
    was down. `_sweep_orphan_worktrees` removes worktree directories whose
    tasks shouldn't have one — covers the activation race where the
    backend crashed between `worktree_add` and the DB transition.
    """
    if not db.DB_PATH:
        return
    stale_ids = await db.sweep_stale_triggers(utcnow())
    for tid in stale_ids:
        log.warning("[worker] Marked stale task #%d as interrupted (workspace preserved)", tid)
    if state.PROJECT_DIR:
        try:
            await git.worktree_prune(state.PROJECT_DIR)
        except Exception:
            log.warning("[worker] worktree prune on startup failed")
        try:
            await _sweep_orphan_worktrees()
        except Exception:
            log.exception("[worker] orphan-worktree sweep failed")
        try:
            await _sweep_stale_workspace_pointers()
        except Exception:
            log.exception("[worker] stale-workspace-pointer sweep failed")
        try:
            await _sweep_chat_events_retention()
        except Exception:
            log.exception("[worker] chat_events retention sweep failed")
        try:
            await _sweep_task_events_retention()
        except Exception:
            log.exception("[worker] task_events retention sweep failed")


async def _sweep_stale_workspace_pointers():
    """Clear DB worktree_path/task_branch on tasks whose disk artifacts are gone.

    Reverse of _sweep_orphan_worktrees: that sweep walks the disk and
    cleans up directories with no DB task. This walks the DB and clears
    pointers on tasks whose directory or branch has gone missing (operator
    cleanup, partial crash recovery, manual `git worktree remove`, etc.).
    Without this, the WorkspaceBanner keeps surfacing for a workspace that
    no longer exists, and clicking Integrate produces a confusing error
    because the branch isn't there to merge.
    """
    db_conn = await db.get_db()
    rows = await db_conn.execute_fetchall(
        """SELECT t.id, te.worktree_path, te.task_branch
           FROM tasks t
           JOIN task_executions te ON te.task_id = t.id
           WHERE te.worktree_path IS NOT NULL"""
    )
    for row in rows:
        tid = row["id"]
        wt_path = row["worktree_path"]
        branch = row["task_branch"]
        dir_exists = bool(wt_path) and os.path.isdir(wt_path)
        branch_present = bool(branch) and await git.branch_exists(state.PROJECT_DIR, branch)
        if dir_exists and branch_present:
            continue
        log.warning(
            "[worker] Clearing stale workspace pointers for task #%d "
            "(dir_exists=%s, branch_exists=%s, path=%s, branch=%s)",
            tid, dir_exists, branch_present, wt_path, branch,
        )
        # Path-based: also clears any chain ancestors pointing at the same path.
        await db.clear_workspace_pointers(wt_path)


async def _sweep_retention(config_key: str, prune_fn) -> None:
    """Shared shape: read `config_key` as int days, prune if > 0, log count."""
    raw = await db.get_config(config_key)
    try:
        days = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        log.warning("[worker] %s not an integer: %r", config_key, raw)
        return
    if days <= 0:
        return
    deleted = await prune_fn(days)
    if deleted:
        log.info("[worker] %s pruned %d rows older than %d days",
                 config_key, deleted, days)


async def _sweep_chat_events_retention():
    """Drop old chat_events when the operator has opted into a retention window.

    `chat_events_retention_days` defaults to 0 (never prune) per the
    platform-philosophy of not imposing workflow opinions; set it to a
    positive integer via config to enable. The DELETE in `prune_chat_events`
    only targets sessions whose task is terminal AND whose chat_messages
    cache already holds an assistant row, so the fallback reconstruction
    never depends on rows we deleted.
    """
    await _sweep_retention("chat_events_retention_days", db.prune_chat_events)


async def _sweep_task_events_retention():
    """Drop old task_events when the operator has opted into a retention window.

    `task_events_retention_days` defaults to 0. Trade is documented on
    `prune_task_events`: pruned triggers fall off the dashboard timeline.
    The materialized `tasks.status` cache survives, so queue listings are
    unaffected.
    """
    await _sweep_retention("task_events_retention_days", db.prune_task_events)


async def _sweep_orphan_worktrees():
    """Remove worktree dirs whose task is missing or shouldn't have one.

    Closes the activation race: if the backend crashed between
    `worktree_add` and the `transition_trigger(active, worktree_path=...)`
    write, the directory is on disk but no task references it. Also
    catches manual/test detritus and worktrees left after a task was
    deleted directly from the DB.
    """
    worktrees_dir = os.path.join(state.PROJECT_DIR, ".maistro", "worktrees")
    if not os.path.isdir(worktrees_dir):
        return
    for entry in os.listdir(worktrees_dir):
        if not entry.startswith("task-"):
            continue
        try:
            task_id = int(entry[len("task-"):])
        except ValueError:
            continue
        task = await db.get_trigger(task_id)
        # Keep worktrees for active tasks (in-flight) and non-success
        # terminals (preserved for inspection).
        if task and task.get("status") in db.NON_SUCCESS_TERMINAL_STATUSES | {"active"}:
            continue
        full_path = os.path.join(worktrees_dir, entry)
        log.warning("[worker] Sweeping orphan worktree task-%d (status=%s)",
                    task_id, task.get("status") if task else "MISSING")
        await _force_remove_worktree(state.PROJECT_DIR, full_path)
        # Best-effort branch cleanup if we can resolve the slug.
        if task:
            job = await db.get_job(task["job_id"])
            if job:
                await git.branch_delete(state.PROJECT_DIR,
                                        _task_branch_for(job["slug"], task_id),
                                        force=True)


async def _loop():
    """Main worker loop — poll for pending tasks."""
    global _last_loop_tick_at
    _swept_project = None
    while True:
        try:
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            _wake_event.clear()
            _last_loop_tick_at = utcnow()

            if not state.PROJECT_DIR:
                continue

            # Health halt: refuse to pull new work after a confirmed
            # silent-failure streak. Existing in-flight dispatches were
            # never affected (the loop only gates the NEXT pull); the
            # operator clears this via clear_health_halt() through the
            # /api/system/health/clear route.
            if _health_halt_reason:
                continue

            if _swept_project != state.PROJECT_DIR:
                await _sweep_stale()
                _swept_project = state.PROJECT_DIR

            # Guard the queue poll so close_db() waits for us.
            # Task execution itself is protected by the active-task check
            # at the HTTP layer — this covers the poll gap.
            async with db.db_read_guard():
                async with _lock:
                    task = await db.get_oldest_queued_trigger()
                    if task:
                        await _process_trigger(task)

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


async def _process_trigger(task: dict):
    """Execute a single task — create session, run CLI, store output."""
    task_id = task["id"]
    job_id = task["job_id"]

    if not state.PROJECT_DIR:
        try:
            await db.transition_trigger(task_id, "active")
        except ValueError:
            pass
        await db.transition_trigger(task_id, "failed", error="no project open")
        return

    job = await db.get_job(job_id)
    if not job:
        try:
            await db.transition_trigger(task_id, "active")
        except ValueError:
            pass
        await db.transition_trigger(task_id, "failed", error=f"job '{job_id}' not found")
        return

    # Collect subordinate tasks (coalesced) and pass to queue context builder
    subordinates = await db.get_subordinate_triggers(task_id)

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
    start_commit = await git.head_hash(state.PROJECT_DIR)

    # Self-heal projects that were opened before the bootstrap-on-open fix:
    # if HEAD is null, create the platform's bootstrap commit now so this
    # task (and every future one) has a real ref to branch off.
    if not start_commit:
        log.info("[worker] Task #%d: project has no commits — bootstrapping initial commit", task_id)
        start_commit = await git.ensure_initial_commit(state.PROJECT_DIR)

    # Phase 3b: per-task worktree. The CLI subprocess and MCP server's
    # write/read tools all resolve against this path. The operator's main
    # checkout (state.PROJECT_DIR) is structurally untouchable by the agent.
    workspace_dir = _worktree_path_for(state.PROJECT_DIR, task_id)
    task_branch = _task_branch_for(job["slug"], task_id)
    reuse_existing_worktree = False

    # Resume tasks must run in the original task's worktree. Claude CLI
    # stores sessions per-cwd at ~/.claude/projects/<hash-of-cwd>/, so a
    # `--resume <session>` invocation in a fresh worktree finds nothing
    # and insta-fails. The original's worktree also still has the agent's
    # prior commits on its branch — picking up there is what "resume"
    # actually means.
    #
    # `auto_continue` shares the worktree-inheritance behavior but not the
    # CLI session reuse: it runs a fresh conversation in the prior task's
    # preserved worktree+branch. The prior conversation already hit its
    # limit; resuming it would just hit the limit again.
    needs_worktree_inheritance = (
        (resume_session_id and task.get("trigger_detail"))
        or (task.get("trigger") == "auto_continue" and task.get("trigger_detail"))
    )
    if needs_worktree_inheritance:
        try:
            original_id = int(task["trigger_detail"])
        except (ValueError, TypeError):
            original_id = None
        if original_id:
            original = await db.get_trigger(original_id)
            original_path = original.get("worktree_path") if original else None
            original_branch = original.get("task_branch") if original else None
            if original_path and os.path.isdir(original_path):
                workspace_dir = original_path
                task_branch = original_branch or task_branch
                reuse_existing_worktree = True
                kind = "resuming" if resume_session_id else "auto-continuing"
                log.info("[worker] Task #%d %s in original worktree %s on branch %s",
                         task_id, kind, workspace_dir, task_branch)
            else:
                # Original's worktree is gone (cleaned up after successful
                # integration, or operator discarded it). For resume the
                # session storage is gone too; for auto_continue the
                # preserved-worktree premise is violated. Fail loudly.
                kind = "resume" if resume_session_id else "auto-continue"
                err = (f"cannot {kind} task #{original_id}: the original "
                       f"worktree no longer exists")
                await db.transition_trigger(task_id, "active",
                                         session_id=session_id,
                                         start_commit=start_commit)
                await db.transition_trigger(task_id, "failed", error=err)
                await db.cascade_completion(task_id, "failed", error=err)
                pubsub.broadcast(task_id, events.error(err))
                pubsub.broadcast(task_id, events.done())
                pubsub.cleanup_trigger(task_id)
                pubsub.notify_queue_changed()
                _active_task_id = None
                _cancel_event = None
                return

    if not start_commit:
        await db.transition_trigger(task_id, "active",
                                 session_id=session_id,
                                 start_commit=start_commit)
        await db.transition_trigger(task_id, "failed",
                                 error="cannot create worktree: project has no commits yet")
        await db.cascade_completion(task_id, "failed",
                                    error="cannot create worktree: project has no commits yet")
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_trigger(task_id)
        pubsub.notify_queue_changed()
        _active_task_id = None
        _cancel_event = None
        return

    if not reuse_existing_worktree:
        # Defensive cleanup: a stale worktree from a prior crashed attempt
        # would block creation. Task IDs are unique so this only collides on
        # leftover state, never on legitimate concurrent runs.
        if os.path.exists(workspace_dir):
            log.warning("[worker] Task #%d found stale workspace at %s — removing", task_id, workspace_dir)
            await _force_remove_worktree(state.PROJECT_DIR, workspace_dir)
            # Drop any leftover branch from the prior attempt too
            await git.branch_delete(state.PROJECT_DIR, task_branch, force=True)

        wt_ok, wt_err = await git.worktree_add(
            state.PROJECT_DIR, workspace_dir, task_branch, start_commit,
            user_name=job["name"],
        )
        if not wt_ok:
            log.error("[worker] Task #%d worktree creation failed: %s", task_id, wt_err)
            await db.transition_trigger(task_id, "active",
                                     session_id=session_id,
                                     start_commit=start_commit)
            await db.transition_trigger(task_id, "failed",
                                     error=f"worktree creation failed: {wt_err}")
            await db.cascade_completion(task_id, "failed",
                                        error=f"worktree creation failed: {wt_err}")
            pubsub.broadcast(task_id, events.done())
            pubsub.cleanup_trigger(task_id)
            pubsub.notify_queue_changed()
            _active_task_id = None
            _cancel_event = None
            return

    await db.transition_trigger(task_id, "active",
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
        # NOTE: for auto_continue tasks this nukes the *inherited* prior worktree,
        # which an operator expected preserved. Separate concern from the DB
        # pointer hygiene fixed here; flagged for follow-up.
        await _force_remove_worktree(state.PROJECT_DIR, workspace_dir)
        await git.branch_delete(state.PROJECT_DIR, task_branch, force=True)
        await db.transition_trigger(task_id, "failed", error=mcp_error)
        await db.clear_workspace_pointers(workspace_dir)
        await db.cascade_completion(task_id, "failed", error=mcp_error)
        pubsub.broadcast(task_id, events.error(mcp_error))
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_trigger(task_id)
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
    # Track whether the agent invoked any tools — used by the silent-failure
    # health heuristic. Tool use is a strong positive signal that the API
    # really answered (a $0 dispatch with tool calls is impossible).
    _has_tool_use = False

    # Raw NDJSON events buffer. Streaming writes were one-commit-per-line,
    # which serialized every concurrent task on the single writer connection.
    # Flush by size or at natural boundaries (turn end, terminal events,
    # post-loop, error handler, finally) so the durability window stays
    # short while bulk-amortizing the per-event commit.
    _raw_buffer: list[tuple[str, str]] = []
    RAW_BUFFER_FLUSH_SIZE = 25

    async def _flush_raw():
        if not _raw_buffer:
            return
        pending = list(_raw_buffer)
        _raw_buffer.clear()
        try:
            await db.add_chat_events_batch(session_id, pending)
        except Exception:
            log.warning("[worker] Task #%d failed to flush %d chat events",
                        task_id, len(pending))

    try:
        task_with_session = {**task, "session_id": session_id}
        async for event in run_task(task_id, job, state.PROJECT_DIR,
                                    cancel_event=_cancel_event,
                                    task=task_with_session,
                                    subordinates=subordinates,
                                    workspace_dir=workspace_dir):
            etype = event.get("type")
            if etype == "tool_use":
                _has_tool_use = True

            # Incremental persistence: buffer raw NDJSON, flush by size.
            if etype == "_raw":
                _raw_buffer.append((event["event_type"], event["raw_json"]))
                if len(_raw_buffer) >= RAW_BUFFER_FLUSH_SIZE:
                    await _flush_raw()
                continue

            pubsub.broadcast(task_id, event)

            if etype == "assistant_complete":
                full_response.append(event.get("content", ""))
                # Turn boundary — flush so a crash mid-task loses at most
                # the in-flight turn.
                await _flush_raw()
                continue

            if etype == "session_id":
                # Persist as soon as the CLI announces it (system/init event),
                # not just at the final `result` event. Otherwise a timeout,
                # cancellation, or crash before `result` loses the session ID
                # and Resume becomes impossible.
                cli_sid = event.get("cli_session_id")
                if cli_sid:
                    await db.update_chat_session(session_id, cli_session_id=cli_sid)
            elif etype == "result_meta":
                cli_sid = event.get("cli_session_id")
                if cli_sid:
                    await db.update_chat_session(session_id, cli_session_id=cli_sid)
                _result_meta = {
                    "stop_reason": event.get("stop_reason"),
                    "num_turns": event.get("num_turns"),
                    "cost_usd": event.get("cost_usd"),
                }
            elif etype == "error":
                await _flush_raw()
                await db.add_chat_message(session_id, "system", event.get("message", "error"))

        # End-of-stream flush — covers the tail below the threshold.
        await _flush_raw()
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
        worktree_head = await git.head_hash(workspace_dir) or start_commit

        # Build execution metadata fields for task record
        meta_fields = {}
        if _result_meta.get("stop_reason"):
            meta_fields["stop_reason"] = _result_meta["stop_reason"]
        if _result_meta.get("num_turns") is not None:
            meta_fields["num_turns"] = _result_meta["num_turns"]
        if _result_meta.get("cost_usd") is not None:
            meta_fields["cost_usd"] = _result_meta["cost_usd"]

        if _timed_out:
            await db.transition_trigger(task_id, "timed_out",
                                     result_commit=worktree_head, error="timed out",
                                     **meta_fields)
            await db.cascade_completion(task_id, "timed_out",
                                        result_commit=worktree_head, error="timed out")
            log.info("[worker] Task #%d timed out (worktree=%s, dependents skipped, workspace preserved)",
                     task_id, worktree_head[:8])
            await _maybe_auto_continue(task_id, job, "timed_out", meta_fields, timeout_seconds)
            await _check_governor_trigger(task_id)
        elif _cancelled:
            await db.transition_trigger(task_id, "cancelled",
                                     result_commit=worktree_head, error="cancelled",
                                     **meta_fields)
            await db.cascade_completion(task_id, "cancelled",
                                        result_commit=worktree_head, error="cancelled")
            log.info("[worker] Task #%d cancelled (worktree=%s, workspace preserved)",
                     task_id, worktree_head[:8])
        elif meta_fields.get("stop_reason") == "max_turns":
            # Agent hit turn limit — exhausted, not completed
            max_turns = job["properties"].get("max_turns", 100)
            turns_used = meta_fields.get("num_turns", "?")
            err = f"hit turn limit ({turns_used}/{max_turns})"
            await db.transition_trigger(task_id, "exhausted",
                                     result_commit=worktree_head, error=err,
                                     **meta_fields)
            await db.cascade_completion(task_id, "exhausted",
                                        result_commit=worktree_head, error=err)
            log.info("[worker] Task #%d exhausted (%s/%s turns, worktree=%s, workspace preserved)",
                     task_id, turns_used, max_turns, worktree_head[:8])
            await _maybe_auto_continue(task_id, job, "exhausted", meta_fields, None)
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
        # Flush any buffered raw events before writing the failure response —
        # otherwise the audit log loses the final turn that triggered the error.
        await _flush_raw()
        try:
            response_text = "".join(full_response)
            if response_text:
                await db.add_chat_message(session_id, "assistant", response_text)
            await db.add_chat_message(session_id, "system", f"Error: {e}")
        except Exception as write_err:
            log.warning("[worker] Task #%d failed to write error response: %s", task_id, write_err)
        worktree_head = await git.head_hash(workspace_dir) or start_commit
        await db.transition_trigger(task_id, "failed",
                                 result_commit=worktree_head, error=str(e))
        try:
            await db.cascade_completion(task_id, "failed",
                                        result_commit=worktree_head, error=str(e))
        except Exception as cascade_err:
            log.warning("[worker] Task #%d failed to cascade failure to subordinates: %s", task_id, cascade_err)
        await _check_governor_trigger(task_id)
    finally:
        # Last-resort raw-event flush in case both try and except missed it
        # (e.g. CancelledError mid-loop). The helper swallows its own errors.
        try:
            await _flush_raw()
        except Exception:
            log.exception("[worker] Task #%d raw-event flush failed in finally", task_id)
        # Last-resort: if status is still 'active' (both try and except
        # crashed), force it to failed so tasks can never get stuck.
        final_status = None
        try:
            task_row = await db.get_trigger(task_id)
            if task_row and task_row.get("status") == "active":
                log.error("[worker] Task #%d still active in finally — forcing to failed", task_id)
                await db.transition_trigger(task_id, "failed",
                                         error="internal error: post-processing failed")
                final_status = "failed"
            elif task_row:
                final_status = task_row.get("status")
        except Exception:
            log.exception("[worker] Task #%d CRITICAL: could not transition to failed", task_id)
        # Feed the silent-failure health heuristic. Safe to call even if
        # meta_fields never populated — _is_silent_api_failure returns False
        # when cost_usd is None.
        if final_status:
            _update_health_after_dispatch(_result_meta, _has_tool_use, final_status)
        if watchdog and not watchdog.done():
            watchdog.cancel()
        pubsub.broadcast(task_id, events.done())
        pubsub.cleanup_trigger(task_id)
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


async def _maybe_auto_continue(task_id: int, job: dict, terminal_state: str,
                               meta_fields: dict, timeout_seconds: int | None):
    """Enqueue an auto_continue task when the job opts in and the terminal qualifies.

    Only fires on `exhausted` and `timed_out`. The new task inherits the
    prior dispatch's worktree+branch (the worker recognizes the trigger
    kind in _process_trigger). The continuation throttle in enqueue_trigger
    forces consecutive auto_continues into pending after N=2 — so a
    misconfigured job that keeps overflowing surfaces on the kanban
    instead of burning indefinite cycles.
    """
    if not job["properties"].get("auto_continue"):
        return
    if terminal_state not in ("exhausted", "timed_out"):
        return

    if terminal_state == "exhausted":
        turns = meta_fields.get("num_turns", "?")
        limit = job["properties"].get("max_turns", 100)
        synth = (f"Prior dispatch ended **exhausted** after {turns}/{limit} turns. "
                 f"Worktree preserved with the prior commits. Review what's already "
                 f"there (`git log`, `git diff`) and continue where it stopped.")
    else:
        synth = (f"Prior dispatch ended **timed_out** after {timeout_seconds}s. "
                 f"Worktree preserved with the prior commits. Review what's already "
                 f"there (`git log`, `git diff`) and continue where it stopped.")

    context = await build_trigger_context(
        "auto_continue",
        original_task_id=task_id,
        user_context=synth,
    )
    new_id = await db.enqueue_trigger(
        job["id"], "auto_continue",
        trigger_detail=str(task_id),
        context=context,
    )
    log.info("[worker] Auto-continue: enqueued #%d for task #%d (terminal=%s)",
             new_id, task_id, terminal_state)
    notify()


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
        context = await build_trigger_context(
            "cascade",
            project_dir=state.PROJECT_DIR,
            upstream_name=upstream_name,
            upstream_task_id=task_id,
            start_commit=start_commit,
            result_commit=result_commit,
        )

        new_id = await db.enqueue_trigger(
            job["id"], "cascade",
            trigger_detail=str(completed_job_id),
            context=context,
        )
        log.info("[worker] Cascade: enqueued #%d for job %d (upstream: %d #%d)",
                 new_id, job["id"], completed_job_id, task_id)
        notify()
