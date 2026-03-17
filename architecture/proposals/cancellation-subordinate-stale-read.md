# Proposal: Cancellation Path — Subordinate Stale-Read Double-Write

## Observation

When a task is cancelled via the cancel route (`POST /api/tasks/{id}/cancel`), two separate code paths both update subordinate tasks, with the second pass operating on a stale in-memory snapshot:

1. **Cancel route** (`queue_routes.py:170-176`) immediately reads current subordinates from DB and batch-updates them: `completed_at=now, error="cancelled"`. This commits to DB.

2. **Worker cancel path** (`worker.py:260-266`) uses the `subordinates` list fetched at task start (line 167). By the time this path runs, those in-memory dicts don't reflect the cancel route's DB update. The filter `if not s.get("completed_at")` operates on stale data and is effectively a no-op — all subordinates are re-updated with the same values.

The double-write is functionally benign: both passes write the same field values. However, it represents a stale-read anti-pattern and means `completed_at` on subordinates is recorded as the worker cleanup timestamp rather than the cancel-route timestamp, which is slightly later.

Additionally, if new tasks are coalesced to a running root between task start and cancellation, the cancel route will capture them (live DB read) but the worker's in-memory list will not.

## Root Cause

The worker fetches subordinates once at task start and holds them in memory for the full task lifecycle. This is efficient for the success path (single read, multiple reuses) but creates a stale window for the cancellation path where a concurrent write has already resolved the same data.

## Proposed Fix

In the worker's `_cancelled` branch, re-fetch subordinates from DB rather than using the stale in-memory list:

```python
elif _cancelled:
    head = git.head_hash(state.PROJECT_DIR)
    now = utcnow()
    # Re-fetch to capture any coalesces added after task start,
    # and to avoid double-writing tasks the cancel route already resolved.
    fresh_subs = await db.get_subordinate_tasks(task_id)
    await db.update_task(task_id, _commit=False, completed_at=now, result_commit=head)
    sub_ids = [s["id"] for s in fresh_subs if not s.get("completed_at")]
    await db.update_tasks_batch(sub_ids, completed_at=now, error="cancelled")
```

This adds one DB query on the cancellation path (already an infrequent event) and eliminates the stale-read window.

## Priority

Low. The double-write is idempotent and the behavioral difference is sub-second timestamp drift. Worthwhile to address alongside any future refactor of the task lifecycle completion paths.
