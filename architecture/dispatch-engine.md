# Dispatch Engine

The dispatch engine is the execution core of mAistro. It enforces the queue-first invariant, processes tasks sequentially, manages the full task lifecycle, and coordinates with the streaming and storage systems.

## Queue-First Invariant

No task may execute without passing through the queue. Every code path — manual dispatch, watch trigger, schedule fire, dependency completion, agent dispatch, resume, retry — writes a record to `tasks` first. The worker is the only code path that reads from the queue and invokes execution.

This invariant decouples trigger sources from execution. A trigger's responsibility is to write a queue record with appropriate context; it never needs to know about CLI invocation, streaming, or session management.

**Corollary**: a job's running state never blocks enqueue. A user can dispatch a job that is currently executing — the new task enters the queue and waits. The worker processes it when the current task completes. Blocking dispatch based on running state would violate the queue-first invariant by making the enqueue path aware of execution state.

## Task Record

Each `tasks` row tracks:

| Column | Purpose |
|--------|---------|
| `job_id` | Which job this task belongs to |
| `status` | Authoritative task state — see [Task Status](#task-status) below |
| `trigger` | Primary trigger type (manual, commit, schedule, dependency, resume, retry) |
| `trigger_detail` | Trigger-specific reference (commit hash, cron expression, upstream job ID) |
| `context` | Pre-formatted context text built at the enqueue site |
| `session_id` | Linked chat session (set when execution starts) |
| `resume_session_id` | CLI session ID for resume tasks |
| `approval` | Gate status: `null` (no gate), `pending`, `approved`, `rejected` |
| `start_commit` | HEAD hash when execution began |
| `stop_reason` | Why the agent stopped (end_turn, max_turns, error, etc.) |
| `num_turns` | Agent turns consumed during execution |
| `cost_usd` | Estimated API cost |
| `created_at` | When the task was enqueued |
| `queued_at` | When the task was promoted to queued state (NULL = still pending) |
| `started_at` | When the worker began processing |
| `completed_at` | When execution finished (success, failure, or cancellation) |
| `result_commit` | HEAD hash after execution completed |
| `error` | Error detail for non-success terminal states (free-form text for `failed`, sentinel strings for others) |
| `coalesced_id` | Links subordinate tasks to a root task for merge; NULL = standalone/root |

### Task Status

Task lifecycle is event-sourced. See [Task Lifecycle](task-lifecycle.md) for the full specification.

The `status` column on the task record is a materialized view of the latest lifecycle event — it exists for query performance, not as a source of truth. The `task_events` table records every transition as an immutable event with a timestamp and optional detail.

**Status values**: `pending`, `queued`, `active`, `completed`, `exhausted`, `failed`, `cancelled`, `interrupted`, `timed_out`, `rejected`. Completed is success; everything else is non-success.

**Transition rules** are enforced by `transition_task()`, which validates the transition, writes an event to `task_events`, and updates the materialized `status` — all in one transaction.

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

**Worker query**: `WHERE status = 'queued' AND (approval IS NULL OR approval = 'approved') AND coalesced_id IS NULL`. The `approval` and `coalesced_id` predicates are orthogonal concerns. Index on `(status, approval, coalesced_id)`.

**Invariants**:
- `status` is never NULL — every task has exactly one status at all times
- `status` only changes through `transition_task` — no raw UPDATE on status
- Terminal statuses are permanent — once set, a task's status never changes again
- Every `transition_task` call produces exactly one event in `task_events`
- The `error` column is informational for terminal non-success states — `status` is authoritative for determining the outcome type

## Worker

**Module**: `backend/worker.py`

The worker is a single `asyncio.Task` running a poll loop. It wakes on notification (via `asyncio.Event`) or every 2 seconds, whichever comes first.

### Two-Stage Queue

Tasks progress through two pre-execution states before the worker picks them up:

- **Pending** — the staging area. Newly created tasks land here by default. The user reviews, coalesces, and curates pending tasks before promoting them to queued. Pending tasks are invisible to the worker.
- **Queued** — the execution runway. Tasks here are committed to run. The worker pulls the highest-priority queued task when ready. The user reorders queued tasks to control execution sequence.

**Auto-queueing** (`queue_auto_dispatch` config) controls the initial routing of newly created tasks. When enabled, new tasks skip pending and go directly to queued (`queued_at` set at creation). When disabled, new tasks enter pending (`queued_at` remains NULL). The worker always runs — auto-queueing only controls where tasks land on creation, not whether the worker processes.

The user can override auto-queueing for any individual task by transferring it between columns (dragging from queued to pending or vice versa). The transfer sets or clears `queued_at` accordingly.

### Sequential Execution

An `asyncio.Lock` guards task processing. Exactly one task runs at a time. The lock is held for the full duration of CLI execution — from session creation through final DB update.

The `_active_task_id` global tracks which task is currently running, enabling cancellation and the "active task blocks project switch" safety invariant.

The worker's poll loop also makes DB calls outside the lock (e.g., `get_oldest_queued_task`). These are protected by the connection-level coordination described in [Storage — Project-Switch Coordination](storage.md#project-switch-coordination), which ensures `close_db()` waits for in-flight DB operations to complete.

The worker only queries for tasks with `status = 'queued'`. Pending tasks are invisible to the worker regardless of their queue position.

### Task Ordering

The queued column maintains a sort order via `sort_order` that determines execution priority. The worker pulls the highest-priority queued task (lowest `sort_order` in the queued set). The user controls execution order by reordering queued tasks via drag-and-drop.

The pending column does not maintain a meaningful sort order — it is a staging area for curation, not a priority queue. Tasks in pending are displayed by creation time.

Newly created tasks are appended to the end of their target column (pending by default, or queued if auto-queueing is enabled). Tasks transferred from pending to queued are appended to the end of the queued column.

### Execution Flow

1. **Session creation**: creates (or reuses for resume) a `chat_session` linked to the task
2. **Lifecycle start**: records `started_at`, `start_commit`, and `session_id` on the task record
3. **External MCP health check**: probes all external MCP servers assigned to the task's job. If any server is unreachable or disabled, the task fails immediately with a specific error naming the server and the failure reason. This gate ensures the task never runs with a silently missing tool surface. See [Tool Mediation — Pre-Dispatch Health Check](tool-mediation.md#lifecycle)
4. **Timeout watchdog**: spawns an async task that fires the cancellation event after the configured timeout
5. **Task execution**: calls `run_task()` which assembles prompts and invokes the CLI — yields events
6. **Event processing**: raw events go to the audit trail; translated events go to live subscribers; text accumulates for the final chat message. MCP tool invocation events are recorded as structured audit entries
7. **Completion**: records `completed_at` and `result_commit`; triggers dependent jobs if successful
8. **Cleanup**: cancels watchdog, broadcasts `_done` to subscribers, clears active task state

### Cancellation

Cancellation works through an `asyncio.Event` shared with the CLI bridge. Setting the event causes the CLI subprocess to be terminated (SIGTERM, then SIGKILL after 5s grace). Both user-initiated cancellation and timeout use this same mechanism.

### Stale Sweep

On startup (once per project open), the worker transitions any tasks with `status = 'active'` to `interrupted` via `transition_task`. This handles the case where the process died mid-task.

### Dependent Job Propagation

After successful completion (no error, no timeout), the worker scans all jobs for those declaring the completed job in their `depends_on` list. For each match, it enqueues a new task with `dependency` trigger, including context about the upstream job, its commit range, and outcome summary. A dependent job fires when *any* of its upstream jobs completes — it does not wait for all upstreams.

Circular dependency chains are safe: coalescing absorbs redundant triggers, and sequential execution ensures no concurrent amplification. A cycle produces at most one pending task per job at any time.

Only `status = 'completed'` triggers dependents — `exhausted`, `failed`, `timed_out`, `cancelled`, `interrupted`, and `rejected` tasks do not.

## Approval Gates

Jobs with `require_approval=true` get `approval='pending'` on task enqueue — except manual dispatches, which bypass the gate (manual = explicit human intent). The worker's queued task query skips rows where `approval='pending'`.

Approval and rejection are API operations that propagate to the full coalesce group:
- **Approve**: sets `approval='approved'` on the root task and all subordinates with `approval='pending'`, then wakes the worker
- **Reject**: sets `approval='rejected'` and marks as completed with error "rejected" — on both the root task and all pending-approval subordinates

Processing a specific task via `process_one()` auto-approves pending-approval tasks (explicit intent, same rationale as manual dispatch).

## Commit Tracking (No Auto-Commit)

The worker captures `start_commit` (HEAD at task start) and `result_commit` (HEAD at task end) to track what an agent produced. These bookend the agent's work — if `start_commit == result_commit`, the agent made no commits.

**The platform does not auto-commit.** Agents have structured git tools via the internal MCP server (see [Tool Mediation](tool-mediation.md)) and are instructed to commit in the system prompt. The platform trusts agents to commit their own work. Rationale:

- **Authorship integrity**: every commit carries the job's identity (`[GoalName]` prefix, job-specific author). An auto-commit would break this — the platform would have to guess what message and authorship to apply.
- **Atomic intent**: agents decide what constitutes a logical commit. They may make multiple commits for distinct changes or one commit for related changes. Auto-commit would force a single "catch-all" commit with no meaningful message.
- **Parallel execution future**: if tasks ever run concurrently, auto-commit becomes intractable — the working tree contains interleaved changes from multiple agents, and there's no way to attribute which changes belong to which task.
- **Observable failure**: when `start_commit == result_commit` but the agent was supposed to produce changes, the task output and audit trail reveal what happened. This is more useful than silently committing unknown changes.

The commit range (`start_commit..result_commit`) feeds into dependency trigger context — downstream jobs see exactly which commits their upstream produced.

## Turn Limit

Each job has a configurable `max_turns` property. The value is passed to the CLI's `--max-turns` flag. When the agent reaches the limit, the CLI stops the session and the platform transitions the task to `exhausted`.

The turn limit is a safety bound, not a target. Most tasks finish well within it. When a task hits the limit, it typically means the instructions are too broad, the agent is stuck in a loop, or the work genuinely requires more interaction than anticipated. Exhaustion is a distinct terminal state from timeout — turns vs. time — with different diagnostic responses.

## Execution Metadata

When a task reaches a terminal state, the platform records execution metadata alongside the lifecycle transition:

- **Stop reason**: why the agent stopped — natural completion, turn limit reached, error, timeout, cancellation
- **Turns consumed**: how many agent turns were used, relative to the configured limit
- **Duration**: wall-clock execution time
- **Cost**: estimated API cost in USD

This metadata is surfaced in the Dispatch view alongside the outcome summary. "Completed in 3/50 turns at $0.02" is operationally useful; "completed" alone is not. The metadata enables the operator to assess efficiency, detect anomalies (e.g., a task that normally takes 5 turns suddenly consuming 40), and tune job configuration.

The metadata fields are stored on the task record, set once at task completion. They are informational — they do not affect the state machine or dependency propagation.

## Dispatch Outcomes

When a task completes, the platform derives an outcome summary from git. This serves both the operator (scan completed work at a glance) and downstream agents (understand what upstream actually produced).

### Outcome Summary

The summary is computed from git artifacts — the commits between `start_commit` and `result_commit`. It captures commit messages and change statistics. This is a derived value, not an authored one: the platform reads what the repository records, not what the agent claims.

- If `start_commit == result_commit`, the task produced no commits and has no summary
- The summary is derived on demand from `start_commit` and `result_commit` — not stored. These two columns are the authoritative link between a task and its git changes; the summary is always recoverable from them
- When this task triggers downstream dependents, the outcome summary is included in the trigger context — downstream agents receive concrete information about what their upstream produced

## Manual Queue Composition

The user can merge and split tasks in the pending column directly from the Dispatch view. This gives explicit control over the grouping that automatic coalescing performs implicitly. Composition is a pending-column operation — it belongs to the curation stage, not the execution runway.

### Implementation: Coalesced ID

Rather than destructively removing task records during merge (losing individual trigger provenance), the system uses a **`coalesced_id` column** on `tasks`. This preserves every task as an atomic record while linking them for unified execution.

| `coalesced_id` value | Meaning |
|----------------------|---------|
| `NULL` | Standalone task — visible in queue, dispatched independently |
| `<task_id>` | Subordinate task — linked to the root task identified by this ID |

**Queue rendering**: the queue shows all tasks where `coalesced_id IS NULL`. These are either standalone tasks or root tasks that have subordinates linked to them. Both pending and queued columns filter on this condition independently.

**Dispatch collection**: when the worker pulls a queued task for execution, it collects all tasks whose `coalesced_id` equals the dispatched task's ID. The context from all collected tasks is unified into the prompt. Each subordinate task's context is preserved verbatim — merge does not rewrite history.

**Route behavior**: API routes that act on a specific task ID automatically consider all tasks with `coalesced_id` equal to that ID. This means cancellation, approval, and other lifecycle operations propagate to the full group.

### Merge

Combines two pending tasks for the same job into a single logical unit. Triggered by dragging one pending task onto another in the Dispatch view (see [Frontend — Drag Interaction Model](frontend.md)).

1. The older task (by `created_at`) becomes the root — its `coalesced_id` remains NULL
2. The dragged task gets `coalesced_id` set to the root task's ID
3. The root task retains its queue position; the subordinate task becomes invisible in the queue view
4. **Flatten**: if the newly subordinated task had its own subordinates (was itself a root), those subordinates are re-pointed to the new root. This ensures coalesce groups are always flat — depth 1, never nested. Every subordinate points directly to its root.

**Constraints**:
- Only pending tasks (not queued, not started, not completed, not pending-approval). Merge is a curation operation that belongs to the staging area
- Same job only — a task's identity is bound to one job; cross-job merge would break prompt assembly, tool configuration, and commit authorship. The UI enforces this structurally by suppressing the merge affordance when tasks belong to different jobs
- Tasks that are already subordinates (have a non-null `coalesced_id`) cannot be merge targets — they must be split from their current root first

### Split (Uncoalesce)

Reverses a merge or automatic coalescing. Takes a pending root task that has subordinate tasks and makes them independent again.

1. All tasks with `coalesced_id` equal to the root task's ID get `coalesced_id` set back to NULL
2. Split-off tasks get `sort_order` cleared to NULL and `created_at` reset to the current time — they appear at the end of the pending column
3. Split-off tasks inherit the job's current `require_approval` setting — if the gate is now enabled, they enter pending-approval state even if the original merge happened before the gate was set

**Constraints**:
- Only pending root tasks (`coalesced_id IS NULL`, `status = 'pending'`) that have at least one subordinate can be split. Split is a coalescing operation and coalescing belongs to the pending column
- Only pre-execution tasks (`status IN ('pending', 'queued')`)
- The root task itself is unchanged — it retains its position, trigger, and context. Only subordinates are released

### Transfer

Moves a task between pending and queued columns. This is the mechanism for promoting tasks to the execution runway or demoting them back to staging.

- **Pending → Queued**: sets `queued_at` to current time. The task becomes eligible for the worker.
- **Queued → Pending**: clears `queued_at` back to NULL. The task is no longer eligible for the worker.
- Transferred tasks are appended to the end of the target column.
- Transfer is always a state-change operation — it does not merge with or reorder against existing tasks in the target column.

### Why Coalesced ID Over Record Deletion

The coalesced_id approach has structural advantages:

- **Atomic provenance**: every trigger that created a task retains its own record. Audit trails remain complete without relying on JSON array archaeology
- **Reversibility**: split is a column update, not record reconstruction. No information is lost during merge that must be recreated during split
- **Route simplicity**: "act on all tasks with this coalesced_id" is a single WHERE clause, uniformly applied across all endpoints
- **Automatic coalescing alignment**: the same mechanism can back automatic coalescing — instead of appending to a JSON array, create a new record with `coalesced_id` pointing to the existing pre-execution task

## Retry, Resume, and Reply

- **Retry**: creates a new task record with `retry` trigger. The original task is coalesced under the new one (preserving audit trail). The new task enters the queue normally — retry does not bypass the two-stage queue.
- **Resume**: creates a new task record with `resume_session_id` set to the original CLI session ID. The worker passes this to the CLI's `--resume` flag. If the original chat session still exists, it's reused. The original task is coalesced under the new one via inverted coalescing (see [Trigger System — Inverted Coalescing](trigger-system.md#inverted-coalescing-reply-and-resume)).
- **Reply**: creates a new task record with `reply` trigger and the user's follow-up context. The original task's commit range is included in the trigger context. The original task is coalesced under the new one via inverted coalescing. Unlike resume, reply does not reuse the CLI session — it starts a fresh session with the reply context.

All three create new task records rather than mutating the original. The original becomes a subordinate of the new task, preserving full provenance. In reply chains (A → B → C), the latest task is always the root and all predecessors are flat subordinates — the depth-1 invariant is maintained by `_flatten_coalesce`.

## Relationship to Other Systems

- [Trigger System](trigger-system.md) writes queue records; the dispatch engine reads and processes them
- [CLI Bridge](cli-bridge.md) is invoked by `run_task()` — the engine manages the lifecycle around it
- [Prompt Assembly](prompt-assembly.md) builds the prompts that `run_task()` feeds to the CLI
- [Streaming and Sessions](streaming-and-sessions.md) receives broadcast events from the worker and stores durable output
- [Task Lifecycle](task-lifecycle.md) defines the event-sourced state machine, transition validation, and duration computation
- [Git Integration](git-integration.md) provides `head_hash` for commit tracking and `changed_files_in_commit` for dependency context
- [Tool Mediation](tool-mediation.md) provides the internal MCP server instance configured per-task
