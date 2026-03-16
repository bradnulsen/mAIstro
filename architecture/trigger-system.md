# Trigger System

The trigger system determines when and why tasks are created. Six trigger types exist, each with distinct coalescing behavior, approval interaction, and context generation.

## Trigger Types

### Manual

The user explicitly dispatches a job via the API. Always creates a new task — never coalesces. Bypasses approval gates. Context includes the HEAD commit hash and any user-provided notes.

**Entry point**: `POST /api/dispatch/{task_id}` → `queue_routes.py`

### Commit (Watch)

A git post-commit hook fires an HTTP callback to the backend. The platform checks which jobs have subscription glob patterns matching the changed files and enqueues a task for each.

**Flow**:
1. Post-commit hook (bash script installed in `.git/hooks/post-commit`) curls `POST /api/hooks/post-commit` with the commit hash
2. `check_watch_triggers()` in `dispatch.py` loads all jobs, checks each job's `subscriptions` patterns against the commit's changed files
3. Pattern matching uses `pathlib.PurePath.match()` — supports `**` recursive globbing
4. For each matched job, a task is enqueued with `commit` trigger

**Coalescing**: coalesces with other pending `commit`-triggered tasks for the same job. Multiple commits while a job is busy or pending → one catch-up run with all triggers accumulated.

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

**Coalescing**: always coalesces globally — any pending task for the same job absorbs the new trigger. Repeated cron fires while a task is pending produce one run, not many.

**Persistence**: last-fire timestamps are stored in the `config` table (key: `schedule_last_fire_{task_id}`), surviving process restarts.

### Dependency

When a job's task completes successfully, the worker scans for jobs that declare it as an upstream dependency and enqueues tasks for them. A dependent job fires when *any* of its upstream jobs completes — it does not wait for all upstreams.

**Flow**: `_enqueue_dependents()` in `worker.py` — runs after every successful task completion.

**Coalescing**: coalesces with other pending `dependency`-triggered tasks for the same job. Multiple upstream completions while a dependent is pending → one run.

**Circular chains**: circular dependency chains are permitted. Coalescing absorbs the redundant triggers — a job that already has a pending task absorbs the new dependency trigger rather than creating unbounded queue growth.

**Context**: includes the upstream job name, task ID, result commit, and commit range (start..result).

**Not triggered by**: timed-out tasks, failed tasks, cancelled tasks — only clean completions.

### Resume

Continues a previous task using the CLI's session resume capability. Creates a new task record with `resume_session_id`. Never coalesces — each resume is distinct intent.

**Entry point**: `POST /api/dispatch/{dispatch_id}/resume`

### Retry

Re-enqueues a failed or timed-out task. Unlike resume, retry resurrects the original record in-place — resets lifecycle fields, appends a retry trigger to the history, resets `created_at` so it doesn't jump ahead in the queue. Never coalesces.

**Entry point**: `POST /api/dispatch/{dispatch_id}/retry`

## Coalescing

Coalescing prevents redundant pending tasks. The mechanism is unified in `enqueue_dispatch()`:

1. Determine coalescing mode:
   - `schedule` trigger → coalesce globally (any pending task for the job)
   - `coalesce_dispatches=true` on the job → coalesce globally regardless of trigger type
   - `commit` or `dependency` trigger → coalesce with same-type pending tasks
   - All others → no coalescing

2. If coalescing: query for an existing pending task (not started, no error). If found, append the new trigger entry to its `triggers` JSON array and return the existing task ID.

3. If not coalescing (or no compatible pending task found): insert a new record.

The `triggers` column accumulates all trigger entries that contributed to a task. Each entry has `trigger` (type), `detail` (reference), and `context` (pre-formatted string built at the enqueue site). Context is immutable once written — it captures the state at trigger time, not execution time.

## Subscriptions as Dual-Purpose

Subscription glob patterns define a job's relevant files and serve two functions:

- **Context injection**: every task resolves the job's subscription patterns and lists matching files in the prompt as files relevant to the job. The job's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering**: when a commit changes files matching a job's subscription patterns, a task is enqueued. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A job with non-empty subscriptions is watch-active. There is no separate toggle — subscriptions presence = watch enabled.

A job can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) processes the task records that triggers create
- [Job Configuration](job-configuration.md) provides the properties that control trigger behavior (subscriptions, schedule, depends_on, coalesce_dispatches, require_approval)
- [Git Integration](git-integration.md) provides the post-commit hook and changed file detection
- [Prompt Assembly](prompt-assembly.md) consumes trigger context for the "why you're running" section
