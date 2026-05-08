# Proposal: Agent Self-Requeue

## Status

Draft.

## Summary

Give agents an MCP tool to enqueue a new task on their own job. Two use cases: **clarification** (agent is blocked, needs operator input before proceeding) and **natural breakpoint** (agent finished a discrete chunk, the next chunk is its own dispatch — better isolation, fresh context window, fresh worktree).

The mechanism is the same atomic enqueue path that already backs `dispatch_task` (cross-job agent dispatch). Self-targeting is the only structural difference, and the operator-facing semantics are distinct enough to warrant a separate MCP tool name and a new trigger kind so the kanban can render the two use cases without ambiguity.

A throttle in `enqueue_task` guards against runaway self-requeue loops.

## Goals

- Let agents declare "I'm done for now, queue me up again" without needing an external trigger.
- Preserve the operator-driven kanban contract: every self-requeue produces a visible task that the operator can edit, transfer, coalesce, or cancel before it runs.
- Bound the autonomy: an agent that self-requeues without progress can't drain tokens forever.
- Reuse the existing `enqueue_task` chokepoint so the new trigger kind picks up approval gating, auto-queue handling, and coalescing rules with no special-casing.

## Non-goals (scope guard)

If the implementation pulls any of these in, treat it as a red flag.

- **Cross-job dispatch.** The existing `dispatch_task` MCP tool ([mcp_server.py:241](../../backend/mcp_server.py#L241)) keeps its current shape and gating (`allowed_dispatch_targets`, self-dispatch prohibited). This proposal adds a *separate* tool; the two operate independently.
- **Worker concurrency or worktree lifecycle.** Self-requeue produces a normal task that the worker picks up later in its own dispatch with its own worktree. No mid-run handoff, no shared state.
- **Operator UX surface.** Throttled tasks land in `pending` and carry a `task_events` row; that's the entire operator surface. No new kanban indicators, no Governor finding shape, no toast.
- **Loops across jobs.** This proposal only addresses self-requeue runaways. A→B→A oscillation across two jobs via `dispatch_task` is a separate problem and out of scope.

## Conceptual Model

### The tool

A new MCP tool `requeue_self`, gated by a new job property `allow_self_requeue` (boolean, default `false`). When the property is false, the tool is not registered for that job — the agent doesn't see it at all. This matches the existing pattern for `dispatch_task` (only registered when `allowed_dispatch_targets` is non-empty).

Tool description (proposed):

> Enqueue a new task on this same job, to be picked up after the current dispatch terminates. Use this when (1) you are blocked and need operator input that would change the task's framing, or (2) you have completed a discrete chunk of work and the next chunk should run as its own dispatch with a fresh context window. Include a clear message describing what state you reached and what should happen next — that message becomes the new task's context. Self-requeue is rate-limited; do not loop on it.

Input schema: a single required `message` string. The message becomes the new task's `context`.

### The trigger kind

A new value in the `tasks.trigger` enum: `self_requeue`. Distinct from `agent` (which means "another job's agent dispatched me") so the kanban / detail drawer can label these clearly and the throttle counter can match on it.

The new kind composes cleanly with the existing rules in [`enqueue_task`](../../backend/db_tasks.py#L78):

- **Approval:** honors the job's `require_approval` property. Same as `agent`, `commit`, `schedule`, `cascade`, `dependency`, `resume`, `reply`. Only `manual` bypasses.
- **Coalescing:** never auto-coalesces. Same as `manual`, `resume`, `reply`. (The operator can still drag-drop coalesce a self-requeue task by hand from the kanban.)
- **Auto-queue:** honors the global `queue_auto_dispatch` setting like every other trigger — *except* when the throttle fires (see below).

### The throttle

Hosted in `enqueue_task`. When a `self_requeue` task is being enqueued for job J:

1. Read the most recent N tasks on J (any status), ordered by `created_at` descending. N = 5.
2. If all N are `trigger='self_requeue'`, the throttle fires:
   - Force `status='pending'` regardless of `queue_auto_dispatch`.
   - Skip the `'queued'` lifecycle event.
   - Write an additional `task_events` row: `event='self_requeue_throttled'`, `detail` = N (the run length).

The new task lands in the pending column, where the operator notices it because (a) the auto-dispatched flow stopped delivering throughput on this job, and (b) clicking the task surfaces the `self_requeue_throttled` event in its history.

**Reset rule:** any non-self trigger landing on the job (manual, commit, schedule, cascade, agent, dependency, resume, reply) breaks the run. The next self_requeue's window of 5 starts fresh from that point.

The reset rule is implicit in the "last 5 tasks all `self_requeue`" check — no separate counter, no separate state. The check is one indexed `SELECT … ORDER BY created_at DESC LIMIT 5`.

The agent does not learn that it was throttled. The MCP tool returns success with the new task ID either way. Throttling is purely an operator safety net — a misbehaving agent that knows it's been throttled can just keep retrying; one that doesn't know stops getting auto-dispatched and quietly waits for operator review.

## Schema

No new tables. One enum extension: add `'self_requeue'` to the implicit set of valid `tasks.trigger` values. Today the column is plain `TEXT` with no `CHECK` constraint, so the addition is a code-only change — no migration, no `SCHEMA_SQL` edit.

Documentation lists the canonical set in [CLAUDE.md](../../CLAUDE.md) under Conventions → Triggers. Update to include `self_requeue` alongside the existing `manual`, `commit`, `dependency`, `schedule`, `agent`, `resume`, `reply`, `cascade`.

## Implementation

### Backend

1. **`backend/db_tasks.py` — `enqueue_task` ([db_tasks.py:78](../../backend/db_tasks.py#L78)).** Add the throttle check at the top of the function, before the existing approval / auto-queue logic. When `trigger == 'self_requeue'`, query the last 5 tasks for `job_id`; if all 5 are `self_requeue`, set a local `throttled = True` flag. Use that flag to override the auto-queue branch (force status='pending', skip 'queued' event), and write the `self_requeue_throttled` event after the dispatched event. The coalescing block is reached normally — `self_requeue` falls into the "never coalesce" branch (the current `coalesce_global` and `coalesce_same_type` flags both come up false), so no change there.

2. **`backend/mcp_server.py`.** Add `tool_requeue_self` ([near line 241](../../backend/mcp_server.py#L241), parallel to `tool_dispatch_task`). It posts to a new HTTP route `/api/tasks/self-requeue` with `{"job_id": JOB_ID, "message": ...}`. Register the tool in the `TOOLS` list and the `TOOL_HANDLERS` dispatch dict only when the job's `allow_self_requeue` property is true (the existing module-level config gate is the right hook — same conditional pattern as other gated tools).

3. **`backend/queue_routes.py`.** Add `POST /api/tasks/self-requeue` parallel to the agent-dispatch route. Body: `{"job_id": int, "message": str}`. Validates that `job_id` matches the calling agent's job (defense in depth — the MCP tool already enforces this by passing its own JOB_ID env var) and calls `db.enqueue_task(job_id, "self_requeue", context=message)`. Returns `{"task_id": ...}`.

4. **`backend/db_jobs.py`.** Register the new property in the EAV property def system: `allow_self_requeue` (boolean, default `false`).

### Frontend

5. **`frontend/src/components/Tasks.jsx`** (job config view). Add a checkbox for `allow_self_requeue` in the job properties panel. Same component pattern as the existing `coalesce_tasks` / `require_approval` / `auto_dispatch` toggles.

6. **`frontend/src/util.js`.** Add `self_requeue` to the trigger label map and a trigger icon.

7. **`frontend/src/components/Queue.jsx` and `Tasks.jsx`.** No structural change — these already render arbitrary `trigger` strings via the util label map. The new kind shows up automatically with its label and icon. Throttle is just one more entry in the `task_events` timeline that the detail drawer already renders.

### Documentation

8. **CLAUDE.md.** Update the Triggers list under Conventions to include `self_requeue`. Brief note on the throttle (5 consecutive → forced pending) under the same section.

## Decided

- **Tool name:** `requeue_self` (separate from `dispatch_task`, not a self-targeting variant of it). Clearer agent-facing semantics, distinct kanban surface.
- **Trigger kind:** new value `self_requeue`. Not reusing `agent` so the operator can distinguish "different agent dispatched me" from "I asked to come back."
- **Approval:** honors `require_approval`. Self-requeue is not privileged.
- **Coalescing:** never auto-coalesces. Operator can manually coalesce by hand.
- **Throttle:** 5 consecutive `self_requeue` tasks on the same job → next one lands in `pending`. Reset = any non-self trigger landing on the job.
- **Operator surface:** `task_events` row only. The pending-column placement is the operator signal.
- **Agent does not learn it was throttled.** The MCP tool returns success with the new task ID in all cases.
- **Job-property gate:** `allow_self_requeue` boolean, default false. Mirrors the existing pattern where `dispatch_task` is gated by non-empty `allowed_dispatch_targets`. Two independent gates: "can invoke self" (this property) and "can invoke others" (existing `allowed_dispatch_targets`).

## Open Questions

- **Tool description copy.** The proposed text above is a starting point. Worth revisiting once the feature is in front of an operator who can iterate on whether agents reach for it appropriately.
- **Throttle threshold.** N=5 is a guess. Real value depends on how often legitimate breakpoint-style requeue chains happen. If 3 is too tight or 5 too loose, this is a one-line change in `enqueue_task`.

## Sequencing

One PR. The whole change is small (~150 LOC backend, ~30 LOC frontend, ~10 LOC docs) and self-contained. No schema migration, no behavior change to existing trigger kinds, no operator-visible UI restructure.

The triggers-and-dispatches Stage 1 rename ([proposals/triggers-and-dispatches.md](triggers-and-dispatches.md)) is in flight separately. Whichever lands first, the other inherits the rename pass naturally — the work here doesn't structurally depend on either ordering.
