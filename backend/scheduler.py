"""Cron-based job scheduler — enqueues tasks on a schedule.

Runs as a background asyncio task alongside the task worker.
Checks job schedules every 30 seconds and enqueues when due.
Stores last-fire timestamps per job in the config table to survive restarts.
"""

import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter

from backend import database as db, git, state, worker
from backend.dispatch import build_trigger_context

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


def _config_key(job_id: str) -> str:
    return f"schedule_last_fire_{job_id}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _set_last_fire(job_id: str, dt: datetime):
    await db.set_config(_config_key(job_id), dt.isoformat())


async def _loop():
    """Main scheduler loop — checks all job schedules periodically."""
    while True:
        try:
            if not state.PROJECT_DIR or not db.DB_PATH:
                await asyncio.sleep(_CHECK_INTERVAL)
                continue

            # Guard the entire tick so close_db() waits for us to finish
            async with db.db_read_guard():
                jobs = await db.list_jobs()
                now = _now_utc()

                last_fire_map = await db.get_config_prefix("schedule_last_fire_")

                for job in jobs:
                    schedule = job["properties"].get("schedule", "")
                    if not schedule or not schedule.strip():
                        continue

                    if not croniter.is_valid(schedule):
                        log.warning("[scheduler] Invalid cron '%s' on job %s", schedule, job["id"])
                        continue

                    if job["properties"].get("running"):
                        continue

                    raw = last_fire_map.get(_config_key(job["id"]))
                    last_fire: datetime | None = None
                    if raw:
                        try:
                            last_fire = datetime.fromisoformat(raw)
                        except ValueError:
                            pass

                    if last_fire is None:
                        await _set_last_fire(job["id"], now)
                        log.info("[scheduler] Initialized schedule for %s: %s", job["id"], schedule)
                        continue

                    cron = croniter(schedule, last_fire)
                    next_fire = cron.get_next(datetime)
                    if next_fire.tzinfo is None:
                        next_fire = next_fire.replace(tzinfo=timezone.utc)

                    if next_fire <= now:
                        head = git.head_hash(state.PROJECT_DIR)

                        log.info("[scheduler] Firing %s (schedule: %s)", job["id"], schedule)
                        await db.enqueue_task(
                            job["id"],
                            "schedule",
                            trigger_detail=schedule,
                            context=build_trigger_context(
                                "schedule", schedule_expr=schedule, commit_hash=head,
                            ),
                        )
                        await _set_last_fire(job["id"], now)

                        worker.notify()

        except asyncio.CancelledError:
            raise
        except RuntimeError as e:
            if "closing" in str(e).lower():
                log.info("[scheduler] Skipping tick — project switch in progress")
            else:
                log.exception("[scheduler] Error in loop")
        except Exception:
            log.exception("[scheduler] Error in loop")

        await asyncio.sleep(_CHECK_INTERVAL)
