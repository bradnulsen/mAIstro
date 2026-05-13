# Proposal: Agent Continuation (self-requeue + auto-continue)

## Status

Draft. Folds two related continuation paths into one design so they share the enqueue chokepoint, throttle, and event surface.

## Summary

Two ways an agent's work can outlive a single dispatch:

- **`self_requeue` — voluntary.** The agent calls a `requeue_self` MCP tool. Use cases: **clarification** (agent is blocked, needs operator input before proceeding) and **natural breakpoint** (agent finished a discrete chunk; the next chunk is its own dispatch — better isolation, fresh context window, fresh worktree).
- **`auto_continue` — involuntary.** The worker detects an `exhausted` or `timed_out` terminal and (if the job opts in) enqueues a continuation on the same job. Use case: the agent was killed mid-thought; an automatic fresh-conversation dispatch can pick up from the prior worktree.

Both produce a new task on the same job. Both reuse `enqueue_trigger` so they pick up approval gating, auto-queue handling, and coalescing rules with no special-casing. Both are bounded by a throttle: a runaway agent that self-requeues, or a misconfigured job that exhausts in a loop, lands its next continuation in `pending` so the operator notices.

They differ on five axes:

|  | `self_requeue` | `auto_continue` |
|---|---|---|
| Initiator | agent (MCP tool) | worker (terminal handler) |
| Worktree | fresh from main HEAD | inherits prior worktree+branch |
| CLI conversation | fresh | fresh (no `--resume`) |
| Throttle | 5 consecutive → forced pending | 2 consecutive → forced pending |
| Gate | `allow_self_requeue` property | `auto_continue` property |
| Fires after | agent's explicit call | `exhausted` or `timed_out` only |

Separate trigger kinds (not `agent` re-used) so the kanban / detail drawer can label both clearly and each kind tracks its own throttle counter. Separate job properties because the blast radii are different — an operator might trust voluntary self-requeue but not auto-continue (or vice versa).

## Goals

- Let agents declare "I'm done for now, queue me up again" without an external trigger.
- Let the platform recover from interrupted dispatches automatically when the job allows it.
- Preserve the operator-driven kanban contract: every continuation produces a visible task that the operator can edit, transfer, coalesce, or cancel before it runs.
- Bound the autonomy: misbehaving agents and misconfigured jobs can't drain tokens forever.
- Reuse the existing `enqueue_trigger` chokepoint so each new trigger kind picks up approval gating, auto-queue handling, and coalescing rules with no special-casing.

## Non-goals (scope guard)

If the implementation pulls any of these in, treat it as a red flag.

- **Cross-job dispatch.** `dispatch_task` keeps its current shape and gating (`allowed_dispatch_targets`, self-dispatch prohibited). This proposal adds *separate* trigger kinds; they operate independently.
- **Worker concurrency or mid-dispatch handoff.** Both continuation kinds produce a normal task that the worker picks up later, in a separate dispatch. No mid-run handoff, no shared state.
- **Reshaping resume.** Resume keeps the same CLI session (`--resume <session_id>`); auto-continue is a *fresh* conversation that happens to reuse the worktree. The two are deliberately distinct primitives.
- **Operator UX surface beyond minimum.** Throttled tasks land in `pending` and carry a `task_events` row; that's the operator surface. No new kanban indicators, no Governor finding shape, no toast.
- **Loops across jobs.** A→B→A oscillation via `dispatch_task` is a separate problem and out of scope.

## Conceptual Model

### Self-requeue

A new MCP tool `requeue_self`, gated by `allow_self_requeue` (boolean, default `false`). When the property is false the tool is not registered for that job — the agent doesn't see it at all. Matches the existing pattern for the learning-write tools.

Tool description (proposed):

> Enqueue a new task on this same job, picked up after the current dispatch terminates. Use when (1) you are blocked and need operator input that would change the task's framing, or (2) you completed a discrete chunk and the next chunk should run as its own dispatch with a fresh context window. The `message` describes what state you reached and what should happen next — it becomes the new task's context. Self-requeue is rate-limited; do not loop on it.

Input schema: a single required `message` string.

**Worktree behavior:** fresh worktree from main HEAD (standard dispatch path). The agent chose this breakpoint; "fresh dispatch on the current main" is what self-requeue means.

### Auto-continue

A new job property `auto_continue` (boolean, default `false`). When `true`, the worker's terminal handler for `exhausted` and `timed_out` enqueues a continuation on the same job after the lifecycle transition.

**Triggers only on `exhausted` and `timed_out`.** The other non-success terminals (`failed`, `cancelled`, `rejected`, `interrupted`) mean the run was actively wrong or the operator pulled the plug — auto-continuing would be intrusive.

**Worktree behavior:** inherits the prior dispatch's worktree+branch. The prior worktree is *already preserved* on `exhausted`/`timed_out`; inheritance is what the preservation is for. Same mechanism resume uses today, minus the `--resume <session_id>` flag — auto-continue gets a fresh CLI conversation but a workspace that already has the prior commits.

Synthesized continuation context (assembled by `build_trigger_context`):

> **Auto-continue** — prior dispatch (task #<id>) ended in **<exhausted | timed_out>** after <N> turns / <Ns> elapsed.
> Worktree preserved at branch `<job-slug>/task-<old-id>`.
> Pick up where it stopped: review what's already committed (`git log`, `git diff <start_commit>..HEAD`) and continue.

### The trigger kinds

Two new values in the `tasks.trigger` enum: `self_requeue`, `auto_continue`. Distinct from `agent` (cross-job dispatch) and `resume` (operator-pressed CLI-session continuation) so the kanban / detail drawer label them clearly and each kind tracks its own throttle counter.

Both compose with `enqueue_trigger`'s existing rules:

- **Approval:** both honor the job's `require_approval` property. Only `manual` bypasses.
- **Coalescing:** both never auto-coalesce. Same as `manual`, `resume`, `reply`. Operator can still drag-drop coalesce in pending.
- **Auto-queue:** both honor the global `queue_auto_dispatch` setting like every other trigger — *except* when the per-kind throttle fires (see below).

### The throttle

Shared logic in `enqueue_trigger`. When a `self_requeue` or `auto_continue` task is being enqueued for job J:

1. Read the most recent N tasks on J (any status), ordered by `created_at` descending.
   - `self_requeue` → N = 5
   - `auto_continue` → N = 2
2. If all N are the *same trigger kind* as the new one, the throttle fires:
   - Force `status='pending'` regardless of `queue_auto_dispatch`.
   - Skip the `'queued'` lifecycle event.
   - Write an additional `task_events` row: `event='continuation_throttled'`, `detail = '<kind>:<N>'`.

Tighter `N=2` on `auto_continue` because two-in-a-row almost always means the job is misconfigured (turn limit too low, scope too big, timeout too short) — not making progress. Operator should see it before another dispatch fires. `N=5` on `self_requeue` because voluntary requeues are usually load-bearing and chains of small chunks are legitimate.

**Reset rule:** any non-self trigger of either continuation kind on the job (manual, commit, schedule, cascade, agent, dependency, resume, reply, *and* the other continuation kind) breaks the run. The next continuation's window of N starts fresh from that point.

The reset rule is implicit in the "last N tasks all of this kind" check — no separate counter, no separate state. One indexed `SELECT … ORDER BY created_at DESC LIMIT N`.

The agent does not learn that it was throttled. The MCP tool / terminal handler returns success with the new task ID either way. Throttling is purely an operator safety net — a misbehaving agent that knows it's been throttled can just keep retrying; one that doesn't know stops getting auto-dispatched and quietly waits for operator review.

## Schema

No new tables. Two new property defs:

```sql
('allow_self_requeue', 'false', 'boolean'),
('auto_continue', 'false', 'boolean'),
```

Two new implicit values in `tasks.trigger` (no `CHECK` constraint — code-only).

Documentation lists the canonical set in [CLAUDE.md](../../CLAUDE.md) → Conventions → Triggers: update to include `self_requeue` and `auto_continue` alongside `manual`, `commit`, `dependency`, `schedule`, `agent`, `resume`, `reply`, `cascade`.

## Implementation

### Backend

1. **`db_core.py` — `SEED_SQL`.** Add the two property defs.

2. **`db_triggers.py` — `enqueue_trigger`.** Add the throttle check before the existing approval / auto-queue logic. When `trigger in {'self_requeue', 'auto_continue'}`, query the last N tasks for `job_id` of that exact trigger kind; if all N match, set `throttled=True`. Use the flag to override the auto-queue branch (force status='pending', skip 'queued' event), and write a `continuation_throttled` event row after the dispatched event. Both continuation kinds fall into the "never coalesce" branch — no change there.

3. **`dispatch.py` — `build_trigger_context`.** Add `self_requeue` and `auto_continue` branches. Self-requeue's context is the agent's `message`. Auto-continue's context is the synthesized "prior dispatch ended in X, worktree at Y" block.

4. **`worker.py` — terminal handler.** In the `exhausted` and `timed_out` branches, after the transition+cascade calls, check `job["properties"].get("auto_continue")`. When true, call a new helper `enqueue_auto_continue(task_id, job, terminal_state, meta_fields)` that synthesizes the continuation context and calls `enqueue_trigger(... 'auto_continue', trigger_detail=str(task_id), context=...)`. Helper goes in worker module alongside `_enqueue_cascades`.

5. **`worker.py` — worktree inheritance for `auto_continue`.** Mirror the existing resume path (`_process_trigger` lines ~584–616): when `trigger == 'auto_continue'` and `trigger_detail` is set, look up the original by id, reuse its `worktree_path` + `task_branch`. Do *not* pass `--resume <session_id>`. If the original's worktree is gone (operator discarded), the auto-continue task fails loudly the same way resume does.

6. **`mcp_server.py`.** Add `tool_requeue_self` posting to a new HTTP route `/api/triggers/self-requeue`. Add a constant `SELF_REQUEUE_TOOLS = {"requeue_self"}` and an `ALLOW_SELF_REQUEUE` env gate; intersect with `ALLOWED_INTERNAL_TOOLS` the same way `LEARNING_WRITE_TOOLS` is filtered. Register the tool in `TOOLS` and `TOOL_HANDLERS`. Document the new env var in the module docstring.

7. **`mcp_config.py`.** Pass `MAISTRO_ALLOW_SELF_REQUEUE` env var to the internal MCP server based on the job's `allow_self_requeue` property.

8. **`queue_routes.py`.** Add `POST /api/triggers/self-requeue` parallel to `/api/triggers/agent-dispatch`. Body: `{"job_id": int, "message": str}`. Validates that `job_id` matches the calling agent's job (defense in depth) and calls `enqueue_trigger(job_id, "self_requeue", trigger_detail=str(source_task_id), context=...)`. Returns `{"task_id": ...}`.

9. **`db_jobs.py`.** Register both new properties in the EAV property def system: `allow_self_requeue` (boolean default false), `auto_continue` (boolean default false). (`SEED_SQL` does the heavy lift; this ensures the property is recognized in the assembly helpers.)

### Frontend

10. **`util.js`.** Add `self_requeue` and `auto_continue` entries to `TRIGGER_LABELS` and `TRIGGER_ICONS`.

11. **`Tasks.jsx`** (job config view). Add two checkboxes — one each for `allow_self_requeue` (in the dispatch tab near `require_approval` / `coalesce_tasks`) and `auto_continue` (same area). Add corresponding `TIPS` entries.

12. **`Queue.jsx` / `Tasks.jsx`.** No structural change — these already render arbitrary `trigger` strings via the util label map. New kinds show up automatically with their labels and icons. The throttle event lands as one more entry in the `task_events` timeline that the detail drawer already renders.

### Documentation

13. **`CLAUDE.md`.** Update the Triggers list under Conventions: add `self_requeue` and `auto_continue`. Brief note on throttle behavior (5 / 2 consecutive → forced pending) under the same section.

## Decided

- **Two distinct trigger kinds**, not one shared "continuation" kind. The operator-facing semantics differ enough that the kanban should distinguish them and the throttle counters should be independent.
- **Two distinct job properties.** Independent blast radii.
- **`auto_continue` fires on `exhausted` and `timed_out` only.** Not on `failed`, `cancelled`, `rejected`, `interrupted`.
- **`auto_continue` inherits the prior worktree+branch.** Same mechanism resume uses, minus `--resume`. The worktree preservation invariant on non-success terminals exists precisely so continuation can build on it.
- **`auto_continue` does *not* reuse the prior CLI session.** Fresh conversation. The prior conversation already hit its limit; resuming it would just hit the limit again.
- **Throttle thresholds:** `self_requeue` N=5, `auto_continue` N=2. Tighter on involuntary because involuntary repeats are almost always a config smell.
- **Reset rule:** any non-self-continuation trigger landing on the job breaks the consecutive run.
- **Operator surface:** `task_events` row only. The pending-column placement is the operator signal.
- **Agent does not learn it was throttled.** Returns success with the new task ID in all cases.

## Open Questions

- **Tool description copy** for `requeue_self`. Starting point above; worth revisiting once operators iterate on whether agents reach for it appropriately.
- **Throttle thresholds.** N=5 / N=2 are educated guesses. Real values depend on observed legitimate chain lengths. One-line change in `enqueue_trigger` to retune.
- **Should `auto_continue` honor `require_approval`?** The proposal says yes (it's a non-manual trigger so the gate applies). But an operator who enabled `auto_continue` is *signaling* trust — the approval gate may be redundant friction. Leave as honoring `require_approval` for now; revisit if real usage shows the combination is awkward.

## Sequencing

One PR. Total change footprint: ~250 LOC backend, ~50 LOC frontend, ~20 LOC docs. No schema migration, no behavior change to existing trigger kinds, no operator-visible UI restructure.

The triggers-and-dispatches Stage 1 rename ([proposals/triggers-and-dispatches.md](triggers-and-dispatches.md)) is committed; this PR inherits the renamed vocabulary naturally.
