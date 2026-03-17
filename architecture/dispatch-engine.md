# Dispatch Engine

The dispatch engine is the execution core of mAistro. It enforces the queue-first invariant, processes tasks sequentially, manages the full task lifecycle, and coordinates with the streaming and storage systems.

## Queue-First Invariant

No task may execute without passing through the queue. Every code path — manual dispatch, watch trigger, schedule fire, dependency completion, resume, retry — writes a record to `tasks` first. The worker is the only code path that reads from the queue and invokes execution.

This invariant decouples trigger sources from execution. A trigger's responsibility is to write a queue record with appropriate context; it never needs to know about CLI invocation, streaming, or session management.

## Task Record

Each `tasks` row tracks:

| Column | Purpose |
|--------|---------|
| `job_id` | Which job this task belongs to |
| `trigger` | Primary trigger type (manual, commit, schedule, dependency, resume, retry) |
| `trigger_detail` | Trigger-specific reference (commit hash, cron expression, upstream job ID) |
| `context` | Pre-formatted context text built at the enqueue site |
| `session_id` | Linked chat session (set when execution starts) |
| `resume_session_id` | CLI session ID for resume tasks |
| `approval` | Gate status: `null` (no gate), `pending`, `approved`, `rejected` |
| `start_commit` | HEAD hash when execution began |
| `created_at` | When the task was enqueued |
| `started_at` | When the worker began processing |
| `completed_at` | When execution finished (success, failure, or cancellation) |
| `result_commit` | HEAD hash after execution completed |
| `error` | Error message if failed, timed out, cancelled, interrupted, or rejected |
| `rating` | User-assigned binary rating (positive/negative), null by default |
| `coalesced_id` | Links subordinate tasks to a root task for merge; NULL = standalone/root |

## Worker

**Module**: `backend/worker.py`

The worker is a single `asyncio.Task` running a poll loop. It wakes on notification (via `asyncio.Event`) or every 2 seconds, whichever comes first.

### Processing Modes

The queue operates in two modes controlled by the `queue_auto_dispatch` config:

- **Auto-processing** (`true`): the worker loop continuously pulls the oldest pending task and processes it in order
- **Paused** (`false`): tasks accumulate as pending. The user reorders them via drag-to-reorder in the Dispatch view, then toggles auto-processing when ready

### Sequential Execution

An `asyncio.Lock` guards task processing. Exactly one task runs at a time. The lock is held for the full duration of CLI execution — from session creation through final DB update.

The `_active_task_id` global tracks which task is currently running, enabling cancellation and the "active task blocks project switch" safety invariant.

### Execution Flow

1. **Session creation**: creates (or reuses for resume) a `chat_session` linked to the task
2. **Lifecycle start**: records `started_at`, `start_commit`, and `session_id` on the task record
3. **Timeout watchdog**: spawns an async task that fires the cancellation event after the configured timeout
4. **Task execution**: calls `run_task()` which assembles prompts and invokes the CLI — yields events
5. **Event processing**: raw events go to the audit trail; translated events go to live subscribers; text accumulates for the final chat message. MCP tool invocation events are recorded as structured audit entries
6. **Completion**: records `completed_at` and `result_commit`; triggers dependent jobs if successful
7. **Cleanup**: cancels watchdog, broadcasts `_done` to subscribers, clears active task state

### Cancellation

Cancellation works through an `asyncio.Event` shared with the CLI bridge. Setting the event causes the CLI subprocess to be terminated (SIGTERM, then SIGKILL after 5s grace). Both user-initiated cancellation and timeout use this same mechanism.

### Stale Sweep

On startup (once per project open), the worker marks any tasks that are `started_at IS NOT NULL AND completed_at IS NULL` as interrupted. This handles the case where the process died mid-task.

### Dependent Job Propagation

After successful completion (no error, no timeout), the worker scans all jobs for those declaring the completed job in their `depends_on` list. For each match, it enqueues a new task with `dependency` trigger, including context about the upstream job, its commit range, and outcome summary. A dependent job fires when *any* of its upstream jobs completes — it does not wait for all upstreams.

Circular dependency chains are safe: coalescing absorbs redundant triggers, and sequential execution ensures no concurrent amplification. A cycle produces at most one pending task per job at any time.

Timed-out and failed tasks explicitly do not trigger dependents.

## Approval Gates

Jobs with `require_approval=true` get `approval='pending'` on task enqueue — except manual dispatches, which bypass the gate (manual = explicit human intent). The worker's pending task query (`get_oldest_pending_task`) skips rows where `approval='pending'`.

Approval and rejection are API operations that propagate to the full coalesce group:
- **Approve**: sets `approval='approved'` on the root task and all subordinates with `approval='pending'`, then wakes the worker
- **Reject**: sets `approval='rejected'` and marks as completed with error "rejected" — on both the root task and all pending-approval subordinates

Processing a specific task via `process_one()` auto-approves pending-approval tasks (explicit intent, same rationale as manual dispatch).

## Commit Tracking (No Auto-Commit)

The worker captures `start_commit` (HEAD at task start) and `result_commit` (HEAD at task end) to track what an agent produced. These bookend the agent's work — if `start_commit == result_commit`, the agent made no commits.

**The platform does not auto-commit.** Agents have structured git tools via the internal MCP server (see [Tool Mediation](tool-mediation.md)) and are instructed to commit in the system prompt. The platform trusts agents to commit their own work. Rationale:

- **Authorship integrity**: every commit carries the job's identity (`[JobName]` prefix, job-specific author). An auto-commit would break this — the platform would have to guess what message and authorship to apply.
- **Atomic intent**: agents decide what constitutes a logical commit. They may make multiple commits for distinct changes or one commit for related changes. Auto-commit would force a single "catch-all" commit with no meaningful message.
- **Parallel execution future**: if tasks ever run concurrently, auto-commit becomes intractable — the working tree contains interleaved changes from multiple agents, and there's no way to attribute which changes belong to which task.
- **Observable failure**: when `start_commit == result_commit` but the agent was supposed to produce changes, the task output and audit trail reveal what happened. This is more useful than silently committing unknown changes.

The commit range (`start_commit..result_commit`) feeds into dependency trigger context — downstream jobs see exactly which commits their upstream produced.

## Dispatch Outcomes

When a task completes, the platform derives an outcome summary and supports user rating. These serve both the operator (scan completed work at a glance) and downstream agents (understand what upstream actually produced).

### Outcome Summary

The summary is computed from git artifacts — the commits between `start_commit` and `result_commit`. It captures commit messages and change statistics. This is a derived value, not an authored one: the platform reads what the repository records, not what the agent claims.

- If `start_commit == result_commit`, the task produced no commits and has no summary
- The summary is computed at completion time and stored (or derived on read) for display in the Dispatch view
- When this task triggers downstream dependents, the outcome summary is included in the trigger context — downstream agents receive concrete information about what their upstream produced

### Task Rating

The user can rate a completed task with a binary signal (positive or negative). Ratings are stored on the task record (`rating` column on `tasks`). The default is null (unrated). Ratings are never inferred or auto-assigned — they require explicit user action.

The rating dataset can later be correlated with job instructions, model choices, and trigger patterns. The platform stores the signal; analysis is a future concern.

## Manual Queue Composition

The user can merge and split pending tasks directly from the Dispatch view. This gives explicit control over the grouping that automatic coalescing performs implicitly.

### Implementation: Coalesced ID

Rather than destructively removing task records during merge (losing individual trigger provenance), the system uses a **`coalesced_id` column** on `tasks`. This preserves every task as an atomic record while linking them for unified execution.

| `coalesced_id` value | Meaning |
|----------------------|---------|
| `NULL` | Standalone task — visible in queue, dispatched independently |
| `<task_id>` | Subordinate task — linked to the root task identified by this ID |

**Queue rendering**: the queue shows all tasks where `coalesced_id IS NULL`. These are either standalone tasks or root tasks that have subordinates linked to them.

**Dispatch collection**: when the worker pulls a task for execution, it collects all tasks whose `coalesced_id` equals the dispatched task's ID. The context from all collected tasks is unified into the prompt. Each subordinate task's context is preserved verbatim — merge does not rewrite history.

**Route behavior**: API routes that act on a specific task ID automatically consider all tasks with `coalesced_id` equal to that ID. This means cancellation, approval, and other lifecycle operations propagate to the full group.

### Merge

Combines two pending tasks for the same job into a single logical unit. Triggered by dragging one pending task onto another in the Dispatch view (see [Frontend — Drag Interaction Model](frontend.md)).

1. The older task (by `created_at`) becomes the root — its `coalesced_id` remains NULL
2. The dragged task gets `coalesced_id` set to the root task's ID
3. The root task retains its queue position; the subordinate task becomes invisible in the queue view
4. **Flatten**: if the newly subordinated task had its own subordinates (was itself a root), those subordinates are re-pointed to the new root. This ensures coalesce groups are always flat — depth 1, never nested. Every subordinate points directly to its root.

**Constraints**:
- Only pre-execution tasks (not started, not completed, not pending-approval)
- Same job only — a task's identity is bound to one job; cross-job merge would break prompt assembly, tool configuration, and commit authorship. The UI enforces this structurally by suppressing the merge affordance when tasks belong to different jobs
- Tasks that are already subordinates (have a non-null `coalesced_id`) cannot be merge targets — they must be split from their current root first

### Split (Uncoalesce)

Reverses a merge or automatic coalescing. Takes a root task that has subordinate tasks and makes them independent again.

1. All tasks with `coalesced_id` equal to the root task's ID get `coalesced_id` set back to NULL
2. Split-off tasks get `sort_order` cleared to NULL and `created_at` reset to the current time — they appear at the end of the queue as if newly created
3. Split-off tasks inherit the job's current `require_approval` setting — if the gate is now enabled, they enter pending-approval state even if the original merge happened before the gate was set

**Constraints**:
- Only root tasks (coalesced_id IS NULL) that have at least one subordinate can be split
- Only pre-execution tasks (started_at IS NULL and no error)
- The root task itself is unchanged — it retains its position, trigger, and context. Only subordinates are released

### Why Coalesced ID Over Record Deletion

The coalesced_id approach has structural advantages:

- **Atomic provenance**: every trigger that created a task retains its own record. Audit trails remain complete without relying on JSON array archaeology
- **Reversibility**: split is a column update, not record reconstruction. No information is lost during merge that must be recreated during split
- **Route simplicity**: "act on all tasks with this coalesced_id" is a single WHERE clause, uniformly applied across all endpoints
- **Automatic coalescing alignment**: the same mechanism can back automatic coalescing — instead of appending to a JSON array, create a new record with `coalesced_id` pointing to the existing pending task

## Retry and Resume

- **Retry**: resurrects the original task record — resets all lifecycle fields (`started_at`, `completed_at`, `error`, commits, session), resets `created_at` to now (so it doesn't jump ahead in the queue), appends a retry trigger. The task ID is preserved.
- **Resume**: creates a new task record with `resume_session_id` set to the original CLI session ID. The worker passes this to the CLI's `--resume` flag. If the original chat session still exists, it's reused.

## Relationship to Other Systems

- [Trigger System](trigger-system.md) writes queue records; the dispatch engine reads and processes them
- [CLI Bridge](cli-bridge.md) is invoked by `run_task()` — the engine manages the lifecycle around it
- [Prompt Assembly](prompt-assembly.md) builds the prompts that `run_task()` feeds to the CLI
- [Streaming and Sessions](streaming-and-sessions.md) receives broadcast events from the worker and stores durable output
- [Git Integration](git-integration.md) provides `head_hash` for commit tracking and `changed_files_in_commit` for dependency context
- [Tool Mediation](tool-mediation.md) provides the internal MCP server instance configured per-task
