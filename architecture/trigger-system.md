# Trigger System

The trigger system determines when and why tasks are created. Seven trigger types exist, each with distinct coalescing behavior, approval interaction, and context generation.

## Trigger Types

### Manual

The user explicitly dispatches a job via the API. Always creates a new task — never coalesces. Bypasses approval gates. Context includes the HEAD commit hash and any user-provided notes.

**Entry point**: `POST /api/dispatch/{job_id}` → `queue_routes.py`

### Commit (Watch)

A git post-commit hook fires an HTTP callback to the backend. The platform checks which jobs have subscription glob patterns matching the changed files and enqueues a task for each.

**Flow**:
1. Post-commit hook (bash script installed in `.git/hooks/post-commit`) curls `POST /api/hooks/post-commit` with the commit hash
2. `check_watch_triggers()` in `dispatch.py` loads all jobs, checks each job's `subscriptions` patterns against the commit's changed files
3. Pattern matching uses `pathlib.PurePath.match()` — supports `**` recursive globbing
4. For each matched job, a task is enqueued with `commit` trigger

**Coalescing**: coalesces with other pre-execution (pending or queued) `commit`-triggered tasks for the same job. Multiple commits while a job is busy or has a pre-execution task → one catch-up run with all triggers accumulated.

**Context**: includes the commit hash and one-line summary.

### Schedule

A background scheduler evaluates cron expressions. When a job's schedule fires, a task is enqueued.

**Module**: `backend/scheduler.py`

**Flow**:
1. An asyncio task polls every 30 seconds
2. For each job with a non-empty `schedule` property, validates the cron expression via `croniter`
3. Compares the next fire time (after the last recorded fire) against the current time
4. If overdue, enqueues with `schedule` trigger and updates the last-fire timestamp
5. First encounter of a schedule sets the baseline to now without firing — prevents immediate fire on job creation

**Connection safety**: the scheduler makes multiple DB calls per tick (list_jobs, config reads, enqueue) without holding any lock. During project switch, `close_db()` must wait for the scheduler's in-flight operations to complete. See [Storage — Project-Switch Coordination](storage.md#project-switch-coordination).

**Coalescing**: always coalesces globally — any pre-execution task (pending or queued) for the same job absorbs the new trigger. Repeated cron fires while a task is pre-execution produce one run, not many.

**Persistence**: last-fire timestamps are stored in the `config` table (key: `schedule_last_fire_{job_id}`), surviving process restarts.

### Dependency

When a job's task completes successfully, the worker scans for jobs that declare it as an upstream dependency and enqueues tasks for them. A dependent job fires when *any* of its upstream jobs completes — it does not wait for all upstreams.

**Flow**: `_enqueue_dependents()` in `worker.py` — runs after every successful task completion.

**Coalescing**: coalesces with other pre-execution (pending or queued) `dependency`-triggered tasks for the same job. Multiple upstream completions while a dependent has a pre-execution task → one run.

**Circular chains**: circular dependency chains are permitted. Coalescing absorbs the redundant triggers — a job that already has a pre-execution task absorbs the new dependency trigger rather than creating unbounded queue growth.

**Context**: includes the upstream job name, task ID, result commit, and commit range (start..result).

**Not triggered by**: failed, timed-out, cancelled, interrupted, or rejected tasks — only successful completions (no error).

### Agent

Another agent's task programmatically dispatches a job via the `dispatch_task` MCP tool (see [Tool Mediation — Inter-Agent Coordination](tool-mediation.md#inter-agent-coordination)). The dispatching agent provides a message explaining why the target job should run.

**Flow**:
1. An executing agent calls `dispatch_task` through the internal MCP server
2. The platform verifies the target job is in the dispatching job's `allowed_dispatch_targets`
3. A new task is enqueued with `agent` trigger, the dispatching task's identity as `trigger_detail`, and the agent's message as context
4. Self-dispatch (dispatching one's own job) is prohibited

**Coalescing**: coalesces with other pre-execution (pending or queued) `agent`-triggered tasks for the same job.

**Approval**: respects `require_approval` — agent dispatch is automated, not explicit human intent. A job with approval gates will hold agent-dispatched tasks for human review.

**Context**: includes the dispatching task's identity (job name, task ID) and the agent's message.

**Provenance**: the new task's `trigger_detail` references the originating task, creating a traceable chain. The user can trace any agent-dispatched task back to the task that requested it.

**Depth limiting**: a configurable depth limit on agent-initiated dispatch chains prevents runaway cascades where agents recursively dispatch each other.

### Resume

Continues a previous task using the CLI's session resume capability. Creates a new task record with `resume_session_id`. Never coalesces — each resume is distinct intent.

**Entry point**: `POST /api/dispatch/{task_id}/resume`

### Retry

Re-enqueues a failed or timed-out task. Unlike resume, retry resurrects the original record in-place — resets lifecycle fields, appends a retry trigger to the history, resets `created_at` so it doesn't jump ahead in the queue. Never coalesces.

**Entry point**: `POST /api/dispatch/{task_id}/retry`

## Coalescing

Coalescing prevents redundant pre-execution tasks. It checks both pending and queued columns — a new trigger coalesces into whichever matching task exists, regardless of state. The mechanism is unified in `enqueue_task()`:

1. Determine coalescing mode:
   - `schedule` trigger → coalesce globally (any pre-execution task for the job)
   - `coalesce_tasks=true` on the job → coalesce globally regardless of trigger type
   - `commit`, `dependency`, or `agent` trigger → coalesce with same-type pre-execution tasks
   - All others → no coalescing

2. If coalescing: query for an existing pre-execution task (`status IN ('pending', 'queued')`). If found, the new trigger is absorbed and the existing task ID is returned.

3. If not coalescing (or no compatible pre-execution task found): insert a new record. The new task enters pending or queued based on the auto-queueing setting.

The `context` column stores pre-formatted text built at the enqueue site. Context is immutable once written — it captures the state at trigger time, not execution time.

### Coalesced ID Mechanism

Automatic coalescing uses the same `coalesced_id` mechanism as manual merge (see [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition)). Rather than modifying an existing task's record, the system always creates a new atomic task record first (preserving trigger provenance), then sets its `coalesced_id` to point to the existing pre-execution root task. Each trigger retains its own task record — coalescing links them, it does not merge data.

The flow in `enqueue_task()`:
1. Insert the new task record unconditionally (trigger, context, approval all set)
2. If coalescing applies: query for an existing pre-execution root (`coalesced_id IS NULL`, same job, not started, no error) — searches both pending and queued tasks
3. If a root exists: set the new task's `coalesced_id` to the root's ID — the new task becomes a subordinate

The queue-level view filters on `coalesced_id IS NULL`, so subordinate tasks are invisible in the queue. At dispatch time, the worker collects all tasks linked to the root and unifies their context into the prompt.

## Manual Queue Composition

Three user-initiated operations complement automatic coalescing — merge, split, and transfer. These operate on pre-execution tasks (pending or queued) from the Dispatch view. Merge and split are **same-state** operations (within a single column); transfer moves tasks between columns.

### Merge

Combines two pre-execution tasks in the same column for the same job. The oldest task becomes the root; all others get `coalesced_id` set to the root's task ID. Trigger history is preserved — no entries are lost or rewritten.

Constraints: pre-execution tasks only, same column (both pending or both queued), same job only. Cross-job merge would violate the one-task-one-job invariant. Cross-column drag is always a transfer, never a merge.

### Split (Uncoalesce)

Reverses a merge or automatic coalescing. All tasks subordinate to a root (those with `coalesced_id` pointing to it) get `coalesced_id` cleared back to NULL, becoming independent queue entries. Split-off tasks get `sort_order` cleared and `created_at` reset, placing them at the end of the same column as the root. They inherit the job's current `require_approval` setting. Split tasks remain in the same state as the original.

Constraints: only root tasks with subordinates, pre-execution only (not started, no error).

### Transfer

Moves a task between pending and queued columns. Sets or clears `queued_at` accordingly. The transferred task is appended to the end of the target column. Transfer does not merge with or reorder against existing tasks in the target.

See [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition) for implementation details.

## Subscriptions as Dual-Purpose

Subscription glob patterns define a job's relevant files and serve two functions:

- **Context injection**: every task resolves the job's subscription patterns and lists matching files in the prompt as files relevant to the job. The job's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering**: when a commit changes files matching a job's subscription patterns, a task is enqueued. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A job with non-empty subscriptions is watch-active. There is no separate toggle — subscriptions presence = watch enabled.

A job can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) processes the task records that triggers create
- [Job Configuration](job-configuration.md) provides the properties that control trigger behavior (subscriptions, schedule, depends_on, coalesce_tasks, require_approval)
- [Git Integration](git-integration.md) provides the post-commit hook and changed file detection
- [Prompt Assembly](prompt-assembly.md) consumes trigger context for the "why you're running" section
