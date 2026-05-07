# Proposal: Triggers and Dispatches

## Status

Draft.

## Summary

Two changes, additive to the existing `tasks` + `task_executions` shape:

1. **Rename throughout.** `tasks` → `triggers` (they're context packets — that's all they ever were). `task_executions` → `dispatches`. The terminology already in the codebase (`trigger`, `trigger_detail`, `task_branch`, `task_executions`) becomes self-consistent: a trigger is a context packet aimed at a job; a dispatch is a single agent run.

2. **Promote dispatches to first-class.** Today `task_executions` is a sibling table keyed by `task_id` — it has no `id` of its own and isn't directly addressable. Give it `id INTEGER PRIMARY KEY AUTOINCREMENT`. Hang normalized child tables off it: events (lifecycle), outputs (LLM messages / tool calls), and `dispatch_triggers` (which triggers were consumed to build this dispatch's context). The dispatch row becomes immutable on terminal — outcome columns never overwrite, and child rows are append-only.

Reply / resume / retry stop using *inverted coalescing*. Instead the new trigger carries a fully-rendered standalone context built transiently from the prior dispatch (start_commit, result_commit, error, key outputs). No re-rooting, no FK back to the prior dispatch in the lifecycle path. (A non-load-bearing `references_dispatch_id` for UI navigation is an open question — see below.)

**No user-visible behavior change.** Operators see the same kanban, the same detail drawer, the same governor. This is a clarification of internal vocabulary and a strengthening of the entity boundary that already exists.

## Goals

- **Make "what actually ran" addressable.** `dispatches` get their own primary key. Cost, success-rate, governor analytics queries stop joining through `tasks WHERE coalesced_id IS NULL` and just talk about dispatches directly.
- **Make triggers feel like the context packets they are.** Strip outcome semantics off the trigger entirely. A trigger's lifecycle is `pending → queued → consumed`, end of story. Anything about what happened during execution lives on its dispatch.
- **Replace inverted coalescing for reply/resume with transient context.** The current pattern (new task becomes root, original becomes subordinate to "unlock" the terminal subtree) is a clever workaround — but it muddies the read path (subordinates' outcome columns COALESCE through the view) and confuses operators looking at the queue. Render the context once at trigger-creation time, store it as text on the new trigger, move on.
- **Keep the read paths honest.** "Per-trigger as the agent saw it" and "per-dispatch / per-cost" remain the two semantic profiles, but they map cleanly to two distinct entities instead of one entity in two different filterings.

## Non-goals (scope guard)

If implementation pulls any of these in, treat it as a red flag.

- **The worker concurrency model.** One dispatch at a time, lock-protected, same as today. Future parallel dispatch is *enabled* by this proposal (dispatches are independently addressable) but is a separate proposal.
- **The git integration / worktree lifecycle.** A dispatch still owns one worktree on one task branch. The integration-as-completion-gate, operator-wins conflict policy, and stash safety net are unchanged. Worktree-related columns (`worktree_path`, `task_branch`, `orphan_stash_ref`) move from `task_executions` to `dispatches` by the rename — that's it.
- **The MCP tool surface.** No new internal tools. The agent's view of the world is unchanged. (Internal MCP env vars that reference `task_id` may need renaming to `trigger_id` or `dispatch_id`; that's mechanical.)
- **The frontend UX.** Same views, same drawers, same kanban. Backend response shapes adjust; React components mostly just rename props.
- **The MCP / external tool catalog.**
- **`task_events` → some new event sourcing system.** Just a split (or polymorphic relabel) — not a redesign.
- **The Governor's logic, prompt assembly, or trigger schedule.**

## Conceptual Model

### Trigger

A **context packet** aimed at a job. Has identity, queue placement, and a lifecycle through dispatch consumption. Carries no outcome data, ever.

Fields:
- `id` (PK)
- `job_id` (FK)
- `kind` — what currently lives in `tasks.trigger`: `manual`, `commit`, `schedule`, `cascade`, `agent`, `resume`, `reply`. (Renaming the column resolves the "trigger means two things" collision noted in pre-draft discussion.)
- `kind_detail` — what currently lives in `tasks.trigger_detail`: commit hash, schedule expr, upstream task ID.
- `context` — pre-formatted text. Identical to `tasks.context`.
- `status` — `pending | queued | consumed | cancelled | rejected`. (Replaces `tasks.status`'s active/completed/failed/etc. — those move to dispatches.)
- `sort_order`, `approval` — queue placement / gating, unchanged from `tasks`.
- `created_at`
- `coalesced_id` (nullable FK to triggers) — see Coalescing below.
- `references_dispatch_id` (nullable FK to dispatches) — set on resume/reply triggers for UI navigation only; not load-bearing for execution.

**Immutability boundary:** triggers are mutable while `pending` (operator can edit context, transfer between jobs, split coalesce groups). Once `queued`, only the worker touches them, transitioning to `consumed` at dispatch creation.

### Dispatch

A **single agent run**. Has its own primary key, captures everything about that one execution, immutable on terminal.

Fields (same content as today's `task_executions`, plus `id` and `job_id`):
- `id` (PK, autoincrement)
- `job_id` (FK) — denormalized from the consumed triggers for fast filtering
- `status` — `created | active | completed | exhausted | failed | timed_out | cancelled | interrupted`
- `session_id`, `resume_session_id`
- `start_commit`, `result_commit`
- `stop_reason`, `num_turns`, `cost_usd`
- `started_at`, `completed_at`, `error`
- `worktree_path`, `task_branch`, `orphan_stash_ref`

**Immutability:** all outcome columns are write-once during `active → terminal` transition. Child tables are append-only.

### Normalized children

Three child tables, all keyed by `dispatch_id`:

- **`dispatch_events`** — lifecycle events for the dispatch. `(id, dispatch_id, event, detail, created_at)`. Replaces the active-and-after portion of today's `task_events`.
- **`dispatch_outputs`** — normalized LLM thread output. One row per surfaced item (assistant message, tool use, tool result, thinking block, result_meta). Replaces the bespoke `chat_messages` + `chat_events` shape (which is currently keyed by `chat_session.task_id`). Schema TBD in implementation but at minimum `(id, dispatch_id, ordinal, type, payload_json, created_at)`.
- **`dispatch_triggers`** — many-to-many join. `(dispatch_id, trigger_id, position)` where `position` is the order the triggers' contexts were concatenated into the dispatch's prompt.

(I'll use "normalized child tables" rather than "EAV" — the children have known shapes per row. EAV proper is open-attribute storage, which we don't want here. If the framing matters, push back.)

### Lifecycle

Two distinct lifecycles, two distinct event streams:

```
Trigger:   pending → queued → consumed
                  → cancelled / rejected (early terminals)

Dispatch:  created → active → completed
                          → exhausted / failed / timed_out
                          → cancelled / interrupted
```

The transition from "trigger queued" to "trigger consumed" *is* the same atomic moment as "dispatch created." The worker's crystallization step (see below) writes both transitions in the same transaction.

### Crystallization

When the worker pulls a queued trigger today, it activates that one task and runs it. In the new model:

1. Worker pulls the highest-priority `queued` trigger.
2. Worker resolves the consumption set — the trigger plus any siblings in its coalesce group (see Coalescing below).
3. Atomically:
   - Create a `dispatches` row, status `created`.
   - Insert one `dispatch_triggers` row per consumed trigger, with `position` reflecting prompt order (the worker's pulled-trigger first, then siblings in their coalesce-order).
   - Transition each consumed trigger to `consumed`.
4. Worker proceeds with worktree creation, prompt assembly (concatenating consumed triggers' contexts via the join), CLI invocation. Dispatch transitions through `active → terminal` as today.

Late-arriving triggers cannot join an in-flight dispatch — once crystallization happens, the dispatch's trigger set is fixed.

### Retry / Reply / Resume

Today's pattern (inverted coalescing) goes away. New pattern, **uniform across reply, resume, retry, agent-dispatched, and any future "follow-up" trigger types**:

1. Operator (or another agent) acts on a terminal dispatch.
2. The route handler — which already lives in the call site for that action — builds a standalone context string transiently from the prior dispatch: start_commit, result_commit, error if any, the last assistant message, plus whatever the action-specific input is (operator's reply text, resume marker, etc.). The context is fully self-contained: an agent reading it doesn't need to look anything up.
3. A **new trigger** is created with this rendered context, `kind=<reply|resume|retry|agent>`, and `references_dispatch_id=<prior dispatch id>` (for UI navigation; non-load-bearing).
4. The trigger goes into the queue normally. When crystallized, it consumes itself into a fresh dispatch with no execution-level link to the prior dispatch.

This is the orthogonality dividend: every trigger type reduces to "a context packet aimed at a job." The reply path, the resume path, the cascade path, the agent-dispatch path — they each render their own context their own way, but downstream of trigger creation everything is identical. There's no special "is this a follow-up?" branch in the worker, no inverted coalescing for some kinds and not others, no `tasks_resolved`-style read-time fixup.

Side effects:
- The `tasks_resolved` view goes away. There's nothing to resolve — every trigger has at most its own dispatch (via `dispatch_triggers`), and outcome data lives on dispatches directly.
- The "lock-at-terminal" emergent property of pending-only coalescing (see [storage.md](architecture/storage.md)) is replaced by dispatch immutability. Reply doesn't unlock anything because nothing was locked — the prior dispatch stays terminal forever, and the reply is just a new trigger.
- For session resumption: `resume_session_id` lives on the new dispatch. The transient-context renderer for `kind=resume` includes the prior dispatch's `session_id` in the context so the worker can wire it up at execution time.

### Coalescing in the new model

Coalescing semantics carry over from today **unchanged**. The new model is a clean rename of the existing FK shape:

- `triggers.coalesced_id` (FK to triggers, depth-1) replaces `tasks.coalesced_id`.
- Existing SQLite triggers (`tasks_depth1_insert`, `tasks_depth1_update_target`, `tasks_depth1_update_self`) carry over verbatim — just retargeted at the renamed table.
- Operator-driven grouping (drag-drop merge in the kanban) works identically.
- Auto-coalescing at enqueue (the `coalesce_tasks` job property + scheduled-trigger always-coalesce rule) works identically.
- Pre-execution mutability is unchanged: while triggers are `pending`, operators can split, merge, or transfer groups; once `queued`, the worker is the only writer.

The only thing that changes is what the worker does with the group at crystallization time:

1. Worker pulls the highest-priority `queued` trigger that is a coalesce root (no `coalesced_id`).
2. Worker reads "this root + all triggers whose `coalesced_id` points at this root" — that's the consumption set, exactly as today.
3. Atomically: create the dispatch, write one `dispatch_triggers` row per consumed trigger (root at `position=0`, subordinates by their existing order), transition all consumed triggers to `consumed`.

The dispatch_triggers join becomes the persistent record of "which triggers fed this dispatch." The trigger-side `coalesced_id` FK is functionally a queue-time grouping that becomes redundant once consumption happens (it points at a row that's now `consumed` and locked) but stays in place for historical reference.

**The kanban contract is preserved:** what the operator sees in the queue is exactly what the worker will execute. No surprise coalesces, no consume-time grouping logic.

Reply/resume's removal of inverted coalescing (see [Retry / Reply / Resume](#retry--reply--resume)) is a separate mechanism — not a change to operator-driven coalesce.

## Schema

```sql
-- Triggers (the rename of `tasks` minus outcome columns)
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    kind_detail TEXT,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'queued', 'consumed', 'cancelled', 'rejected')),
    sort_order INTEGER,
    approval TEXT,
    coalesced_id INTEGER REFERENCES triggers(id) ON DELETE CASCADE,
    references_dispatch_id INTEGER REFERENCES dispatches(id),
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_triggers_job_status ON triggers(job_id, status);
CREATE INDEX IF NOT EXISTS idx_triggers_coalesced ON triggers(coalesced_id);
-- Depth-1 enforcement triggers carry over unchanged from tasks_depth1_*

-- Trigger lifecycle event log (pending → queued → consumed | cancelled | rejected)
CREATE TABLE IF NOT EXISTS trigger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_id INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    detail TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

-- Dispatches (the rename + promotion of task_executions)
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'created'
        CHECK(status IN ('created', 'active', 'completed', 'exhausted',
                          'failed', 'timed_out', 'cancelled', 'interrupted')),
    session_id TEXT,
    resume_session_id TEXT,
    start_commit TEXT,
    result_commit TEXT,
    stop_reason TEXT,
    num_turns INTEGER,
    cost_usd REAL,
    started_at DATETIME,
    completed_at DATETIME,
    error TEXT,
    worktree_path TEXT,
    task_branch TEXT,
    orphan_stash_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_dispatches_job_status ON dispatches(job_id, status);
CREATE INDEX IF NOT EXISTS idx_dispatches_completed ON dispatches(completed_at);

-- Dispatch lifecycle event log (created → active → terminal)
CREATE TABLE IF NOT EXISTS dispatch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    detail TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

-- Normalized LLM output (replaces chat_messages + chat_events keyed via chat_sessions)
CREATE TABLE IF NOT EXISTS dispatch_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    type TEXT NOT NULL,         -- assistant_text | thinking | tool_use | tool_result | result_meta
    payload_json TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dispatch_outputs_dispatch ON dispatch_outputs(dispatch_id, ordinal);

-- Many-to-many: which triggers fed context into this dispatch
CREATE TABLE IF NOT EXISTS dispatch_triggers (
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
    trigger_id INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (dispatch_id, trigger_id)
);

CREATE INDEX IF NOT EXISTS idx_dispatch_triggers_trigger ON dispatch_triggers(trigger_id);
```

**Removed:** `tasks`, `task_executions`, `task_events`, `chat_sessions`, `chat_messages`, `chat_events`, `tasks_resolved` view, `tasks_depth1_*` triggers (re-created on `triggers` table).

## Routes / API impact

External API names get a deliberate decision:

- **Recommended default:** rename internal but keep `/api/tasks/*` URLs for stability. Frontend already uses these strings; renaming would touch every component without operator benefit. The URL `/api/tasks/{id}` continues to refer to a trigger; new `/api/dispatches/{id}` URLs are added for dispatch-addressable operations.
- **Alternative:** rename URLs too. Cleaner long-term, more frontend churn.

Either way, the response *shapes* change cleanly:

- `/api/tasks/{id}` returns the trigger. Its `dispatch_id` field (computed from `dispatch_triggers`, may be null if not yet consumed) lets the client navigate to the dispatch.
- `/api/dispatches/{id}` (new) returns the dispatch with its events and trigger membership.
- `/api/dispatches/{id}/output` (new) returns ordered `dispatch_outputs`.
- `/api/dispatches/{id}/diff` (new) — what's currently at `/api/tasks/{id}/diff`, but addressed at the dispatch since that's where start/result_commit live.
- `/api/tasks/{id}/output`, `/diff`, `/outcome` redirect / proxy to the dispatch endpoints based on the trigger's consumed dispatch.

## Backend module changes

- **`db_core`**: SCHEMA_SQL updated. `tasks_resolved` view removed.
- **`db_tasks`** → renamed `db_triggers`. Same surface (`transition_*`, `coalesce_under`, `merge_*`, `split_*`, `transfer_*`) but operating on triggers; outcome-routing logic (`upsert_execution` etc.) moves to `db_dispatches`.
- **`db_dispatches`** (new): dispatch CRUD, event log, output insertion, dispatch_triggers management, the crystallize-from-trigger entry point.
- **`db_chat`** → folded into `db_dispatches`. The chat_session abstraction goes away — the dispatch *is* the session.
- **`worker.py`**: crystallization step at trigger pickup. `_process_task` becomes `_process_trigger` → opens a dispatch → runs to terminal.
- **`dispatch.py`**: `build_dispatch_system_prompt` and `build_user_prompt` now read `dispatch_triggers` to assemble the context (concatenate by `position`).
- **`pubsub.py`**: subject-keyed channels — today keyed by `task_id`, become keyed by `dispatch_id` for output streaming. Trigger updates broadcast on the global queue channel.
- **`governor.py`**: `get_recent_tasks_for_governor` becomes `get_recent_dispatches_for_governor`. Reads dispatches directly without joining `tasks`.
- **`db_dashboard`**: `dashboard_health`, `dashboard_timeline`, `dashboard_job_impact` — all currently filter `tasks WHERE coalesced_id IS NULL` to avoid double-counting. They become straightforward `SELECT FROM dispatches`.

## Frontend impact

Despite "no UX change," some component-level work:

- **Queue.jsx**: backend returns triggers in the queue rows. The card still shows job, status, trigger metadata. When `dispatch_id` is non-null (i.e., consumed), display the dispatch's outcome metrics (cost, turns, status) — same fields as today, just sourced through the join.
- **Tasks.jsx, drawer**: similar — trigger identity at the top, dispatch outcome below.
- **Dashboard.jsx**: `dashboard_job_impact`'s execution-side numbers come from dispatches directly. Shape is identical.
- **Governor.jsx**: governor findings reference dispatches now, not tasks. If we keep `/api/tasks/*` URLs, the change is purely internal.

## Migration

Per the project's no-migration-system policy:

1. Edit `SCHEMA_SQL` in `db_core` — drop `tasks`/`task_executions`/`task_events`/`chat_*`, add new tables.
2. Recreate the dev DB.
3. One-shot `migrate_db.py` script for existing project DBs:
   - For each `tasks` row → insert into `triggers`. Keep `id`. Map `trigger`/`trigger_detail` → `kind`/`kind_detail`. Drop outcome-shaped columns. Map status `active`/`completed`/etc. on a non-coalesced row to `consumed` (the trigger was consumed by some dispatch).
   - For each task with a `task_executions` row → insert into `dispatches` with a fresh `id`. Insert `dispatch_triggers(dispatch_id, trigger_id=task_id, position=0)`.
   - For each coalesced subordinate (no `task_executions` of its own) → insert `dispatch_triggers(dispatch_id=<root's new dispatch id>, trigger_id=<subordinate's task id>, position=N)`.
   - For each `task_events` row → split between `trigger_events` (pre-active) and `dispatch_events` (active+).
   - For each `chat_session` + its `chat_messages`/`chat_events` → flatten into `dispatch_outputs` rows on the corresponding dispatch.

The migration is offline, idempotent, and verifiable (row-count parity per task → trigger; per execution → dispatch). It is not part of the running backend.

## Decided

- **Status taxonomy** — split per entity. Triggers: `pending | queued | consumed | cancelled | rejected`. Dispatches: `created | active | completed | exhausted | failed | timed_out | cancelled | interrupted`. The `cancelled` overlap is intentional and meaningful — trigger-cancelled = nuked before consumption; dispatch-cancelled = killed mid-run. `completed` is dispatch-only. `rejected` is trigger-only.
- **`references_dispatch_id` on follow-up triggers** — yes, nullable. UI navigation only, not load-bearing for execution. Reply/resume/retry/agent-dispatched triggers all carry it pointing at the prior dispatch they were spawned from.
- **External URL rename** — `/api/tasks/*` → `/api/triggers/*`; new `/api/dispatches/*` for dispatch-addressable endpoints. Mechanical change; pays off in long-term consistency.
- **`chat_*` consolidation** — fold `chat_sessions` + `chat_messages` + `chat_events` into `dispatch_outputs`. The `chat_session.cli_session_id` indirection moves to a `cli_session_id` column on `dispatches`. Each NDJSON event from the CLI becomes one `dispatch_outputs` row (`type`, `payload_json`). Eliminates the dual-write bug class (events vs. messages out of sync) and deletes `db_chat.py` entirely.
- **Resolved kanban query strategy** — Strategy C: dispatch-primary for the resolved column (`SELECT FROM dispatches WHERE status IN (terminals)`), with a batch trigger fetch via `dispatch_triggers` to populate the consumed-trigger list per card. Pending/queued columns stay trigger-primary (`SELECT FROM triggers WHERE status IN ('pending','queued')`). Card identity for resolved becomes the dispatch with consumed triggers nested inside; for pending/queued it's the trigger. Net: drops the `WHERE coalesced_id IS NULL` filter, drops the `tasks_resolved` view, no COALESCE-through-view dance.
- **Framing — normalized child tables, not EAV.** `dispatch_events`, `dispatch_outputs`, `dispatch_triggers` each have known per-row shapes. Confirmed.
- **Event log shape — two tables, dispatch owns the consumption record.** `trigger_events` records the pre-consumption queue lifecycle: `pending → queued → cancelled / rejected`, end of story. There is no `consumed` event on the trigger side. Crystallization is recorded only on the dispatch side: `dispatch_events` opens with a `created` row whose detail carries the consumed `trigger_ids`. Trigger consumption is observable via `triggers.status = 'consumed'` and `dispatch_triggers` membership; the audit narrative lives in `dispatch_events`. No cross-referential duplication, both event logs append-only, full historical detail preserved.
- **Coalescing semantics — unchanged from today.** `triggers.coalesced_id` FK + depth-1 enforcement triggers + pre-execution mutability all carry over verbatim. Operator-driven drag-drop merge and auto-coalesce-at-enqueue both work identically. Worker reads the group at crystallization to write `dispatch_triggers`. Kanban contract preserved: what operator sees == what worker executes.
- **Detail Drawer rendering — dispatch-centric.** The drawer's timeline shows `dispatch_events` only (created → active → terminal). Consumed triggers render separately as a static list — kind, kind_detail, context — with no event timeline of their own. Operator gets "here's what fed in" plus "here's what happened during execution" without interleaving the two. Trigger lifecycle events (queued, cancelled, rejected) remain queryable for incident debugging but are not surfaced in the drawer.

## Open Questions

None — design is fully settled. Implementation will surface concrete decisions (column types, index choices, exact migration script edge cases), but the model, schema shape, lifecycles, semantics, and UX contract are all pinned.

## Sequencing

Sized for a multi-day chunk on the same order as the original task-workspace-isolation Phase 2. Each step leaves the system functional at every commit (modulo the schema-recreation step, which is gated on operator permission).

1. **Rename pass — terminology only.** Across the program: rename `task` → `trigger` and `task_execution` → `dispatch` in identifier names where they refer to the entities (variables, function names, comment text, MCP env vars, log strings). Schema unchanged. URL surface unchanged. This is the boring textual rename — a single PR that touches a lot but changes no behavior. Validates the framing and reveals naming collisions.

2. **Schema split.** New tables (`triggers`, `dispatches`, `dispatch_events`, `dispatch_outputs`, `dispatch_triggers`, `trigger_events`). Drop `tasks`, `task_executions`, `task_events`, `chat_*`, `tasks_resolved`. Migration script in `migrate_db.py` for existing project DBs. Dev DB recreated. All read paths switch to the new tables.

3. **Crystallization rewire.** Worker's `_process_task` becomes `_process_trigger` → crystallize → dispatch. Auto-coalesce moves to consume time (per Open Question #1). Inverted coalescing for reply/resume removed; replaced by transient-context rendering at trigger-creation.

4. **Output normalization.** `chat_*` consolidation into `dispatch_outputs` (per Open Question #4). The CLI bridge's NDJSON event handler writes directly to `dispatch_outputs` instead of going through `chat_events`/`chat_messages`.

5. **Dispatch-addressable URLs.** New `/api/dispatches/*` endpoints; existing `/api/tasks/*` routes either stay as compatibility shims (per Open Question #3) or get renamed.

6. **Documentation pass.** Architecture docs (storage, task-lifecycle, streaming-and-sessions, dispatch-engine, governor) all need terminology and diagram updates. CLAUDE.md gets the new model.

Steps 1 and 2 ship as separate PRs (rename, then schema). Steps 3–4 can interleave or be one combined PR. Step 5 is a small cleanup. Step 6 lands with whatever PR completes the model — probably the schema split.
