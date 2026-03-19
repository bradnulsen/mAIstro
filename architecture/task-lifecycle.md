# Task Lifecycle

Task lifecycle is event-sourced. A `task_events` table records every state transition as an immutable event. The task record itself carries identity and a materialized `status` column for query performance, but the event log is the source of truth for when things happened and in what order.

## Why Event Sourcing

The prior design stored lifecycle state as columns on the task record: `status`, `queued_at`, `started_at`, `completed_at`, `error`. This worked but created structural problems:

- **Schema coupling**: every new lifecycle state requires a column or migration. Adding `paused`, `waiting_for_input`, or `retrying` means altering the task record.
- **Lost history**: retry resets fields in-place — the previous attempt's timestamps are overwritten. There is no record that a task was active from 14:00–14:05, failed, then retried at 14:10.
- **Redundant derivation**: the `status` column was added to replace multi-column predicate derivation from timestamps, but the timestamps remained. Two representations of the same information, maintained in parallel.
- **Rigid duration computation**: duration is `completed_at - started_at`, but this breaks for tasks that were interrupted and resumed. The event log captures each active/terminal pair independently.

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

| Event | Meaning | Detail |
|-------|---------|--------|
| `created` | Task record inserted | Trigger type and source |
| `queued` | Promoted to execution runway | — |
| `unqueued` | Demoted back to pending | — |
| `active` | Worker picked up the task | `session_id`, `start_commit` |
| `completed` | Agent finished successfully | `result_commit` |
| `failed` | Agent encountered an error | Error message |
| `cancelled` | User or system cancelled | — |
| `timed_out` | Watchdog terminated the agent | Timeout value |
| `interrupted` | Process died while task was active | — |
| `rejected` | User rejected a pending-approval task | — |

### Derived Status

The mapping from the most recent lifecycle event to the materialized `status` column:

| Latest event | Status |
|-------------|--------|
| `created` | `pending` |
| `queued` | `queued` |
| `unqueued` | `pending` |
| `active` | `active` |
| `completed` | `completed` |
| `failed` | `failed` |
| `cancelled` | `cancelled` |
| `timed_out` | `timed_out` |
| `interrupted` | `interrupted` |
| `rejected` | `rejected` |

The `status` column on the task record is updated atomically with each event insert. It is a performance optimization — not a source of truth. If the status column and the latest event ever disagree, the event log wins.

### Metadata Events (Non-Status-Changing)

These events record significant moments that don't change the task's status. They extend the audit trail without touching the state machine.

| Event | Meaning | Detail |
|-------|---------|--------|
| `approved` | Approval gate passed | — |
| `coalesced` | Merged into another task's group | Root task ID |
| `uncoalesced` | Split from a coalesce group | — |

Metadata events are extensible — new types can be added without schema changes or state machine modifications. The only contract is: metadata events do not affect `status`.

## Standard Procedures

Three query patterns replace all ad-hoc timestamp column reads:

### Current Status

Already materialized on the task record for performance. When the event log is needed directly:

```sql
SELECT event, detail, created_at
FROM task_events
WHERE task_id = ?
ORDER BY id DESC
LIMIT 1
```

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

Used by the frontend timeline display. Replaces the current pattern of conditionally rendering timestamps from four separate columns.

## Duration Computation

Duration is computed from event pairs, not from column arithmetic:

- **Execution duration**: time between the most recent `active` event and the most recent terminal event (`completed`, `failed`, `cancelled`, `timed_out`, `interrupted`)
- **Queue wait time**: time between the most recent `queued` event and the most recent `active` event
- **Total lifecycle**: time between `created` and the terminal event

For tasks with retry history (multiple `active` → terminal cycles), each pair is independently computable. The full event timeline makes this natural — iterate events and pair each `active` with its subsequent terminal.

The API layer computes these server-side and includes them in task responses. The frontend does not need to query events directly for duration — it receives computed values.

## Transition Function

`transition_task()` becomes a two-step atomic operation:

1. **Validate** — check that `(current_status, new_status)` is a legal transition (unchanged from current logic)
2. **Write event** — insert a row into `task_events` with the event type and any detail
3. **Update materialized status** — set `tasks.status` to the new value

Steps 2 and 3 happen in the same transaction. The event is the record of what happened; the status update is the cache of where we are.

```python
async def transition_task(task_id, new_status, **fields):
    # 1. Validate
    current = await _get_current_status(task_id)
    assert (current, new_status) in LEGAL_TRANSITIONS

    # 2. Write event
    event_type = _STATUS_TO_EVENT[new_status]
    detail = _build_event_detail(new_status, fields)
    await _insert_event(task_id, event_type, detail)

    # 3. Update materialized status
    await _update_task_status(task_id, new_status, **fields)
```

The `**fields` kwargs (error, session_id, start_commit, result_commit) continue to update the task record directly — these are execution metadata that the worker and API read frequently. They also flow into the event's `detail` column for the audit trail.

## What Stays on the Task Record

The task record retains columns that are either identity (immutable after creation) or frequently queried by the worker hot path:

| Column | Category | Rationale |
|--------|----------|-----------|
| `id`, `job_id`, `trigger`, `trigger_detail`, `context` | Identity | Immutable, set at creation |
| `status` | Materialized | Worker query performance — indexed, single-column filter |
| `session_id`, `resume_session_id` | Execution | Set once, read by worker and API on every task access |
| `approval` | Orthogonal | Not lifecycle state — separate concern, queried with status |
| `start_commit`, `result_commit` | Execution | Set once, read for outcome summaries and dependency context |
| `error` | Execution | Set once on terminal non-success, read by API for display |
| `coalesced_id`, `sort_order` | Queue management | Not lifecycle state — separate concerns |
| `created_at` | Identity | Record creation time, used for sort tiebreaking |

### What Moves to Events

| Former column | Replaced by |
|---------------|-------------|
| `queued_at` | `queued` event timestamp |
| `started_at` | `active` event timestamp |
| `completed_at` | Terminal event timestamp |

These columns can be dropped once all reads migrate to event queries. During transition, they are maintained in parallel (dual-write) and the API layer can serve either source.

## Migration Path

### Phase 1: Dual-Write (Non-Breaking)

Add `task_events` table. `transition_task()` writes both the event and updates the task record (including timestamp columns). All existing queries continue to work unchanged. The event table begins accumulating history for new tasks.

Backfill: for existing tasks, synthesize events from current column values — one event per non-null timestamp. This populates history for tasks that predate the event table.

### Phase 2: Read Migration

Switch timeline display and duration computation to read from events. The API returns computed timestamps from events. Frontend continues to receive the same data shape — the computation moves server-side.

Drop the `getTaskStatus()` timestamp fallback in the frontend (it exists for pre-migration data that will have been backfilled in Phase 1).

### Phase 3: Column Removal

Drop `queued_at`, `started_at`, `completed_at` from the task record. These are fully redundant with events. The task record shrinks to identity + materialized status + execution metadata.

## Knock-On Effects

### Worker Hot Path

No change. The worker queries `WHERE status = 'queued' AND (approval IS NULL OR approval = 'approved') AND coalesced_id IS NULL`. The materialized `status` column and its index are preserved.

### Stale Sweep

No change to logic. On startup, find tasks with `status = 'active'`, transition them to `interrupted`. The transition writes an `interrupted` event and updates the materialized status.

### Dashboard Aggregation

Health queries filter on `status` (materialized) — no change. Timeline queries currently use `started_at`/`completed_at` for duration; these migrate to event-pair computation in Phase 2. The index on `task_events(task_id, event, id DESC)` supports the required lookups.

### Coalescing

Coalescing queries filter on `status IN ('pending', 'queued')` — no change (uses materialized status).

### Frontend Timeline

Currently renders four conditional timestamp lines. Migrates to rendering the full event list from the timeline endpoint. This is strictly more informative — the user sees every transition, not just four checkpoints.

### Retry

Currently resets fields in-place on the same record. With events, retry creates a new `active` event on the same task — the previous `failed` event remains. The full retry history is visible: `created → queued → active → failed → queued → active → completed`. No information is lost.

Note: the current retry implementation creates a new task record with `retry` trigger and coalesces the original. The event log captures the lifecycle of each task independently. If retry is ever changed to reuse the same record, the event log naturally supports multiple cycles.

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) — `transition_task()` is the sole writer of lifecycle events. The worker, queue routes, and approval gates all go through it.
- [Storage](storage.md) — the `task_events` table is part of the project database, subject to the same connection management and project-switch coordination.
- [Streaming and Sessions](streaming-and-sessions.md) — chat events are a separate audit trail (raw CLI output). Task events are lifecycle transitions. They complement each other: chat events record what the agent said and did; task events record what the platform did to the task.
- [Frontend](frontend.md) — the timeline section migrates from timestamp columns to the event timeline endpoint. Status display continues to use the materialized status.
