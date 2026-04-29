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

`transition_task()` is a two-step atomic operation:

1. **Validate** — check that `(current_status, new_status)` is in `LEGAL_TRANSITIONS`. The current status is read from the event log via `_resolve_task_status` (backfilling from the materialized column for legacy tasks).
2. **Write event** — insert a row into `task_events` with the mapped event type and structured detail (error, result_commit, session_id, start_commit composed by `_build_event_detail`).
3. **Update materialized status** — `UPDATE tasks SET status = ?, <timestamp_col> = ?, ...` in the same transaction.

Steps 2 and 3 happen in the same transaction. The event is the record of what happened; the status update is the cache of where we are.

`transition_tasks_batch()` is the bulk variant used for cascading subordinates: it skips the per-task validation pass (caller asserts uniform source state), writes one event per task, and issues a single `UPDATE ... WHERE id IN (...)`. The worker uses this through `cascade_completion()` after every terminal transition to propagate the outcome to the root's coalesced subtree.

The `**fields` kwargs (error, session_id, start_commit, result_commit) update the task record directly — these are execution metadata that the worker and API read frequently. They also flow into the event's `detail` column for the audit trail.

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

The task record retains columns that are either identity (immutable after creation), frequently queried by the worker hot path, or transitional from the pre-event-log era:

| Column | Category | Rationale |
|--------|----------|-----------|
| `id`, `job_id`, `trigger`, `trigger_detail`, `context` | Identity | Immutable, set at creation |
| `status` | Materialized | Worker query performance — indexed, single-column filter |
| `session_id`, `resume_session_id` | Execution | Set once, read by worker and API on every task access |
| `approval` | Orthogonal | Not lifecycle state — separate concern, queried with status |
| `start_commit`, `result_commit` | Execution | Set once, read for outcome summaries and cascade context |
| `stop_reason`, `num_turns`, `cost_usd` | Execution | Set once at terminal, read by API for execution metadata display |
| `error` | Execution | Set once on terminal non-success, read by API for display |
| `coalesced_id`, `sort_order` | Queue management | Not lifecycle state — separate concerns |
| `created_at` | Identity | Record creation time, used for sort tiebreaking |
| `queued_at`, `started_at`, `completed_at` | Transitional | Maintained via dual-write — see [Migration Path](#migration-path) |

### Transitional Timestamp Columns

| Column | Replaced by |
|--------|-------------|
| `queued_at` | `queued` event timestamp |
| `started_at` | `activated` event timestamp |
| `completed_at` | Terminal event timestamp |

These columns are still maintained by `transition_task` (dual-write) so existing queries continue to work, but they are redundant with the event log and slated for removal.

## Migration Path

### Phase 1: Dual-Write (Current)

`transition_task()` writes both the event and updates the task record (including timestamp columns). All existing queries continue to work unchanged. The event table accumulates history for new tasks. For legacy tasks with a status column but no events, `_resolve_task_status` backfills a synthetic event on first transition.

### Phase 2: Read Migration

Switch timeline display and duration computation to read from events. The API returns computed timestamps from events. Frontend continues to receive the same data shape — the computation moves server-side.

Drop the `getTaskStatus()` timestamp fallback in the frontend (it exists for pre-event-log data that will have been backfilled in Phase 1).

### Phase 3: Column Removal

Drop `queued_at`, `started_at`, `completed_at` from the task record. These are fully redundant with events. The task record shrinks to identity + materialized status + execution metadata.

## Knock-On Effects

### Worker Hot Path

No change. The worker queries `WHERE status = 'queued' AND (approval IS NULL OR approval = 'approved') AND coalesced_id IS NULL`. The materialized `status` column and its `idx_tasks_status_worker` index are preserved.

### Stale Sweep

On startup, find tasks with `status = 'active'` and transition them to `interrupted`. The transition writes an `interrupted` event and updates the materialized status. `sweep_stale_tasks` does this in a single batch.

### Dashboard Aggregation

Health queries filter on `status` (materialized) and `coalesced_id IS NULL` — no change. Timeline queries pair `activated` with terminal events for duration; the `idx_task_events_type` index supports the required lookups.

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
