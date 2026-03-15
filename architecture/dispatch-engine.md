# Dispatch Engine

The dispatch engine is the execution core of mAistro. It enforces the queue-first invariant, processes dispatches sequentially, manages the full dispatch lifecycle, and coordinates with the streaming and storage systems.

## Queue-First Invariant

No dispatch may execute without passing through the queue. Every code path — manual dispatch, watch trigger, schedule fire, dependency completion, resume, retry — writes a record to `dispatch_queue` first. The worker is the only code path that reads from the queue and invokes execution.

This invariant decouples trigger sources from execution. A trigger's job is to write a queue record with appropriate context; it never needs to know about CLI invocation, streaming, or session management.

## Dispatch Queue Record

Each `dispatch_queue` row tracks:

| Column | Purpose |
|--------|---------|
| `task_id` | Which task this dispatch belongs to |
| `trigger` | Primary trigger type (manual, commit, schedule, dependency, resume, retry) |
| `trigger_detail` | Trigger-specific reference (commit hash, cron expression, upstream task ID) |
| `triggers` | JSON array of all trigger entries (supports coalescing — multiple triggers on one dispatch) |
| `session_id` | Linked chat session (set when execution starts) |
| `resume_session_id` | CLI session ID for resume dispatches |
| `approval` | Gate status: `null` (no gate), `pending`, `approved`, `rejected` |
| `start_commit` | HEAD hash when execution began |
| `created_at` | When the dispatch was enqueued |
| `started_at` | When the worker began processing |
| `completed_at` | When execution finished (success, failure, or cancellation) |
| `result_commit` | HEAD hash after execution completed |
| `error` | Error message if failed, timed out, cancelled, interrupted, or rejected |

## Worker

**Module**: `backend/worker.py`

The worker is a single `asyncio.Task` running a poll loop. It wakes on notification (via `asyncio.Event`) or every 2 seconds, whichever comes first.

### Processing Modes

The queue operates in two modes controlled by the `queue_auto_dispatch` config:

- **Auto-processing** (`true`): the worker loop continuously pulls the oldest pending dispatch and processes it
- **Manual** (`false`): the loop runs but skips processing. Dispatches execute only when explicitly triggered via `process_next()`, `process_one()`, or `process_all()`

### Sequential Execution

An `asyncio.Lock` guards dispatch processing. Exactly one dispatch runs at a time. The lock is held for the full duration of CLI execution — from session creation through final DB update.

The `_active_dispatch_id` global tracks which dispatch is currently running, enabling cancellation and the "active dispatch blocks project switch" safety invariant.

### Execution Flow

1. **Session creation**: creates (or reuses for resume) a `chat_session` linked to the dispatch
2. **Lifecycle start**: records `started_at`, `start_commit`, and `session_id` on the dispatch record
3. **Timeout watchdog**: spawns an async task that fires the cancellation event after the configured timeout
4. **Dispatch execution**: calls `run_dispatch()` which assembles prompts and invokes the CLI — yields events
5. **Event processing**: raw events go to the audit trail; translated events go to live subscribers; text accumulates for the final chat message. MCP tool invocation events are recorded as structured audit entries
6. **Completion**: records `completed_at` and `result_commit`; triggers dependent tasks if successful
7. **Cleanup**: cancels watchdog, broadcasts `_done` to subscribers, clears active dispatch state

### Cancellation

Cancellation works through an `asyncio.Event` shared with the CLI bridge. Setting the event causes the CLI subprocess to be terminated (SIGTERM, then SIGKILL after 5s grace). Both user-initiated cancellation and timeout use this same mechanism.

### Stale Sweep

On startup (once per project open), the worker marks any dispatches that are `started_at IS NOT NULL AND completed_at IS NULL` as interrupted. This handles the case where the process died mid-dispatch.

### Dependent Task Propagation

After successful completion (no error, no timeout), the worker scans all tasks for those declaring the completed task in their `depends_on` list. For each match, it enqueues a new dispatch with `dependency` trigger, including context about the upstream task and its commit range. A dependent task fires when *any* of its upstream tasks completes — it does not wait for all upstreams.

Circular dependency chains are safe: coalescing absorbs redundant triggers, and sequential execution ensures no concurrent amplification. A cycle produces at most one pending dispatch per task at any time.

Timed-out and failed dispatches explicitly do not trigger dependents.

## Approval Gates

Tasks with `require_approval=true` get `approval='pending'` on enqueue — except manual dispatches, which bypass the gate (manual = explicit human intent). The worker's pending dispatch query (`get_oldest_pending_dispatch`) skips rows where `approval='pending'`.

Approval and rejection are API operations:
- **Approve**: sets `approval='approved'`, wakes the worker
- **Reject**: sets `approval='rejected'`, marks as completed with error "rejected"

Manual processing via `process_one()` auto-approves pending-approval dispatches (explicit intent, same rationale as manual dispatch).

## Commit Tracking (No Auto-Commit)

The worker captures `start_commit` (HEAD at dispatch start) and `result_commit` (HEAD at dispatch end) to track what an agent produced. These bookend the agent's work — if `start_commit == result_commit`, the agent made no commits.

**The platform does not auto-commit.** Agents have structured git tools via the internal MCP server (see [Tool Mediation](tool-mediation.md)) and are instructed to commit in the system prompt. The platform trusts agents to commit their own work. Rationale:

- **Authorship integrity**: every commit carries the task's identity (`[TaskName]` prefix, task-specific author). An auto-commit would break this — the platform would have to guess what message and authorship to apply.
- **Atomic intent**: agents decide what constitutes a logical commit. They may make multiple commits for distinct changes or one commit for related changes. Auto-commit would force a single "catch-all" commit with no meaningful message.
- **Parallel dispatch future**: if dispatches ever run concurrently, auto-commit becomes intractable — the working tree contains interleaved changes from multiple agents, and there's no way to attribute which changes belong to which dispatch.
- **Observable failure**: when `start_commit == result_commit` but the agent was supposed to produce changes, the dispatch output and audit trail reveal what happened. This is more useful than silently committing unknown changes.

The commit range (`start_commit..result_commit`) feeds into dependency trigger context — downstream tasks see exactly which commits their upstream produced.

## Retry and Resume

- **Retry**: resurrects the original dispatch record — resets all lifecycle fields (`started_at`, `completed_at`, `error`, commits, session), resets `created_at` to now (so it doesn't jump ahead in the queue), appends a retry trigger to the triggers array. The dispatch ID is preserved.
- **Resume**: creates a new dispatch record with `resume_session_id` set to the original CLI session ID. The worker passes this to the CLI's `--resume` flag. If the original chat session still exists, it's reused.

## Relationship to Other Systems

- [Trigger System](trigger-system.md) writes queue records; the dispatch engine reads and processes them
- [CLI Bridge](cli-bridge.md) is invoked by `run_dispatch()` — the engine manages the lifecycle around it
- [Prompt Assembly](prompt-assembly.md) builds the prompts that `run_dispatch()` feeds to the CLI
- [Streaming and Sessions](streaming-and-sessions.md) receives broadcast events from the worker and stores durable output
- [Git Integration](git-integration.md) provides `head_hash` for commit tracking and `changed_files_in_commit` for dependency context
- [Tool Mediation](tool-mediation.md) provides the internal MCP server instance configured per-dispatch
