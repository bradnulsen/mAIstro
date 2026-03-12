"""Cron-based task scheduler — enqueues dispatches on a schedule.

Runs as a background asyncio task alongside the dispatch worker.
Checks task schedules every 30 seconds and enqueues when due.
Stores last-fire timestamps per task in the config table to survive restarts.
"""

import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter

from backend import database as db

log = logging.getLogger("maistro.scheduler")

_scheduler_task: asyncio.Task | None = None
_CHECK_INTERVAL = 30  # seconds between schedule checks


async def start():
    global _scheduler_task
    _scheduler_task = asyncio.create_task(_loop())
    log.info("[scheduler] Started")


async def stop():
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    log.info("[scheduler] Stopped")


def _config_key(task_id: str) -> str:
    return f"schedule_last_fire_{task_id}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _get_last_fire(task_id: str) -> datetime | None:
    val = await db.get_config(_config_key(task_id))
    if val:
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


async def _set_last_fire(task_id: str, dt: datetime):
    await db.set_config(_config_key(task_id), dt.isoformat())


async def _loop():
    """Main scheduler loop — checks all task schedules periodically."""
    while True:
        try:
            from backend.main import PROJECT_DIR
            if not PROJECT_DIR or not db.DB_PATH:
                await asyncio.sleep(_CHECK_INTERVAL)
                continue

            tasks = await db.list_tasks()
            now = _now_utc()

            for task in tasks:
                schedule = task["properties"].get("schedule", "")
                if not schedule or not schedule.strip():
                    continue

                # Validate cron expression
                if not croniter.is_valid(schedule):
                    log.warning("[scheduler] Invalid cron '%s' on task %s", schedule, task["id"])
                    continue

                # Skip if task is already running or has pending dispatches
                if task["properties"].get("running"):
                    continue

                last_fire = await _get_last_fire(task["id"])

                # Determine if we should fire
                if last_fire is None:
                    # First time — set baseline to now, don't fire immediately
                    await _set_last_fire(task["id"], now)
                    log.info("[scheduler] Initialized schedule for %s: %s", task["id"], schedule)
                    continue

                # Get next fire time after last fire
                cron = croniter(schedule, last_fire)
                next_fire = cron.get_next(datetime)
                # Make timezone-aware if needed
                if next_fire.tzinfo is None:
                    next_fire = next_fire.replace(tzinfo=timezone.utc)

                if next_fire <= now:
                    # Check for pending (not-yet-started) dispatches to avoid piling up
                    pending = await _has_pending_dispatch(task["id"])
                    if pending:
                        log.debug("[scheduler] %s already has pending dispatch, skipping", task["id"])
                        await _set_last_fire(task["id"], now)
                        continue

                    log.info("[scheduler] Firing %s (schedule: %s)", task["id"], schedule)
                    await db.enqueue_dispatch(
                        task["id"],
                        "schedule",
                        trigger_detail=schedule,
                        context=f"Scheduled dispatch ({schedule})",
                    )
                    await _set_last_fire(task["id"], now)

                    # Notify the worker
                    from backend.worker import notify
                    notify()

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[scheduler] Error in loop")

        await asyncio.sleep(_CHECK_INTERVAL)


async def _has_pending_dispatch(task_id: str) -> bool:
    """Check if a task has any pending (not started) dispatches."""
    d = await db.get_db()
    try:
        rows = await d.execute_fetchall(
            "SELECT 1 FROM dispatch_queue WHERE task_id = ? AND started_at IS NULL AND error IS NULL LIMIT 1",
            (task_id,),
        )
        return bool(rows)
    finally:
        await d.close()
