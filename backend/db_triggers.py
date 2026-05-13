"""Trigger lifecycle: CRUD, state machine, event log, coalescing.

Triggers are atomic units of work — exactly one kind, one context, one job.
They are never mutated after creation; reply and resume create new triggers
that coalesce the original via the `coalesced_id` FK. The status state
machine is authoritative: every transition goes through `transition_trigger`
(or `transition_triggers_batch`) which validates the source→target pair,
writes a `task_events` row, and updates the materialized status column.

Coalescing maintains a depth-1 invariant — enforced by SQLite triggers in
SCHEMA_SQL and re-asserted in code via `_flatten_coalesce` whenever a
trigger is re-parented.

Note: SQL table names (`tasks`, `task_events`, `task_executions`) reflect
Stage 1 of the triggers-and-dispatches refactor — Python identifiers have
been renamed but SQL strings continue to reference the legacy table names.
The schema rename is Stage 2+ (deferred).
"""

import json
import logging

import aiosqlite

from backend.db_core import get_db

log = logging.getLogger("maistro.database")


# ── Schema split: outcome fields live on `task_executions` ────
#
# The tasks table holds atomic identity + queue placement; per-execution
# outcome data lives on a separate row keyed by task_id. Helpers below
# route writes accordingly. Reads use a SELECT fragment that joins the
# execution row so callers see the same dict shape they always have.

_OUTCOME_FIELDS = frozenset({
    "session_id", "start_commit", "result_commit",
    "stop_reason", "num_turns", "cost_usd",
    "started_at", "completed_at", "error",
    "worktree_path", "task_branch", "orphan_stash_ref",
})

# Reusable SELECT fragment that pulls every execution column out of a
# LEFT JOIN aliased as `te`. Used so callers' returned dicts include the
# same keys they had before normalization.
_DISPATCH_SELECT = (
    "te.session_id, te.start_commit, te.result_commit, "
    "te.stop_reason, te.num_turns, te.cost_usd, "
    "te.started_at, te.completed_at, te.error, "
    "te.worktree_path, te.task_branch, te.orphan_stash_ref"
)


async def clear_workspace_pointers(worktree_path: str) -> int:
    """Null worktree_path + task_branch on every dispatch row pointing at a path.

    Use whenever a worktree is physically removed from disk. Resume and
    auto_continue inherit a prior dispatch's worktree, so multiple rows may
    point at the same path — clearing only the current task leaves dangling
    pointers on its ancestors. Path-based is naturally chain-aware: any
    row pointing at this path is stale once the path is gone.

    Returns the number of rows updated (for logging).
    """
    db = await get_db()
    cur = await db.execute(
        "UPDATE task_executions SET worktree_path = NULL, task_branch = NULL WHERE worktree_path = ?",
        (worktree_path,)
    )
    await db.commit()
    return cur.rowcount or 0


async def upsert_dispatch(trigger_id: int, conn=None, _commit: bool = True, **fields):
    """Create or update the per-task execution row.

    Insert-or-update on `task_executions` keyed by task_id. The first call
    for a task (typically at activation) creates the row; later calls
    (terminal handling) update it. Unknown fields raise — outcome fields
    must be in the canonical set (`_OUTCOME_FIELDS`).
    """
    if not fields:
        return
    bad = set(fields) - _OUTCOME_FIELDS
    if bad:
        raise ValueError(f"upsert_dispatch: unknown outcome fields {bad}")
    if conn is None:
        conn = await get_db()
    cols = list(fields.keys())
    placeholders = ", ".join("?" * (1 + len(cols)))
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO task_executions (task_id, {', '.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(task_id) DO UPDATE SET {set_clause}"
    )
    await conn.execute(sql, [trigger_id] + [fields[c] for c in cols])
    if _commit:
        await conn.commit()


_CONTINUATION_TRIGGERS = {"self_requeue", "auto_continue"}
_CONTINUATION_THROTTLE_N = {"self_requeue": 5, "auto_continue": 2}


async def enqueue_trigger(job_id: int, trigger: str,
                       trigger_detail: str | None = None,
                       context: str | None = None) -> int:
    """Enqueue an atomic task. Coalescing links via coalesced_id instead of mutating data.

    Coalescing rules:
    - 'schedule' always coalesces globally
    - 'commit', 'dependency', and 'agent' coalesce with other pending tasks of the same type
    - coalesce_tasks=true coalesces globally
    - All other triggers (manual, resume, reply, self_requeue, auto_continue) never coalesce

    Continuation throttle: when `trigger` is `self_requeue` or `auto_continue`,
    if the last N tasks on the same job are all of that same kind, the new
    task is forced into `pending` (skipping `auto_dispatch`) and an extra
    `continuation_throttled` event is emitted. N differs per kind — voluntary
    self-requeue tolerates longer chains; involuntary auto-continue trips at 2
    because two-in-a-row almost always means the job is misconfigured.
    """
    db = await get_db()

    # Fetch only the two properties needed
    prop_rows = await db.execute_fetchall(
        "SELECT key, value FROM job_properties WHERE job_id = ? AND key IN ('coalesce_tasks', 'require_approval')",
        (job_id,)
    )
    job_props = {r["key"]: r["value"] for r in prop_rows}
    coalesce_global = (trigger == "schedule") or (job_props.get("coalesce_tasks", "").lower() == "true")
    coalesce_same_type = trigger in ("commit", "cascade", "agent")

    # Approval gate: manual tasks bypass, others check job property
    approval = None
    if trigger != "manual" and job_props.get("require_approval", "").lower() == "true":
        approval = "pending"

    # Continuation throttle: same kind N times in a row forces pending
    throttled = False
    if trigger in _CONTINUATION_TRIGGERS:
        n = _CONTINUATION_THROTTLE_N[trigger]
        recent_rows = await db.execute_fetchall(
            "SELECT trigger FROM tasks WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
            (job_id, n)
        )
        if len(recent_rows) == n and all(r["trigger"] == trigger for r in recent_rows):
            throttled = True

    # Auto-queueing: when enabled, new tasks skip pending and go directly to queued
    from backend.state import utcnow
    from backend.db_config import get_config
    queued_at = None
    status = "pending"
    if not throttled:
        auto_queue = await get_config("queue_auto_dispatch")
        if auto_queue == "true":
            queued_at = utcnow()
            status = "queued"

    # Insert the atomic task (not committed yet — coalesce update shares the transaction)
    now = utcnow()
    cursor = await db.execute(
        """INSERT INTO tasks (job_id, status, trigger, trigger_detail, context, approval, queued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, status, trigger, trigger_detail, context, approval, queued_at)
    )
    new_id = cursor.lastrowid

    # Emit lifecycle events: dispatched (and queued if auto-queued)
    await db.execute(
        "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, 'dispatched', ?, ?)",
        (new_id, trigger, now)
    )
    if throttled:
        await db.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, 'continuation_throttled', ?, ?)",
            (new_id, f"{trigger}:{_CONTINUATION_THROTTLE_N[trigger]}", now)
        )
    if queued_at:
        await db.execute(
            "INSERT INTO task_events (task_id, event, created_at) VALUES (?, 'queued', ?)",
            (new_id, queued_at)
        )

    # Coalesce: find an existing pending root and link this task to it
    root_id = None
    if coalesce_global or coalesce_same_type:
        query = f"""SELECT id FROM tasks
                   WHERE job_id = ? AND status IN {PRE_EXECUTION_STATUSES_SQL}
                     AND coalesced_id IS NULL AND id != ?"""
        params: list = [job_id, new_id]
        if coalesce_same_type and not coalesce_global:
            query += " AND trigger = ?"
            params.append(trigger)
        query += " ORDER BY created_at ASC LIMIT 1"
        rows = await db.execute_fetchall(query, params)
        if rows:
            root_id = rows[0]["id"]
            await db.execute(
                "UPDATE tasks SET coalesced_id = ? WHERE id = ?",
                (root_id, new_id)
            )

    # Single commit: insert + optional coalesce link land atomically
    await db.commit()
    return root_id if root_id is not None else new_id


async def get_trigger(trigger_id: int) -> dict | None:
    """Read a task with its execution row (if it ran) joined.

    Identity-mode read: returns this task's own data without coalescing
    fall-through. Use `get_task_resolved` when you want subordinates to
    inherit their root's execution outcome.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        f"""SELECT t.*, j.name as job_name,
                  {_DISPATCH_SELECT}
           FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           LEFT JOIN task_executions te ON te.task_id = t.id
           WHERE t.id = ?""",
        (trigger_id,)
    )
    if not rows:
        return None
    task = dict(rows[0])
    apply_events(task, await get_trigger_events(trigger_id, db))
    return task


async def get_trigger_resolved(trigger_id: int) -> dict | None:
    """Read a task through the tasks_resolved view.

    Outcome columns (session_id, start_commit, result_commit, num_turns,
    cost_usd, stop_reason, started_at, completed_at, error) fall through
    to the root for coalesced subordinates. Use this when the caller cares
    about the *effective* outcome the user/agent saw — e.g. output, diff,
    and outcome endpoints, which would otherwise return empty for any
    subordinate that didn't run independently.

    Use plain get_task when the caller cares about per-task identity
    (e.g. queue listing, where the per-task null is the truth).
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT t.*, j.name as job_name FROM tasks_resolved t JOIN jobs j ON j.id = t.job_id WHERE t.id = ?",
        (trigger_id,)
    )
    if not rows:
        return None
    task = dict(rows[0])
    apply_events(task, await get_trigger_events(trigger_id, db))
    return task


async def get_agent_dispatch_depth(trigger_id: int) -> int:
    """Trace the agent dispatch chain back from a task and return its depth.

    Each agent-triggered task has trigger_detail of format 'source_job_id#source_task_id'.
    Follows the chain until a non-agent trigger is found or the chain breaks.
    Returns the number of agent dispatch hops (0 if the task itself is not agent-triggered).
    """
    conn = await get_db()
    depth = 0
    current_id = trigger_id
    seen = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        rows = await conn.execute_fetchall(
            "SELECT trigger, trigger_detail FROM tasks WHERE id = ?",
            (current_id,)
        )
        if not rows:
            break
        row = rows[0]
        if row["trigger"] != "agent":
            break
        depth += 1
        # trigger_detail is 'source_job_id#source_task_id'
        detail = row["trigger_detail"] or ""
        parts = detail.rsplit("#", 1)
        if len(parts) == 2 and parts[1].isdigit():
            current_id = int(parts[1])
        else:
            break
    return depth


async def get_trigger_queue(limit: int = 50) -> list[dict]:
    """Return the most-recent N root triggers with execution outcome and
    a subordinate count attached.

    Reads roots through `tasks_resolved` so outcome columns mirror what
    the detail drawer sees. Subordinate counts come from a separate keyed
    aggregate so the queue scan doesn't have to GROUP BY across the full
    coalesce graph (and so the partial index `idx_tasks_roots_recent` can
    actually back the ORDER BY).
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.*, j.name as job_name
           FROM tasks_resolved t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.coalesced_id IS NULL
           ORDER BY t.created_at DESC LIMIT ?""",
        (limit,)
    )
    if not rows:
        return []
    triggers = [dict(r) for r in rows]

    ids = [t["id"] for t in triggers]
    placeholders = ",".join("?" * len(ids))
    sub_rows = await db.execute_fetchall(
        f"SELECT coalesced_id, COUNT(*) as cnt FROM tasks "
        f"WHERE coalesced_id IN ({placeholders}) GROUP BY coalesced_id",
        ids,
    )
    sub_counts = {r["coalesced_id"]: r["cnt"] for r in sub_rows}
    for t in triggers:
        t["subordinate_count"] = sub_counts.get(t["id"], 0)
    return triggers


async def get_triggers_by_result_commits(commits: list[str]) -> list[dict]:
    """Return root triggers whose result_commit matches any of the given hashes.

    Keyed lookup for the activity feed: instead of pulling a fixed window
    of recent triggers and filtering in Python, hand in the commit hashes
    the feed actually rendered and let the DB scan only those rows. Reads
    through `tasks_resolved` so outcome columns mirror the queue/detail
    shape, with `subordinate_count` attached the same way `get_trigger_queue`
    does it.
    """
    if not commits:
        return []
    db = await get_db()
    placeholders = ",".join("?" * len(commits))
    rows = await db.execute_fetchall(
        f"""SELECT t.*, j.name as job_name
            FROM tasks_resolved t
            JOIN jobs j ON j.id = t.job_id
            WHERE t.coalesced_id IS NULL
              AND t.result_commit IN ({placeholders})""",
        commits,
    )
    if not rows:
        return []
    triggers = [dict(r) for r in rows]

    ids = [t["id"] for t in triggers]
    id_placeholders = ",".join("?" * len(ids))
    sub_rows = await db.execute_fetchall(
        f"SELECT coalesced_id, COUNT(*) as cnt FROM tasks "
        f"WHERE coalesced_id IN ({id_placeholders}) GROUP BY coalesced_id",
        ids,
    )
    sub_counts = {r["coalesced_id"]: r["cnt"] for r in sub_rows}
    for t in triggers:
        t["subordinate_count"] = sub_counts.get(t["id"], 0)
    return triggers


async def get_oldest_queued_trigger() -> dict | None:
    """Get the highest-priority queued task (skips pending, approval-pending, and subordinates)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.*, j.name as job_name FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.status = 'queued'
             AND (t.approval IS NULL OR t.approval = 'approved')
             AND t.coalesced_id IS NULL
           ORDER BY t.sort_order IS NULL, t.sort_order ASC, t.created_at ASC
           LIMIT 1"""
    )
    return dict(rows[0]) if rows else None


async def update_trigger(trigger_id: int, _commit: bool = True, **kwargs):
    """Update fields on a task, routing outcome fields to its execution row.

    Tasks-table columns and task_executions columns get directed to the
    right table based on `_OUTCOME_FIELDS` membership.
    """
    db = await get_db()
    exec_kwargs = {k: kwargs[k] for k in kwargs if k in _OUTCOME_FIELDS}
    task_kwargs = {k: kwargs[k] for k in kwargs if k not in _OUTCOME_FIELDS}
    if task_kwargs:
        sets = ", ".join(f"{k} = ?" for k in task_kwargs)
        vals = list(task_kwargs.values()) + [trigger_id]
        await db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
    if exec_kwargs:
        await upsert_dispatch(trigger_id, conn=db, _commit=False, **exec_kwargs)
    if _commit:
        await db.commit()


async def update_triggers_batch(trigger_ids: list[int], **kwargs):
    """Update multiple tasks with the same field values in a single statement.

    Routes outcome fields to per-task execution rows. Always commits —
    even when trigger_ids is empty — to flush any prior uncommitted writes
    in the same transaction.
    """
    db = await get_db()
    exec_kwargs = {k: kwargs[k] for k in kwargs if k in _OUTCOME_FIELDS}
    task_kwargs = {k: kwargs[k] for k in kwargs if k not in _OUTCOME_FIELDS}
    if trigger_ids:
        if task_kwargs:
            sets = ", ".join(f"{k} = ?" for k in task_kwargs)
            placeholders = ",".join("?" * len(trigger_ids))
            vals = list(task_kwargs.values()) + list(trigger_ids)
            await db.execute(f"UPDATE tasks SET {sets} WHERE id IN ({placeholders})", vals)
        if exec_kwargs:
            for tid in trigger_ids:
                await upsert_dispatch(tid, conn=db, _commit=False, **exec_kwargs)
    await db.commit()


# ── Trigger Status State Machine ──────────────────────────

# Legal transitions: (current_status, new_status) -> allowed
LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    ("pending", "queued"),
    ("pending", "cancelled"),
    ("pending", "rejected"),
    ("queued", "pending"),
    ("queued", "active"),
    ("queued", "cancelled"),
    ("active", "completed"),
    ("active", "exhausted"),
    ("active", "failed"),
    ("active", "cancelled"),
    ("active", "timed_out"),
    ("active", "interrupted"),
}

TERMINAL_STATUSES = frozenset({"completed", "exhausted", "failed", "cancelled", "interrupted", "timed_out", "rejected"})
# Terminal states where a per-task worktree is preserved for inspection.
# Excludes `completed` (worktree is removed after integration) and
# `rejected` (never executed, no worktree). Used to gate workspace-discard
# affordances so an active or already-cleaned task can't have its worktree yanked.
NON_SUCCESS_TERMINAL_STATUSES = frozenset({"exhausted", "failed", "cancelled", "interrupted", "timed_out"})

# Pre-execution: still in the queue, not yet running. Coalesce / merge / split /
# uncoalesce / restore are all "pending-or-queued column" operations.
PRE_EXECUTION_STATUSES = frozenset({"pending", "queued"})

# SQL fragments derived from the sets above so queries don't drift from the
# Python source of truth. Build once at module load; safe to f-string into
# query text because the contents are hardcoded literals, not user input.
def _status_set_sql(statuses: frozenset[str]) -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(statuses)) + ")"

TERMINAL_STATUSES_SQL = _status_set_sql(TERMINAL_STATUSES)
PRE_EXECUTION_STATUSES_SQL = _status_set_sql(PRE_EXECUTION_STATUSES)

# Map status → event name for task_events writes.
_STATUS_TO_EVENT = {
    "pending": "restored",
    "queued": "queued",
    "active": "activated",
    "completed": "completed",
    "exhausted": "exhausted",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "interrupted": "interrupted",
    "rejected": "rejected",
}

# Reverse mapping: event name → status.
# Includes both current and legacy event names for backward compat with pre-migration data.
_EVENT_TO_STATUS = {
    # Current event names
    "dispatched": "pending",
    "restored": "pending",
    "queued": "queued",
    "activated": "active",
    "completed": "completed",
    "exhausted": "exhausted",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "interrupted": "interrupted",
    "rejected": "rejected",
    # Legacy event names (pre-migration data)
    "created": "pending",
    "unqueued": "pending",
    "active": "active",
}

_STATUS_EVENTS = frozenset(_EVENT_TO_STATUS.keys())


async def get_trigger_status_from_events(trigger_id: int, conn=None) -> str | None:
    """Derive current task status from the latest lifecycle event.

    Returns None if the task has no events.
    """
    if conn is None:
        conn = await get_db()
    placeholders = ",".join(f"'{e}'" for e in _STATUS_EVENTS)
    rows = await conn.execute_fetchall(
        f"SELECT event FROM task_events WHERE task_id = ? AND event IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (trigger_id,)
    )
    if not rows:
        return None
    return _EVENT_TO_STATUS[rows[0]["event"]]


async def _resolve_trigger_status(trigger_id: int, conn) -> str:
    """Get current task status, backfilling from the status column if no events exist.

    Tasks created before task_events was introduced have no events. This
    synthesises the missing event so subsequent transitions work normally.
    Raises ValueError if the task doesn't exist.
    """
    from backend.state import utcnow
    status = await get_trigger_status_from_events(trigger_id, conn)
    if status is not None:
        return status
    rows = await conn.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (trigger_id,))
    if not rows:
        raise ValueError(f"Trigger #{trigger_id} not found")
    status = rows[0]["status"]
    synth_event = _STATUS_TO_EVENT.get(status, status)
    await conn.execute(
        "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
        (trigger_id, synth_event, "backfilled", utcnow())
    )
    log.warning("[database] Trigger #%d had no events — backfilled from status=%s", trigger_id, status)
    return status

# Map status to the timestamp column that should be set on transition.
# `queued_at` lives on tasks (queue placement); `started_at` and
# `completed_at` live on task_executions (execution data).
_STATUS_TIMESTAMP = {
    "queued": "queued_at",
    "active": "started_at",
    "completed": "completed_at",
    "exhausted": "completed_at",
    "failed": "completed_at",
    "cancelled": "completed_at",
    "timed_out": "completed_at",
    "interrupted": "completed_at",
    "rejected": "completed_at",
}

_TRIGGER_TIMESTAMP_COLS = frozenset({"queued_at"})  # remaining on tasks
# Other timestamps (started_at, completed_at) are routed to task_executions
# via the _OUTCOME_FIELDS membership check.


def _build_event_detail(new_status: str, fields: dict) -> str | None:
    """Extract event detail from transition fields for the audit log.

    Selects the most meaningful field for the event type: error message for
    failures, session_id for activation, result_commit for completion.
    """
    if new_status in ("failed", "cancelled", "timed_out", "interrupted", "exhausted"):
        return fields.get("error")
    if new_status == "active":
        parts = {}
        if "session_id" in fields:
            parts["session_id"] = fields["session_id"]
        if "start_commit" in fields:
            parts["start_commit"] = fields["start_commit"]
        return json.dumps(parts) if parts else None
    if new_status == "completed":
        return fields.get("result_commit")
    return None


async def transition_trigger(trigger_id: int, new_status: str, _commit: bool = True, **fields):
    """Transition a task to a new status with validation.

    All status changes must go through this function. Status + queue
    placement go to `tasks`; execution-related fields (start_commit,
    result_commit, session_id, num_turns, etc.) are routed to the per-task
    `task_executions` row. The lifecycle event (source of truth) is
    written to `task_events`. The full sequence is wrapped in a single
    transaction with explicit rollback on failure — partial commits used
    to leave the events log diverged from the column when an UPDATE
    failed mid-transition.

    Raises ValueError if the transition is illegal.
    """
    conn = await get_db()
    current_status = await _resolve_trigger_status(trigger_id, conn)

    if (current_status, new_status) not in LEGAL_TRANSITIONS:
        raise ValueError(
            f"Illegal transition for trigger #{trigger_id}: {current_status} → {new_status}"
        )

    from backend.state import utcnow

    # Split fields by destination table.
    exec_fields = {k: fields[k] for k in fields if k in _OUTCOME_FIELDS}
    task_fields = {k: fields[k] for k in fields if k not in _OUTCOME_FIELDS}

    # Apply per-status defaults to the right table.
    ts_col = _STATUS_TIMESTAMP.get(new_status)
    ts_value = None
    if ts_col:
        if ts_col in _OUTCOME_FIELDS:
            ts_value = exec_fields.pop(ts_col, utcnow())
            exec_fields[ts_col] = ts_value
        else:
            ts_value = task_fields.pop(ts_col, utcnow())
            task_fields[ts_col] = ts_value

    if new_status == "pending":
        task_fields["queued_at"] = None

    # Build the task-row update (status is always written to materialize the cache).
    task_updates = {"status": new_status, **task_fields}

    event_type = _STATUS_TO_EVENT[new_status]
    event_detail = _build_event_detail(new_status, fields)
    event_ts = ts_value or utcnow()

    try:
        # Event log: the source of truth.
        await conn.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (trigger_id, event_type, event_detail, event_ts)
        )

        # Materialized status + queue placement on tasks.
        sets = ", ".join(f"{k} = ?" for k in task_updates)
        vals = list(task_updates.values()) + [trigger_id]
        await conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)

        # Execution data on its own row (insert-or-update on task_id).
        if exec_fields:
            await upsert_dispatch(trigger_id, conn=conn, _commit=False, **exec_fields)

        if _commit:
            await conn.commit()
    except Exception:
        # Roll back the partial transaction so the event log doesn't diverge
        # from the materialized task row on a mid-flight failure (the bug
        # that originally motivated this rewrite).
        try:
            await conn.rollback()
        except Exception:
            log.exception("[database] rollback after transition_trigger failure also failed")
        raise


async def transition_triggers_batch(trigger_ids: list[int], new_status: str, **fields):
    """Transition multiple tasks to the same new status.

    Skips validation per-task for performance — caller is responsible for
    ensuring all tasks are in a valid source state. Used for subordinate
    cascades. Outcome fields are routed to per-task `task_executions`
    rows; status + queue placement are written to `tasks`. Wrapped in a
    transaction with rollback on failure.
    """
    if not trigger_ids:
        return
    conn = await get_db()
    from backend.state import utcnow
    now = utcnow()

    exec_fields = {k: fields[k] for k in fields if k in _OUTCOME_FIELDS}
    task_fields = {k: fields[k] for k in fields if k not in _OUTCOME_FIELDS}

    ts_col = _STATUS_TIMESTAMP.get(new_status)
    ts_value = None
    if ts_col:
        if ts_col in _OUTCOME_FIELDS:
            ts_value = exec_fields.pop(ts_col, now)
            exec_fields[ts_col] = ts_value
        else:
            ts_value = task_fields.pop(ts_col, now)
            task_fields[ts_col] = ts_value

    task_updates = {"status": new_status, **task_fields}

    event_type = _STATUS_TO_EVENT[new_status]
    event_detail = _build_event_detail(new_status, fields)
    event_ts = ts_value or now

    try:
        await conn.executemany(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            [(tid, event_type, event_detail, event_ts) for tid in trigger_ids]
        )

        sets = ", ".join(f"{k} = ?" for k in task_updates)
        placeholders = ",".join("?" * len(trigger_ids))
        vals = list(task_updates.values()) + list(trigger_ids)
        await conn.execute(f"UPDATE tasks SET {sets} WHERE id IN ({placeholders})", vals)

        if exec_fields:
            for tid in trigger_ids:
                await upsert_dispatch(tid, conn=conn, _commit=False, **exec_fields)

        await conn.commit()
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            log.exception("[database] rollback after transition_triggers_batch failure also failed")
        raise


# ── Trigger Event Queries ─────────────────────────────────

async def get_trigger_events(trigger_id: int, conn=None) -> list[dict]:
    """Full event chain for a task, ordered chronologically.

    This is the canonical read — every event the task has ever experienced,
    in the order it happened. The chain is append-only and immutable.
    """
    if conn is None:
        conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT event, detail, created_at FROM task_events WHERE task_id = ? ORDER BY id ASC",
        (trigger_id,)
    )
    return [dict(r) for r in rows]


async def get_trigger_events_batch(trigger_ids: list[int], conn=None) -> dict[int, list[dict]]:
    """Full event chains for multiple triggers, keyed by trigger_id.

    Same as get_task_events but batched — one query instead of N.
    """
    if not trigger_ids:
        return {}
    if conn is None:
        conn = await get_db()
    placeholders = ",".join("?" * len(trigger_ids))
    rows = await conn.execute_fetchall(
        f"SELECT task_id, event, detail, created_at FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id ASC",
        trigger_ids
    )
    result: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        tid = d.pop("task_id")
        result.setdefault(tid, []).append(d)
    return result


async def prune_task_events(retention_days: int) -> int:
    """Drop task_events rows for terminal triggers whose history has aged out.

    Predicate: trigger is in terminal status AND its most recent event
    is older than `retention_days`. Since events are append-only and
    monotonic, "no event newer than the cutoff" means "all events are
    older than the cutoff" — pruning takes the whole chain at once.

    Trade: the dashboard timeline derives start/end from these events,
    so pruned triggers fall off the timeline. The `tasks.status` column
    is the materialized cache of the latest event, so queue/listing
    queries are unaffected. Caller decides cadence (typically once per
    startup). Opt-in via `task_events_retention_days` (default 0).
    """
    if retention_days <= 0:
        return 0
    db = await get_db()
    cur = await db.execute(
        f"""DELETE FROM task_events
            WHERE task_id IN (
                SELECT t.id FROM tasks t
                WHERE t.status IN {TERMINAL_STATUSES_SQL}
                  AND NOT EXISTS (
                      SELECT 1 FROM task_events e
                      WHERE e.task_id = t.id
                        AND e.created_at >= datetime('now', ?)
                  )
            )""",
        (f"-{retention_days} days",),
    )
    await db.commit()
    return cur.rowcount or 0


_TERMINAL_EVENTS = frozenset({"completed", "exhausted", "failed", "cancelled", "timed_out", "interrupted", "rejected"})


def status_from_events(events: list[dict]) -> str | None:
    """Derive current status from an ordered event list (ascending by id).

    Returns None if no lifecycle events are present.
    """
    status = None
    for e in events:
        mapped = _EVENT_TO_STATUS.get(e["event"])
        if mapped is not None:
            status = mapped
    return status


def apply_events(task: dict, events: list[dict]) -> None:
    """Overlay event-derived fields onto a task dict (mutates in place).

    Sets status, timestamps, durations, and the event chain itself.
    This is the single point where event data is materialized onto a task —
    every read path should call this rather than doing ad-hoc derivation.
    """
    task["events"] = events
    event_status = status_from_events(events)
    if event_status is not None:
        task["status"] = event_status
    task.update(timestamps_from_events(events))
    task["durations"] = compute_durations_from_events(events)


def _parse_event_timestamps(events: list[dict]) -> tuple:
    """Extract lifecycle timestamps from an ordered event list.

    Returns (created_at, queued_at, active_at, terminal_at) — each a datetime
    string or None.
    """
    created_at = None
    queued_at = None
    active_at = None
    terminal_at = None

    for e in events:
        evt = e["event"]
        ts = e["created_at"]
        if not isinstance(ts, str):
            continue  # skip bad data (e.g. integer queue positions from v4 migration)
        if evt in ("dispatched", "created") and created_at is None:
            created_at = ts
        elif evt == "queued":
            queued_at = ts
        elif evt in ("activated", "active"):
            active_at = ts
        elif evt in _TERMINAL_EVENTS:
            terminal_at = ts

    return created_at, queued_at, active_at, terminal_at


def timestamps_from_events(events: list[dict]) -> dict:
    """Derive the same timestamps as the legacy columns from the event log.

    Returns dict with queued_at, started_at, completed_at — matching the column
    names so the frontend doesn't need to change.
    """
    created_at, queued_at, active_at, terminal_at = _parse_event_timestamps(events)
    return {
        "queued_at": queued_at,
        "started_at": active_at,
        "completed_at": terminal_at,
    }


def compute_durations_from_events(events: list[dict]) -> dict:
    """Compute execution duration, queue wait, and total lifecycle from an event list.

    Returns a dict with keys: execution_duration, queue_wait, total_duration (all in seconds,
    None if the relevant event pair is missing).
    """
    created_at, queued_at, active_at, terminal_at = _parse_event_timestamps(events)

    def _diff_secs(a, b):
        if a is None or b is None:
            return None
        from datetime import datetime
        if isinstance(a, str):
            a = datetime.fromisoformat(a)
        if isinstance(b, str):
            b = datetime.fromisoformat(b)
        return max(0, (b - a).total_seconds())

    return {
        "execution_duration": _diff_secs(active_at, terminal_at),
        "queue_wait": _diff_secs(queued_at, active_at),
        "total_duration": _diff_secs(created_at, terminal_at),
    }


async def transfer_trigger(trigger_id: int, to_queued: bool):
    """Move a task between pending and queued columns. Sets or clears queued_at.
    Also transfers subordinate tasks to maintain group cohesion."""
    from backend.state import utcnow
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, coalesced_id FROM tasks WHERE id = ?",
        (trigger_id,)
    )
    if not rows:
        raise ValueError("Trigger not found")
    task = rows[0]
    current_status = await _resolve_trigger_status(trigger_id, db)
    if current_status not in PRE_EXECUTION_STATUSES:
        raise ValueError("Can only transfer pre-execution triggers")
    if task["coalesced_id"] is not None:
        raise ValueError("Cannot transfer a subordinate — transfer its root instead")

    now = utcnow()
    new_status = "queued" if to_queued else "pending"
    queued_at = now if to_queued else None
    event_type = "queued" if to_queued else "restored"

    # Transfer root and all subordinates; clear sort_order (append to end of target column)
    await db.execute(
        "UPDATE tasks SET status = ?, queued_at = ?, sort_order = NULL WHERE id = ?",
        (new_status, queued_at, trigger_id)
    )
    # Dual-write: event for the root task
    await db.execute(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
        (trigger_id, event_type, now)
    )

    # Transfer subordinates
    sub_rows = await db.execute_fetchall(
        f"SELECT id FROM tasks WHERE coalesced_id = ? AND status IN {PRE_EXECUTION_STATUSES_SQL}",
        (trigger_id,)
    )
    await db.execute(
        f"UPDATE tasks SET status = ?, queued_at = ? WHERE coalesced_id = ? AND status IN {PRE_EXECUTION_STATUSES_SQL}",
        (new_status, queued_at, trigger_id)
    )
    # Dual-write: events for subordinates
    if sub_rows:
        await db.executemany(
            "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
            [(r["id"], event_type, now) for r in sub_rows]
        )

    await db.commit()
    return trigger_id


async def transfer_all_triggers(to_queued: bool) -> int:
    """Batch transfer: all pending→queued or all queued→pending.

    Skips subordinate tasks — they follow their root.
    Returns count of tasks transferred.
    """
    from backend.state import utcnow
    conn = await get_db()
    now = utcnow()

    source_status = "pending" if to_queued else "queued"
    target_status = "queued" if to_queued else "pending"
    queued_at = now if to_queued else None
    event_type = "queued" if to_queued else "restored"

    # Find root tasks (not subordinates) in the source status
    rows = await conn.execute_fetchall(
        "SELECT id FROM tasks WHERE status = ? AND coalesced_id IS NULL",
        (source_status,)
    )
    if not rows:
        return 0

    root_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(root_ids))

    # Transfer roots
    await conn.execute(
        f"UPDATE tasks SET status = ?, queued_at = ?, sort_order = NULL "
        f"WHERE id IN ({placeholders})",
        [target_status, queued_at] + root_ids
    )

    # Transfer subordinates of those roots
    sub_rows = await conn.execute_fetchall(
        f"SELECT id FROM tasks WHERE coalesced_id IN ({placeholders}) "
        f"AND status = ?",
        root_ids + [source_status]
    )
    if sub_rows:
        sub_ids = [s["id"] for s in sub_rows]
        sub_ph = ",".join("?" * len(sub_ids))
        await conn.execute(
            f"UPDATE tasks SET status = ?, queued_at = ? "
            f"WHERE id IN ({sub_ph})",
            [target_status, queued_at] + sub_ids
        )

    # Dual-write events
    all_ids = root_ids + [s["id"] for s in sub_rows]
    await conn.executemany(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, ?, ?)",
        [(tid, event_type, now) for tid in all_ids]
    )

    await conn.commit()
    return len(root_ids)


async def reorder_triggers(trigger_ids: list[int]):
    """Set explicit sort_order on pending tasks to control execution priority."""
    db = await get_db()
    await db.executemany(
        f"UPDATE tasks SET sort_order = ? WHERE id = ? AND status IN {PRE_EXECUTION_STATUSES_SQL}",
        [(i, tid) for i, tid in enumerate(trigger_ids)],
    )
    await db.commit()


# ── Coalescing ────────────────────────────────────────────

async def _flatten_coalesce(conn: aiosqlite.Connection, trigger_id: int, new_root_id: int):
    """Re-point any subordinates of trigger_id to new_root_id instead.

    This must run BEFORE setting trigger_id.coalesced_id = new_root_id —
    otherwise the depth-1 trigger fires on the transient depth-2 state
    where trigger_id is a subordinate but still has its own subordinates.
    """
    await conn.execute(
        "UPDATE tasks SET coalesced_id = ? WHERE coalesced_id = ?",
        (new_root_id, trigger_id)
    )


async def coalesce_under(trigger_id: int, new_root_id: int):
    """Coalesce trigger_id (and any of its subordinates) under new_root_id.

    Order matters: flatten first (subordinates re-point to new root) so
    that when trigger_id itself is demoted to a subordinate, it has no
    subordinates of its own. Preserves depth-1 at every intermediate state.
    """
    conn = await get_db()
    await _flatten_coalesce(conn, trigger_id, new_root_id)
    await conn.execute(
        "UPDATE tasks SET coalesced_id = ? WHERE id = ?",
        (new_root_id, trigger_id)
    )
    await conn.commit()


async def get_subordinate_triggers(root_id: int) -> list[dict]:
    """Return all tasks with coalesced_id pointing to root_id."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.*, j.name as job_name FROM tasks t
           JOIN jobs j ON j.id = t.job_id
           WHERE t.coalesced_id = ?""",
        (root_id,)
    )
    return [dict(r) for r in rows]


async def cascade_completion(root_id: int, terminal_status: str, **fields) -> list[int]:
    """Apply terminal_status to subordinates of root_id that are not yet terminal.

    Re-queries subordinates fresh at completion time so any tasks that were
    coalesced during execution (e.g. commit-watch auto-coalesces while the
    root was running) also get the terminal cascade. Filters out subordinates
    that are already in a terminal state to avoid spurious transitions.

    Returns the list of subordinate IDs that were transitioned.
    """
    fresh_subs = await get_subordinate_triggers(root_id)
    sub_ids = [s["id"] for s in fresh_subs if s.get("status") not in TERMINAL_STATUSES]
    if sub_ids:
        await transition_triggers_batch(sub_ids, terminal_status, **fields)
    return sub_ids


async def merge_triggers(trigger_ids: list[int]) -> int:
    """Merge pending same-job tasks. Returns root task ID."""
    if len(trigger_ids) < 2:
        raise ValueError("Need at least 2 triggers to merge")

    db = await get_db()
    placeholders = ",".join("?" * len(trigger_ids))
    rows = await db.execute_fetchall(
        f"""SELECT id, job_id, approval, coalesced_id, created_at
            FROM tasks WHERE id IN ({placeholders})
            ORDER BY created_at ASC""",
        trigger_ids,
    )

    if len(rows) != len(trigger_ids):
        raise ValueError("Some trigger IDs not found")

    events_by_task = await get_trigger_events_batch(trigger_ids, db)

    job_ids = set()
    for r in rows:
        task_status = status_from_events(events_by_task.get(r["id"], []))
        if task_status != "pending":
            raise ValueError(f"Trigger #{r['id']} is not pending — merge is a pending-column operation")
        if r["approval"] == "pending":
            raise ValueError(f"Trigger #{r['id']} is pending approval")
        if r["coalesced_id"] is not None:
            raise ValueError(f"Trigger #{r['id']} is already a subordinate")
        job_ids.add(r["job_id"])

    if len(job_ids) > 1:
        raise ValueError("Cannot merge triggers from different jobs")

    root_id = rows[0]["id"]
    sub_ids = [r["id"] for r in rows[1:]]

    # Flatten first: any pre-existing subordinates of the soon-to-be-merged
    # tasks must re-point to the new root before those tasks themselves
    # become subordinates. Preserves depth-1 at every intermediate state.
    for sid in sub_ids:
        await _flatten_coalesce(db, sid, root_id)

    sub_placeholders = ",".join("?" * len(sub_ids))
    await db.execute(
        f"UPDATE tasks SET coalesced_id = ? WHERE id IN ({sub_placeholders})",
        [root_id] + sub_ids,
    )

    await db.commit()
    return root_id


async def split_trigger(root_id: int) -> list[int]:
    """Split a root task — make all subordinates independent again."""
    db = await get_db()

    root_rows = await db.execute_fetchall(
        "SELECT id, job_id, coalesced_id FROM tasks WHERE id = ?",
        (root_id,)
    )
    if not root_rows:
        raise ValueError("Trigger not found")
    root = root_rows[0]
    if root["coalesced_id"] is not None:
        raise ValueError("Trigger is a subordinate, not a root")
    root_status = await get_trigger_status_from_events(root_id, db)
    if root_status != "pending":
        raise ValueError("Can only split pending triggers — split is a pending-column operation")

    sub_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE coalesced_id = ?", (root_id,)
    )
    if not sub_rows:
        raise ValueError("No subordinate triggers to split")

    sub_ids = [r["id"] for r in sub_rows]

    # Look up the job's current require_approval setting
    prop_rows = await db.execute_fetchall(
        "SELECT value FROM job_properties WHERE job_id = ? AND key = 'require_approval'",
        (root["job_id"],)
    )
    require_approval = prop_rows[0]["value"] == "true" if prop_rows else False
    approval_val = "pending" if require_approval else None

    from backend.state import utcnow
    now = utcnow()
    # Split-off tasks always land in pending (split is a pending-column operation)
    placeholders = ",".join("?" * len(sub_ids))
    await db.execute(
        f"""UPDATE tasks
            SET coalesced_id = NULL, sort_order = NULL, created_at = ?,
                status = 'pending', approval = ?, queued_at = NULL
            WHERE id IN ({placeholders})""",
        [now, approval_val] + sub_ids,
    )

    # Lifecycle event: freed tasks reset to pending
    await db.executemany(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, 'restored', ?)",
        [(sid, now) for sid in sub_ids]
    )

    await db.commit()
    return sub_ids


async def uncoalesce_trigger(trigger_id: int) -> int:
    """Remove a single subordinate from its coalesce group, making it independent."""
    db = await get_db()

    rows = await db.execute_fetchall(
        "SELECT id, job_id, coalesced_id FROM tasks WHERE id = ?",
        (trigger_id,)
    )
    if not rows:
        raise ValueError("Trigger not found")
    task = rows[0]
    if not task["coalesced_id"]:
        raise ValueError("Trigger is not a subordinate — use split on the root trigger")
    task_status = await get_trigger_status_from_events(trigger_id, db)
    if task_status not in PRE_EXECUTION_STATUSES:
        raise ValueError("Can only uncoalesce pre-execution triggers")

    # Verify root is in pending (uncoalesce is a pending-column operation)
    root_status = await get_trigger_status_from_events(task["coalesced_id"], db)
    if root_status is not None and root_status != "pending":
        raise ValueError("Can only uncoalesce from a pending root — split is a pending-column operation")

    # Look up the job's current require_approval setting
    prop_rows = await db.execute_fetchall(
        "SELECT value FROM job_properties WHERE job_id = ? AND key = 'require_approval'",
        (task["job_id"],)
    )
    require_approval = prop_rows[0]["value"] == "true" if prop_rows else False
    approval_val = "pending" if require_approval else None

    from backend.state import utcnow
    now = utcnow()
    # Freed task always lands in pending (uncoalesce is a pending-column operation)
    await db.execute(
        "UPDATE tasks SET coalesced_id = NULL, sort_order = NULL, created_at = ?, status = 'pending', approval = ?, queued_at = NULL WHERE id = ?",
        (now, approval_val, trigger_id),
    )

    # Lifecycle event: freed task resets to pending
    await db.execute(
        "INSERT INTO task_events (task_id, event, created_at) VALUES (?, 'restored', ?)",
        (trigger_id, now)
    )

    await db.commit()
    return trigger_id


async def sweep_stale_triggers(now: str):
    """Mark any in-flight tasks as interrupted (e.g. after restart).

    Uses transition_task for each stale task so the event log records
    the interruption — status is never updated without an event.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE status = 'active'"
    )
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    for trigger_id in ids:
        await transition_trigger(trigger_id, "interrupted", error="interrupted", completed_at=now)
    return ids


async def approve_trigger(trigger_id: int) -> bool:
    """Approve a pending-approval task (and its subordinates).

    No task_event is written here: approval is orthogonal to status — an
    approved task is still 'pending' or 'queued', it just becomes eligible
    for dispatch. reject_task differs because rejection IS a status change
    (→ 'rejected', terminal) and goes through transition_task.
    """
    db = await get_db()
    cursor = await db.execute(
        "UPDATE tasks SET approval = 'approved'"
        " WHERE (id = ? OR coalesced_id = ?) AND approval = 'pending'",
        (trigger_id, trigger_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def reject_trigger(trigger_id: int) -> bool:
    """Reject a pending-approval task (and its subordinates).

    Uses transition_task so the event log records each rejection.
    """
    db = await get_db()
    # Find all tasks in the coalesce group with pending approval
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE (id = ? OR coalesced_id = ?) AND approval = 'pending'",
        (trigger_id, trigger_id)
    )
    if not rows:
        return False
    for row in rows:
        tid = row["id"]
        try:
            await transition_trigger(tid, "rejected", error="rejected")
        except ValueError:
            log.warning("[database] reject_trigger: trigger #%d already terminal — skipping", tid)
            continue
        await db.execute(
            "UPDATE tasks SET approval = 'rejected' WHERE id = ?", (tid,)
        )
        await db.commit()
    return True
