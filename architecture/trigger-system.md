# Trigger System

The trigger system determines when and why **triggers** are created (the rows that hit the queue and drive a job to run). Nine trigger types exist, each with distinct coalescing behavior, approval interaction, and context generation.

> **Vocabulary note.** What this doc calls a *trigger* is what the SQL schema still calls a `tasks` row (and what a `task_id` path param points at). The Python and frontend layers were renamed in Stage 1 of [triggers-and-dispatches](proposals/triggers-and-dispatches.md); the SQL rename is deferred. Where this doc shows `coalesced_id`, `trigger`, `trigger_detail`, or `task_id`, those are real column / param names — read them as living on the trigger record.

## Trigger Types

### Manual

The user explicitly dispatches a job via the API. Always creates a new trigger — never coalesces. Bypasses approval gates. Context includes the HEAD commit hash and any user-provided notes.

**Entry point**: `POST /api/triggers/{job_id}` → `queue_routes.py`

### Commit (Watch)

A git post-commit hook fires an HTTP callback to the backend. The platform checks which jobs have subscription glob patterns matching the changed files and enqueues a trigger for each.

**Flow**:
1. Post-commit hook (bash script installed in `.git/hooks/post-commit`) curls `POST /api/hooks/post-commit` with the commit hash
2. `check_watch_triggers()` in `dispatch.py` loads all jobs, checks each job's `subscriptions` patterns against the commit's changed files (glob matching in `backend/matching.py`)
3. `is_ancestor(commit, HEAD)` filters out task-branch commits — watch only fires on integrations into main
4. For each matched job, a trigger is enqueued with `commit` type

**Coalescing**: coalesces with other pre-execution (pending or queued) `commit`-typed triggers for the same job. Multiple commits while a job is busy or has a pre-execution trigger → one catch-up run with all triggers accumulated.

**Context**: includes the commit hash and one-line summary.

### Schedule

A background scheduler evaluates cron expressions. When a job's schedule fires, a trigger is enqueued.

**Module**: `backend/scheduler.py`

**Flow**:
1. An asyncio task polls every 30 seconds
2. For each job with a non-empty `schedule` property, validates the cron expression via `croniter`
3. Compares the next fire time (after the last recorded fire) against the current time
4. If overdue, enqueues with `schedule` type and updates the last-fire timestamp
5. First encounter of a schedule sets the baseline to now without firing — prevents immediate fire on job creation

**Connection safety**: the scheduler makes multiple DB calls per tick (list_jobs, config reads, enqueue) without holding any lock. During project switch, `close_db()` must wait for the scheduler's in-flight operations to complete. See [Storage — Project-Switch Coordination](storage.md#project-switch-coordination).

**Coalescing**: always coalesces globally — any pre-execution trigger (pending or queued) for the same job absorbs the new one. Repeated cron fires while one is pre-execution produce one run, not many.

**Persistence**: last-fire timestamps are stored in the `config` table (key: `schedule_last_fire_{job_id}`), surviving process restarts.

### Dependency

When a job's dispatch completes successfully, the worker scans for jobs that declare it as an upstream dependency and enqueues triggers for them. A dependent job fires when *any* of its upstream jobs completes — it does not wait for all upstreams.

**Flow**: `_enqueue_dependents()` in `worker.py` — runs after every successful dispatch completion.

**Coalescing**: coalesces with other pre-execution (pending or queued) `dependency`-typed triggers for the same job. Multiple upstream completions while a dependent has a pre-execution trigger → one run.

**Circular chains**: circular dependency chains are permitted. Coalescing absorbs the redundant entries — a job that already has a pre-execution trigger absorbs the new dependency rather than creating unbounded queue growth.

**Context**: includes the upstream job name, trigger ID, result commit, and commit range (start..result).

**Not triggered by**: failed, timed-out, cancelled, interrupted, or rejected dispatches — only successful completions (no error).

### Agent

Another agent's dispatch programmatically targets a job via the `dispatch_task` MCP tool (see [Tool Mediation — Inter-Agent Coordination](tool-mediation.md#inter-agent-coordination)). The dispatching agent provides a message explaining why the target job should run.

**Flow**:
1. An executing agent calls `dispatch_task` through the internal MCP server
2. The platform verifies the target job is in the dispatching job's `allowed_dispatch_targets`
3. A new trigger is enqueued with `agent` type, the dispatching trigger's identity as `trigger_detail`, and the agent's message as context
4. Self-dispatch (dispatching one's own job) is prohibited

**Coalescing**: coalesces with other pre-execution (pending or queued) `agent`-typed triggers for the same job.

**Approval**: respects `require_approval` — agent dispatch is automated, not explicit human intent. A job with approval gates will hold agent-dispatched triggers for human review.

**Context**: includes the dispatching trigger's identity (job name, trigger ID) and the agent's message.

**Provenance**: the new trigger's `trigger_detail` references the originating one, creating a traceable chain. The operator can trace any agent-dispatched trigger back to its origin.

**Depth limiting**: a configurable depth limit on agent-initiated dispatch chains prevents runaway cascades where agents recursively dispatch each other.

### Resume

Continues a previous dispatch using the CLI's session resume capability. Creates a new trigger record with `resume_session_id` and inverts the coalesce relationship — see [Inverted Coalescing](#inverted-coalescing) below. Reuses the original worktree rather than cutting a new branch.

**Entry point**: `POST /api/triggers/{task_id}/resume`

### Reply

Creates a follow-up trigger targeting a resolved one. The user provides additional context or instructions. The reply carries the original's commit range in its context and inverts the coalesce relationship — see [Inverted Coalescing](#inverted-coalescing) below.

**Entry point**: `POST /api/triggers/{task_id}/reply`

### Self-requeue

An executing agent voluntarily requests continuation via the `requeue_self` internal MCP tool (gated by the job's `allow_self_requeue` property). Use cases: clarification (blocked, needs operator input) or natural breakpoint (finished a discrete chunk; the next chunk wants its own dispatch with fresh context window and a fresh worktree).

**Entry point**: agent → internal MCP → `POST /api/triggers/self-requeue` (internal)

**Coalescing**: never coalesces.

**Throttle**: after 5 consecutive `self_requeue` triggers on a job, `enqueue_trigger` forces the next one to `pending` (operator must approve) and emits a `continuation_throttled` event. Shared infrastructure with `auto_continue` — see `_CONTINUATION_TRIGGERS` in `db_triggers.py`.

### Auto-continue

The worker spawns a continuation when a dispatch ends in `exhausted` (turn-limit) or `timed_out`, *if* the job opts in via `auto_continue`. The continuation inherits the prior worktree — the agent picks up where it was killed mid-thought, with a fresh conversation.

**Entry point**: worker terminal handler (no HTTP route — fully internal)

**Coalescing**: never coalesces.

**Throttle**: 2 consecutive `auto_continue` triggers before forced-pending. Stricter than `self_requeue` because the trigger is involuntary — a misconfigured job exhausting in a loop should land on the operator's desk fast.

> Retry (re-enqueueing a failed dispatch) is not a separate trigger type. Re-running failed work is the operator's call: use **reply** to add corrective context, or **resume** to continue the session. The only `/retry`-like surface left is the manual workspace re-merge for preserved task branches.

## Inverted Coalescing (Reply and Resume)

Reply and resume use the same `coalesced_id` mechanism as normal coalescing but **invert the direction**: the new trigger becomes the root and the original becomes a subordinate. This is the opposite of normal coalescing, where new triggers subordinate to existing roots.

The inversion is intentional. The newest trigger in a reply/resume chain is the one the worker should dispatch — it carries the latest intent. The original (and any earlier chain members) become subordinates whose contexts are collected into the prompt.

### Chain Behavior

When replies chain (A → reply B → reply C):

1. A is created as a standalone trigger (`coalesced_id = NULL`)
2. B replies to A: `coalesce_under(A, B)` — A becomes subordinate to B
3. C replies to B: `coalesce_under(B, C)` — B becomes subordinate to C, then the flatten step re-points A from B to C

Result: C is root (`coalesced_id = NULL`), A and B are both flat subordinates of C. The depth-1 invariant holds — no nested coalesce chains. SQLite triggers (`tasks_depth1_insert`, `tasks_depth1_update_target`, `tasks_depth1_update_self`) enforce this at the storage layer.

When C is dispatched, the worker collects subordinates A and B. The prompt includes:
- C's context: "Reply to trigger #B — [user notes]"
- B's context: "Reply to trigger #A — [user notes]"
- A's context: the original trigger (manual, commit, etc.)

The full provenance chain is readable through the subordinate contexts, ordered by creation time. Each trigger's `trigger_detail` references its predecessor by ID, making the chain traversable.

### Structural Properties

- **Inverted root**: the newest trigger is always the root. The worker dispatches the most recent intent.
- **Flat subordinates**: the flatten step re-points all prior chain members to the new root. No depth > 1.
- **Complete provenance**: every trigger in the chain retains its own record with original type, context, and timestamps. No data is lost or rewritten.
- **Resume session threading**: resume triggers carry `resume_session_id` which the worker passes to the CLI's `--resume` flag. The session context from the original dispatch is restored by the CLI, not reconstructed from subordinate contexts.
- **Terminal subtree immutability**: every coalesce-mutating op (split, uncoalesce, merge) requires all participants to be `pending`. A terminal coalesced subtree is structurally frozen — reply/resume unlock it only by introducing a new non-terminal root above it.

## Coalescing

Coalescing prevents redundant pre-execution triggers. It checks both pending and queued columns — a new trigger coalesces into whichever matching one exists, regardless of state. The mechanism is unified in `enqueue_trigger()` (`backend/db_triggers.py`):

1. Determine coalescing mode:
   - `schedule` type → coalesce globally (any pre-execution trigger for the job)
   - `coalesce_tasks=true` on the job → coalesce globally regardless of type
   - `commit`, `dependency`, or `agent` type → coalesce with same-type pre-execution triggers
   - `manual`, `resume`, `reply`, `self_requeue`, `auto_continue` → never coalesce

2. If coalescing: query for an existing pre-execution trigger (`status IN ('pending', 'queued')`). If found, the new entry is absorbed and the existing trigger ID is returned.

3. If not coalescing (or no compatible pre-execution trigger found): insert a new record. The trigger enters pending or queued based on the auto-queueing setting.

The `context` column stores pre-formatted text built at the enqueue site. Context is immutable once written — it captures the state at trigger time, not execution time.

### Coalesced ID Mechanism

Automatic coalescing uses the same `coalesced_id` mechanism as manual merge (see [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition)). Rather than modifying an existing record, the system always creates a new atomic trigger record first (preserving provenance), then sets its `coalesced_id` to point at the existing pre-execution root. Each trigger retains its own record — coalescing links them, it does not merge data.

The flow:
1. Insert the new trigger record unconditionally (type, context, approval all set)
2. If coalescing applies: query for an existing pre-execution root (`coalesced_id IS NULL`, same job, pending or queued) — searches both columns
3. If a root exists: set the new trigger's `coalesced_id` to the root's ID — the new entry becomes a subordinate

The queue-level view filters on `coalesced_id IS NULL`, so subordinate triggers are invisible in the queue. At dispatch time, the worker collects all triggers linked to the root and unifies their context into the prompt. Outcome data lives on the root via `task_executions` — subordinates have no execution row of their own.

## Manual Queue Composition

Three user-initiated operations complement automatic coalescing — merge, split, and transfer. These operate on pre-execution triggers (pending or queued) from the Dispatch view. Merge and split are **same-state** operations (within a single column); transfer moves triggers between columns.

### Merge

Combines two pre-execution triggers in the same column for the same job. The oldest becomes the root; all others get `coalesced_id` set to the root's ID. Type history is preserved — no entries are lost or rewritten.

Constraints: pre-execution triggers only, same column (both pending or both queued), same job only. Cross-job merge would violate the one-trigger-one-job invariant. Cross-column drag is always a transfer, never a merge.

### Split (Uncoalesce)

Reverses a merge or automatic coalescing. All triggers subordinate to a root (those with `coalesced_id` pointing to it) get `coalesced_id` cleared back to NULL, becoming independent queue entries. Split-off triggers get `sort_order` cleared and `created_at` reset, placing them at the end of the same column as the root. They inherit the job's current `require_approval` setting. Split triggers remain in the same state as the original.

Constraints: only root triggers with subordinates, pre-execution only (all participants must be `pending`; `transfer_trigger` accepts `pending` or `queued` but only on roots).

### Transfer

Moves a trigger between pending and queued columns. Sets or clears `queued_at` accordingly. The transferred trigger is appended to the end of the target column. Transfer does not merge with or reorder against existing entries in the target.

See [Dispatch Engine — Manual Queue Composition](dispatch-engine.md#manual-queue-composition) for implementation details.

## Subscriptions as Dual-Purpose

Subscription glob patterns define a job's relevant files and serve two functions:

- **Context injection**: every dispatch resolves the job's subscription patterns and lists matching files in the prompt as files relevant to the job. The job's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering**: when a commit changes files matching a job's subscription patterns, a trigger is enqueued. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A job with non-empty subscriptions is watch-active. There is no separate toggle — subscriptions presence = watch enabled.

A job can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) processes the trigger records that this system creates
- [Job Configuration](job-configuration.md) provides the properties that control trigger behavior (`subscriptions`, `schedule`, `depends_on`, `coalesce_tasks`, `require_approval`, `allow_self_requeue`, `auto_continue`, `allowed_dispatch_targets`)
- [Git Integration](git-integration.md) provides the post-commit hook, changed-file detection, and `is_ancestor` filter that prevents task-branch commits from firing watch
- [Prompt Assembly](prompt-assembly.md) consumes trigger context for the "why you're running" section
- [Task Lifecycle](task-lifecycle.md) defines how a trigger transitions from pending through terminal, and how outcomes land on `task_executions`
