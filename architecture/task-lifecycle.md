# Task Lifecycle

Task lifecycle is event-sourced. A `task_events` table records every state transition as an immutable event. The task record itself carries identity and a materialized `status` column for query performance, but the event log is the source of truth for when things happened and in what order.

## Why Event Sourcing

The prior design stored lifecycle state as columns on the task record: `status`, `queued_at`, `started_at`, `completed_at`, `error`. This worked but created structural problems:

- **Schema coupling**: every new lifecycle state required a column or migration. Adding `paused`, `waiting_for_input`, or `retrying` would mean altering the task record.
- **Lost history**: retry would reset fields in-place — the previous attempt's timestamps were overwritten with no record that a task was active from 14:00–14:05, failed, then retried at 14:10.
- **Redundant derivation**: the `status` column was added to replace multi-column predicate derivation from timestamps, but the timestamps remained. Two representations of the same information, maintained in parallel.
- **Rigid duration computation**: duration as `completed_at - started_at` breaks for tasks that were interrupted and resumed. The event log captures each active/terminal pair independently.

The event log resolves all four: new event types require no schema change, history is append-only, timestamps live in events rather than columns, and duration is computed from event pairs.

## Task Events Table

```sql
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    detail TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
)

CREATE INDEX idx_task_events_task ON task_events(task_id, id DESC)
CREATE INDEX idx_task_events_type ON task_events(task_id, event, id DESC)
```

Each row is an immutable fact: "task X entered state Y at time Z with optional detail." Events are never updated or deleted (except by cascade when the task itself is deleted).

### Column Semantics

| Column | Purpose |
|--------|---------|
| `task_id` | FK to the task this event belongs to |
| `event` | The lifecycle event type (see below) |
| `detail` | Optional context — error message, commit hash, session ID, or structured JSON |
| `created_at` | When this event occurred (UTC) |

The `id` column provides a total ordering tiebreaker when two events share the same `created_at` timestamp (SQLite datetime has second-level granularity).

## Event Types

### Lifecycle Events (Status-Changing)

These events correspond 1:1 with the task status state machine. The most recent lifecycle event determines the task's current status.

| Event | Status | Meaning | Detail |
|-------|--------|---------|--------|
| `dispatched` | `pending` | Task record inserted | Trigger type |
| `restored` | `pending` | Demoted from queued back to staging (transfer queued→pending) | — |
| `queued` | `queued` | Promoted to execution runway | — |
| `activated` | `active` | Worker picked up the task | `session_id`, `start_commit` |
| `completed` | `completed` | Agent finished successfully | `result_commit` |
| `exhausted` | `exhausted` | Agent hit the turn limit without finishing | Turns consumed, turn limit |
| `failed` | `failed` | Agent encountered an error | Error message |
| `cancelled` | `cancelled` | User or system cancelled | — |
| `timed_out` | `timed_out` | Watchdog terminated the agent | Timeout value |
| `interrupted` | `interrupted` | Process died while task was active | — |
| `rejected` | `rejected` | User rejected a pending-approval task | — |

The `dispatched` event is the create event; `restored` is emitted when a task moves backward from queued to pending (the only allowed backward transition). The split between `dispatched` and `restored` lets timeline displays distinguish "first appeared in pending" from "demoted from queued."

Legacy event names from earlier development (`created`, `unqueued`, `active`) are still recognized by the status-resolution code for backward compatibility with pre-rename project DBs, but new writes always use the canonical names above.

### Materialized Status

The `status` column on the task record is updated atomically with each event insert. It is a performance optimization — not a source of truth. If the status column and the latest event ever disagree, the event log wins. The `_resolve_task_status` helper backfills a synthetic event for legacy tasks that have a status column but no events.

### Metadata Events (Non-Status-Changing)

The schema permits non-lifecycle events (e.g., approval transitions, coalesce/uncoalesce records) to be added without touching the state machine. The current implementation does not yet write metadata events — coalesce mutations and approval transitions are tracked through column updates and are recoverable from the relevant column state, not the event log. The slot is reserved for future audit needs.

## Standard Procedures

Three query patterns replace all ad-hoc timestamp column reads:

### Current Status

Already materialized on the task record for performance. When the event log is needed directly:

```sql
SELECT event, detail, created_at
FROM task_events
WHERE task_id = ? AND event IN (<all status-changing events>)
ORDER BY id DESC
LIMIT 1
```

`get_task_status_from_events()` is the public helper.

### Latest Event of a Given Type

"When was this task last queued?" / "What error caused the last failure?"

```sql
SELECT event, detail, created_at
FROM task_events
WHERE task_id = ? AND event = ?
ORDER BY id DESC
LIMIT 1
```

This is the one-size-fits-all the system needs. Any question about task history reduces to: "what was the most recent event of type X?" The answer includes when it happened (`created_at`) and any associated data (`detail`).

### Full Event Timeline

"Show me everything that happened to this task."

```sql
SELECT event, detail, created_at
FROM task_events
WHERE task_id = ?
ORDER BY id ASC
```

Used by the frontend timeline display and `compute_durations_from_events`. The batch helper `get_task_events_batch` fetches events for many tasks in one query — used by queue rendering to avoid N+1.

## Duration Computation

Duration is computed from event pairs, not from column arithmetic:

- **Execution duration**: time between the most recent `activated` event and the most recent terminal event (`completed`, `exhausted`, `failed`, `cancelled`, `timed_out`, `interrupted`)
- **Queue wait time**: time between the most recent `queued` event and the most recent `activated` event
- **Total lifecycle**: time between `dispatched` and the terminal event

For tasks with retry history (multiple `activated` → terminal cycles, possible if retry is ever changed to reuse the same record), each pair is independently computable. The full event timeline makes this natural — iterate events and pair each `activated` with its subsequent terminal.

The API layer computes these server-side via `compute_durations_from_events` and includes them in task responses. The frontend does not need to query events directly for duration — it receives computed values.

## Transition Function

`transition_task()` is an atomic operation across three tables:

1. **Validate** — check that `(current_status, new_status)` is in `LEGAL_TRANSITIONS`. The current status is read from the event log via `_resolve_task_status` (backfilling from the materialized column for legacy tasks).
2. **Split fields by destination** — kwargs in `_OUTCOME_FIELDS` (session_id, start_commit, result_commit, num_turns, cost_usd, started_at, completed_at, error, worktree_path, task_branch, orphan_stash_ref, stop_reason) route to `task_executions` via `upsert_execution`. Other kwargs (queued_at and clearing-on-pending) stay on `tasks`. The status timestamp column for the new state is added automatically — `started_at`/`completed_at` go to executions, `queued_at` stays on tasks.
3. **Write event** — insert a row into `task_events` with the mapped event type and structured detail (error, result_commit, session_id, start_commit composed by `_build_event_detail`).
4. **Update materialized status + queue placement** — `UPDATE tasks SET status = ?, ...` for the cached status and any non-outcome fields.
5. **Upsert execution row** — INSERT-or-UPDATE on `task_executions` keyed by `task_id` for any outcome fields.
6. **Commit** — single transaction.

All five steps run inside `try/except` with explicit `rollback()` on any failure. Earlier the function did not roll back: a partial commit (e.g. UPDATE failing because a column was missing on an unmigrated DB) would leave the event INSERT pending in the connection's transaction buffer, and the next operation that committed would persist the event without the column update. The events log diverged from the column status, and the worker would then get stuck repeatedly attempting to re-activate a task whose events log already said "active." Task #422 in the dev project was the witnessing case — see the `migrate_to_task_executions.py` reconciliation logic for the cleanup path. The rollback closes that hole structurally.

`transition_tasks_batch()` is the bulk variant used for cascading subordinates: it skips the per-task validation pass (caller asserts uniform source state), writes one event per task, issues a single `UPDATE ... WHERE id IN (...)`, and upserts execution rows per task. Same field-routing and rollback treatment as the single-task version. The worker uses this through `cascade_completion()` after every terminal transition to propagate the outcome to the root's coalesced subtree.

`update_task` and `update_tasks_batch` are the non-status helpers. They route fields the same way (outcome fields to executions, non-outcome to tasks) so call sites that update worktree_path, orphan_stash_ref, etc. continue to work unchanged.

### State Machine

```
pending  → queued       (transfer, auto-queue at enqueue time)
pending  → cancelled    (cancel before execution)
pending  → rejected     (reject pending-approval task)
queued   → pending      (transfer back to staging — only backward transition allowed)
queued   → active       (worker picks up task)
queued   → cancelled    (cancel before execution)
active   → completed    (agent finished successfully)
active   → exhausted    (agent hit turn limit without finishing)
active   → failed       (agent error)
active   → cancelled    (user cancellation)
active   → timed_out    (watchdog fires)
active   → interrupted  (stale sweep on startup)
```

## Task Record Columns

After the schema normalization, task data lives in three tables. See [Storage — Task Record](storage.md#task-record) for the full split. Summary:

| Table | Columns |
|---|---|
| `tasks` (12 cols) | id, job_id, trigger, trigger_detail, context, resume_session_id, approval, status, queued_at, sort_order, coalesced_id, created_at |
| `task_executions` (13 cols, one row per task that ran) | task_id (PK), session_id, start_commit, result_commit, stop_reason, num_turns, cost_usd, started_at, completed_at, error, worktree_path, task_branch, orphan_stash_ref |
| `task_events` (append-only) | id, task_id, event, detail, created_at |

`tasks` is intentionally narrow: identity, queue placement, and a materialized `status` cache. Adding new per-execution columns now means a single ALTER on `task_executions`, not on the hot table.

`started_at` and `completed_at` moved with the rest of the per-execution data — they're redundant with the event log (which carries the canonical timestamp on each lifecycle event), but kept on the executions row for query convenience. Duration computation reads from the event log per [Duration Computation](#duration-computation); the column-based timestamps exist for queries and dashboards that filter by completion time without needing the full event history.

`queued_at` stays on `tasks` because queue placement is per-task (transitions queued↔pending preserve it), not per-execution.

## Knock-On Effects

### Worker Hot Path

No change. The worker queries `WHERE status = 'queued' AND (approval IS NULL OR approval = 'approved') AND coalesced_id IS NULL`. The materialized `status` column and its `idx_tasks_status_worker` index are preserved.

### Stale Sweep

On startup, find tasks with `status = 'active'` and transition them to `interrupted`. The transition writes an `interrupted` event, updates the materialized status, and sets `error`/`completed_at` on the execution row. `sweep_stale_tasks` does this in a single batch. The same sweep also runs `git worktree prune` to clear registry entries for worktrees whose directories were removed externally — see [Git Integration — Worktree Management](git-integration.md#worktree-management).

### Dashboard Aggregation

Health queries filter on `tasks.status` (materialized) and `tasks.coalesced_id IS NULL`, then JOIN `task_executions` for the time-window filter on `te.completed_at` and per-execution metrics. Timeline queries pair `activated` with terminal events for duration; the `idx_task_events_type` index supports the required lookups. The `idx_task_executions_completed_at` index covers time-window filtering on completion.

### Coalescing

Coalescing queries filter on `status IN ('pending', 'queued')` (uses materialized status). The event log is not consulted on the hot path. Cascading subordinates to a terminal status uses `transition_tasks_batch` which writes one event per subordinate.

### Frontend Timeline

Currently renders four conditional timestamp lines from columns. Migrating to render the full event list from the timeline endpoint is strictly more informative — the user sees every transition, not just four checkpoints.

### Retry

The current retry implementation creates a new task record and inverts coalescing (the original becomes subordinate to the retry). The event log captures the lifecycle of each task independently. If retry is ever changed to reuse the same record, the event log naturally supports multiple `activated` → terminal cycles on one record.

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) — `transition_task()` is the sole writer of lifecycle events. The worker, queue routes, and approval gates all go through it.
- [Storage](storage.md) — the `task_events` table is part of the project database, subject to the same connection management and project-switch coordination.
- [Streaming and Sessions](streaming-and-sessions.md) — chat events are a separate audit trail (raw CLI output). Task events are lifecycle transitions. They complement each other: chat events record what the agent said and did; task events record what the platform did to the task.
- [Frontend](frontend.md) — the timeline section migrates from timestamp columns to the event timeline endpoint. Status display continues to use the materialized status.
