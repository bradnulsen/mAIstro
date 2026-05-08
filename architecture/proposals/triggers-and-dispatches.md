# Proposal: Triggers and Dispatches

## Status

**Stage 1 (rename) — committed, ready to ship as a single PR.**
**Stages 2+ (structural) — deferred. Design preserved below for when we revisit.**

## Summary

The taxonomy already fits the real architecture. The code says `tasks.trigger`, `tasks.trigger_detail`, `task_executions`, `dispatch_task`, `MAISTRO_TASK_ID` — the entities are the right ones; only the names are wrong. A "task" today is a context packet aimed at a job (a **trigger**), and a `task_executions` row is a single agent run (a **dispatch**).

Two-stage refactor:

1. **Stage 1 — rename only.** Vocabulary fix across Python, frontend, URLs, env vars, and docs. No schema change, no logic change, no UX delta. ~25 files, mostly find-and-replace. Captures the conceptual clarity at low risk.

2. **Stages 2+ — structural (deferred).** Promote `dispatches` to a first-class entity with own PK, hang normalized child tables off it, drop the `tasks_resolved` view, and reshape the reply/resume mechanism. Defer until there's a forcing function (parallel dispatch goal, dual-write bug recurrence, dashboard cost queries getting painful). The current model has known sharp edges, but they are *understood* sharp edges, and the kanban works.

A design lens for Stage 2+: **a single trigger can be dispatched multiple times** as we iterate (reply, resume, retry). Today's `task_executions` is 1:1 with `tasks`, but dispatches first-class would naturally support 1:many — a trigger is "the thing we want done," dispatches accumulate as we work it. This reshapes parts of the structural design currently drafted below (notably, the reply path may not need transient-context renderers if a reply just creates a new dispatch on the existing trigger). The current operator-driven coalesced-triggers kanban stays as-is regardless.

## Stage 1: rename only

The committed scope. One PR.

### What changes

- **Backend Python identifiers.** `task` → `trigger` and `task_execution` → `dispatch` everywhere they refer to these entities: function names, parameter names, variables, log strings, comments, docstrings.
- **Module file renames.** [db_tasks.py](../../backend/db_tasks.py) → `db_triggers.py`. [db_chat.py](../../backend/db_chat.py) keeps its name in Stage 1 (Stage 2+ deletes it). The [database.py](../../backend/database.py) re-export shim updates its imports.
- **Frontend identifiers.** API wrapper names in [api.js](../../frontend/src/api.js), prop/state names in [Queue.jsx](../../frontend/src/components/Queue.jsx), [Tasks.jsx](../../frontend/src/components/Tasks.jsx), [Dashboard.jsx](../../frontend/src/components/Dashboard.jsx), [Governor.jsx](../../frontend/src/components/Governor.jsx); util helper names in [util.js](../../frontend/src/util.js).
- **API URL paths.** `/api/tasks/*` → `/api/triggers/*`. Frontend wrappers update in the same PR. SSE streams keep their event-type names (`text`, `thinking`, `tool_use`, etc. — these are wire-format strings, not entity names).
- **MCP env var.** `MAISTRO_TASK_ID` → `MAISTRO_TRIGGER_ID` ([mcp_config.py:58](../../backend/mcp_config.py#L58), [mcp_server.py:15](../../backend/mcp_server.py#L15), [mcp_server.py:45](../../backend/mcp_server.py#L45), [mcp_server.py:257](../../backend/mcp_server.py#L257)).
- **Documentation.** [CLAUDE.md](../../CLAUDE.md), [STRATEGY.md](../../STRATEGY.md), [REMEDIATION.md](../../REMEDIATION.md), and [architecture/*.md](../) updated to use trigger/dispatch vocabulary. The mismatch between Python (triggers/dispatches) and SQL (still `tasks`/`task_executions`) is called out as a tracked debt that closes when Stage 2 lands.

### What does *not* change in Stage 1

- **SQL schema.** Tables stay named `tasks`, `task_executions`, `task_events`, `chat_*`. Columns unchanged. Triggers and views unchanged. SQL string literals inside `db_*.py` modules continue to reference the legacy names. Schema rename is a real migration; it belongs to Stage 2+.
- **Behavior.** No transaction shape changes. No new endpoints. No removed endpoints (URL rename is the only URL change). No worker loop changes. No coalescing changes. Reply/resume keep their current inverted-coalescing mechanism.
- **MCP tool surface.** External MCP tools keep their names. The internal `dispatch_task` tool was already correctly named.
- **`task_executions` 1:1 with `tasks`.** Stage 1 doesn't open the door to "many dispatches per trigger" — that's a Stage 2+ structural change.

### Stage 1 impact

- **~2,000–2,500 LOC churn**, ~25 files. Almost all of it is mechanical find-and-replace. The complete file-by-file footprint lives in [Impact assessment](#impact-assessment) below — Stage 1 sub-table.
- **Risk: low.** No new transactions, no new failure modes. The only non-mechanical work is updating SQL parameter names from `task_id=?` to `trigger_id=?` while leaving the SQL table names alone, which means a brief vocabulary inversion at the storage boundary (`triggers` table read but legacy SQL says `tasks`). Acceptable temporary state.
- **Validates the framing.** A clean Stage 1 surfaces any naming collisions or call sites that resist renaming, de-risking the structural Stage 2+ if/when it happens.

---

## Stages 2+: structural (deferred)

**The remainder of this document is Stage 2+ design.** It captures the structural promotion of dispatches, the schema split, the chat-table consolidation, and the reply/resume reshape. None of it is committed; treat it as the working design for whenever we pick this up. Section headers below would all be prefixed "Stage 2+:" if we were rigorous about it; left unmarked for readability.

Note when revisiting: the **"one trigger, many dispatches over time"** observation (see [Summary](#summary)) likely changes the Schema, the Routes/API impact, and the Reply/Resume sections. The drafted approach uses transient-context renderers and `references_dispatch_id` to preserve a 1:1-ish trigger:dispatch shape. The alternative — let a trigger have multiple `dispatches` rows over its lifetime, with reply/resume just creating a new dispatch on the same trigger — is simpler conceptually and matches how operators think about the kanban. Pick the simpler model when we get there.

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

### Stage 1 (committed)

1. **Rename pass — terminology only.** Across the program: rename `task` → `trigger` and `task_execution` → `dispatch` in identifier names (variables, function names, parameter names, comment text, log strings). Module file renames where the rename makes the file name wrong. URL paths rename `/api/tasks/*` → `/api/triggers/*`. MCP env var renames. Architecture docs and CLAUDE.md updated to match. Schema unchanged. Behavior unchanged. **One PR.**

### Stages 2+ (deferred)

These stages are not scheduled. They are the planned shape *if* we revisit. Order is logical, not committed.

2. **Schema split.** New tables (`triggers`, `dispatches`, `dispatch_events`, `dispatch_outputs`, `dispatch_triggers`, `trigger_events`). Drop `tasks`, `task_executions`, `task_events`, `chat_*`, `tasks_resolved`. One-shot `migrate_db.py` for existing project DBs. Dev DB recreated. Read paths switch to new tables.

3. **Crystallization rewire.** Worker's `_process_task` becomes `_process_trigger` → crystallize → dispatch. The reply/resume reshape happens here — picking between the drafted "transient context renderer + new trigger" approach and the simpler "many dispatches per trigger" model is the key design call at this stage.

4. **Output normalization.** `chat_*` consolidation into `dispatch_outputs`. The CLI bridge's NDJSON event handler writes directly to `dispatch_outputs` instead of going through `chat_events`/`chat_messages`.

5. **Dispatch-addressable URLs.** New `/api/dispatches/*` endpoints for dispatch-keyed operations (output, diff, timeline).

Stages 2 and 3 are the heavy lifts. Stages 4 and 5 are smaller. The whole structural set is roughly the same size as the original task-workspace-isolation Phase 2 — multi-day, multi-PR. Defer until a forcing function arrives.

## Impact assessment

Pre-implementation walkthrough. Numbers verified against the tree at proposal-draft time — re-run the surveys before commit if much time has passed.

### Stage 1 footprint (committed)

- **~2,000–2,500 LOC churn**, ~25 files. Almost entirely mechanical identifier renames.
- **Risk: low.** No new transactions, no new failure modes, no schema migration.

Stage 1 file-by-file:

| File | Stage 1 work |
|---|---|
| [db_tasks.py](../../backend/db_tasks.py) → rename to `db_triggers.py` | All 31 functions get param/identifier renames (`task_id` → `trigger_id`). `task_executions` writes/reads stay as SQL string literals — only the wrapping function names change (`upsert_execution` → `upsert_dispatch`). `get_task_resolved` stays in place; `tasks_resolved` view stays. |
| [db_chat.py](../../backend/db_chat.py) | No file rename. Internal identifier renames where they reference task IDs as parameters. Stage 2+ deletes the file. |
| [db_dashboard.py](../../backend/db_dashboard.py) | Identifier renames in query result handling. SQL strings stay. |
| [db_governor.py](../../backend/db_governor.py) | `get_recent_tasks_for_governor` → `get_recent_dispatches_for_governor`. SQL still reads `tasks_resolved`. |
| [db_core.py](../../backend/db_core.py) | Comment-only updates. Schema strings untouched. |
| [database.py](../../backend/database.py) | Re-export shim — update import for `db_tasks` → `db_triggers`. |
| [worker.py](../../backend/worker.py) | `_process_task` → `_process_trigger`, `get_active_task_id` → `get_active_dispatch_id` (the running concept is the dispatch, even though the SQL row is still in `tasks`). All `task_id` locals/log strings rename. ~134 occurrences. |
| [dispatch.py](../../backend/dispatch.py) | Param renames; logic unchanged. |
| [cli.py](../../backend/cli.py) | Comment + log string renames. Logic unchanged. |
| [pubsub.py](../../backend/pubsub.py) | Channel-key parameter renamed. Underlying dict still keyed by the same int (the task/trigger/dispatch row's PK in Stage 1 — they're the same row). |
| [scheduler.py](../../backend/scheduler.py) | `enqueue_task` → `enqueue_trigger` at the one call site. |
| [queue_routes.py](../../backend/queue_routes.py) | All 31 routes: path rename `/api/tasks/*` → `/api/triggers/*`, param renames `task_id` → `trigger_id`. Handler internals unchanged. The `tag=["queue"]` etc. stays. |
| [git_routes.py](../../backend/git_routes.py) | One `enqueue_task` call site renamed (line 106). |
| [governor.py](../../backend/governor.py), [governor_routes.py](../../backend/governor_routes.py), [feed_routes.py](../../backend/feed_routes.py) | Identifier renames; logic unchanged. |
| [mcp_server.py](../../backend/mcp_server.py), [mcp_config.py](../../backend/mcp_config.py) | `MAISTRO_TASK_ID` → `MAISTRO_TRIGGER_ID` (4 sites total). `dispatch_task` MCP tool name kept (already correct). |
| [governor_mcp.py](../../backend/governor_mcp.py) | Internal identifier renames. |
| [api.js](../../frontend/src/api.js) | All ~28 functions: rename + URL path rewrite. |
| [Queue.jsx](../../frontend/src/components/Queue.jsx) | State/prop names rename throughout (1326 LOC, mostly mechanical). Display logic unchanged. |
| [Tasks.jsx](../../frontend/src/components/Tasks.jsx) | Same — mechanical rename of `task` → `trigger` and outcome-related identifiers → `dispatch`. |
| [Dashboard.jsx](../../frontend/src/components/Dashboard.jsx), [Governor.jsx](../../frontend/src/components/Governor.jsx), [App.jsx](../../frontend/src/App.jsx) | Spot renames. |
| [util.js](../../frontend/src/util.js) | `getTaskStatus` etc. — rename. |
| [App.css](../../frontend/src/App.css) | No semantic class names tied to entities. ~0 LOC. |
| [CLAUDE.md](../../CLAUDE.md) | Vocabulary update. Note the deliberate Python/SQL split as tracked debt. ~150–200 LOC delta. |
| [STRATEGY.md](../../STRATEGY.md), [REMEDIATION.md](../../REMEDIATION.md) | Spot renames. |
| [architecture/*.md](../) | Vocabulary update across all 13 docs. The doc rename `task-lifecycle.md` → `trigger-and-dispatch-lifecycle.md` is a Stage 2+ concern (lifecycles don't actually split until Stage 2). For Stage 1, content updates only. ~300 LOC delta total. |

What's *not* in Stage 1:
- No `db_dispatches.py` module yet.
- No new tables, indexes, or views.
- No new SQL queries.
- No new HTTP endpoints (only renamed paths on existing endpoints).
- No worker transaction shape changes.
- No reply/resume mechanism changes.
- No `tasks_resolved` view removal.

### Stage 2+ footprint (deferred)

The structural work, when it happens. Headline:

- **~25–30 files modified**, **~3,500–4,500 LOC of churn** across backend, frontend, docs.
- **~70%** mechanical, **~25%** read-path repointing, **~5%** genuinely new logic.

### Stage 2+ concerns to keep eyes on

These are the parts that aren't pure rename and have non-trivial failure modes — relevant when/if we pick Stage 2+ up:

1. **Crystallization transaction (`worker.py`).** Today the worker calls `transition_task(task_id, "active")` and runs. New flow: pull queued trigger → resolve coalesce group via `coalesced_id` → atomically `(INSERT dispatches, INSERT dispatch_triggers ×N, UPDATE triggers SET status='consumed')` → then run. New transaction with new partial-failure modes (e.g., dispatch row created but trigger transitions roll back). Worker startup sweep (`_sweep_stale`) needs updating to recognize orphan dispatches, not orphan tasks.

2. **`chat_*` → `dispatch_outputs` consolidation.** The most invasive part of the refactor. Today the CLI bridge in `cli.py` produces NDJSON events that `worker.py` persists via `db.add_chat_event()` (raw audit) and the message-builder via `db.add_chat_message()` (assembled assistant turns). Both paths collapse into `dispatch_outputs`. The dual-write bug class disappears (per the Decided section), but the rewrite touches `cli.py:_translate_event`, the worker's event loop, and every SSE consumer. Risk: re-thinking what counts as a "row" in the output table — is each NDJSON event a row, or is each assembled turn a row? Decided text says "each NDJSON event = one row" — implementation must hold to that or the schema mismatches reality.

3. **Migration script — write-once, no second chances.** ~300 LOC reading old shape, writing new shape. Three failure modes worth designing against up front: (a) row-count drift between source and target tables (assert parity); (b) ordinal collisions in `dispatch_outputs` if `chat_messages` and `chat_events` are flattened separately and assigned overlapping ordinals; (c) `coalesced_id` chains that aren't depth-1 in older DBs (predates the trigger enforcement). Recommend dry-run mode + integrity assertions before destructive replace.

4. **`tasks_resolved` view removal.** Currently referenced from 14 files including `STRATEGY.md`, `REMEDIATION.md`, multiple architecture docs, and the Governor query. The view encapsulates the COALESCE-through-coalesced_id pattern in one place; removing it means N call sites each have to handle "is this trigger consumed yet?" themselves. Per the Decided section the answer is "resolved kanban column queries dispatches directly, pending/queued queries triggers directly" — so most call sites simplify rather than complicate, but each one needs eyes.

5. **`cascade_completion` removal (db_tasks.py:910).** Today this is the single helper every worker terminal-completion path calls to mark subordinates terminal alongside their root. In the new model it has no work to do: subordinates are already `consumed` from crystallization time, and dispatch terminal status sits on the dispatch row. Removing the helper requires verifying every call site is OK with the no-op (or that the call goes away naturally because subordinates aren't a runtime concept anymore).

### Stage 2+ file-by-file footprint

#### Backend — schema + DB layer

| File | Lines | Nature of change | Size |
|---|---|---|---|
| [db_core.py](../../backend/db_core.py) | 116–348 (SCHEMA_SQL); also `close_db` | Replace 5 CREATE TABLE blocks, 1 CREATE VIEW, 3 CREATE TRIGGER, ~10 CREATE INDEX. Drop `chat_*` (180–203), `tasks_resolved` view (261–294), retarget `tasks_depth1_*` triggers (304–328) at `triggers` table. | ~150 LOC delta |
| [db_tasks.py](../../backend/db_tasks.py) → rename to `db_triggers.py` | 1142 LOC, 31 functions | Most function names rename; outcome-routing functions (`upsert_execution` line 50, the `_OUTCOME_FIELDS` set, the `task_executions` SELECT fragment) move to a new `db_dispatches.py`. `get_task_resolved` (183) and `cascade_completion` (910) **delete**. Param `task_id` → `trigger_id` throughout. | ~600 LOC delta net (heavy rename, modest logic change) |
| **`db_dispatches.py`** (new) | — | Dispatch CRUD, event log writers, `dispatch_outputs` insertion, `dispatch_triggers` join management, **the crystallize-from-trigger atomic helper** (the new transaction). Absorbs `upsert_execution` and outcome routing from db_tasks, plus session helpers from db_chat. | ~500–700 LOC new |
| [db_chat.py](../../backend/db_chat.py) | 137 | **Delete entirely.** All callers (3 files: `database.py`, `worker.py`, `queue_routes.py`) repoint to `db_dispatches`. The `cli_session_id` indirection becomes a column on `dispatches`. | -137 LOC |
| [db_dashboard.py](../../backend/db_dashboard.py) | 240 | Three queries (`dashboard_health`, `dashboard_timeline`, `dashboard_job_impact`) currently filter `tasks WHERE coalesced_id IS NULL`. They become `SELECT FROM dispatches` directly — simpler, same shape. | ~50 LOC delta |
| [db_governor.py](../../backend/db_governor.py) | 159 | `get_recent_tasks_for_governor` reads `tasks_resolved` — repoint to `dispatches`. Renames in calling code in `governor.py`. | ~30 LOC delta |
| [db_migrations.py](../../backend/db_migrations.py) | 195 | Per the no-migration-system policy: do not extend. The legacy migrations stay; new schema lands fresh. | 0 LOC delta |
| [database.py](../../backend/database.py) | 129 | Re-export shim. Update import lines for renamed/new modules. | ~10 LOC delta |
| **`migrate_db.py`** (root, one-shot) | currently ~150 LOC | Rewrite to handle the tasks → triggers + dispatches split. Add row-count parity assertions and dry-run mode. | ~300 LOC new (replaces existing) |

#### Backend — runtime

| File | Lines | Nature of change | Size |
|---|---|---|---|
| [worker.py](../../backend/worker.py) | 785 | `_process_task` → `_process_trigger`. New crystallization transaction at trigger pickup. `get_active_task_id` → `get_active_dispatch_id`. `_integrate_or_fail` (60–153) reframes around dispatch identity. Stale-task sweep on startup recognizes orphan dispatches. ~134 occurrences of `task_id` in identifiers/logs to rename. | ~250 LOC delta (rename heavy, ~60 LOC genuinely new in crystallize path) |
| [dispatch.py](../../backend/dispatch.py) | 326 | `build_user_prompt` reads `dispatch_triggers` join to assemble context (concatenate consumed triggers' `context` text by `position`). Today it reads from a single task. Param renames throughout. | ~80 LOC delta |
| [cli.py](../../backend/cli.py) | 422 | `_translate_event` and the NDJSON event handler — currently writes to `chat_events` / `chat_messages` indirectly. Repoint at `dispatch_outputs`. | ~40 LOC delta |
| [pubsub.py](../../backend/pubsub.py) | 80 | Channel key shape: keyed by `task_id` today, becomes keyed by `dispatch_id`. ~5 callsites in worker, ~3 in queue_routes. | ~20 LOC delta |
| [scheduler.py](../../backend/scheduler.py) | 122 | Calls `enqueue_task` (one site at scheduler line) — rename to `enqueue_trigger`. | ~5 LOC delta |
| [queue_routes.py](../../backend/queue_routes.py) | 728 | **31 routes** (counted via decorator grep, not 22). Most need parameter renames. Resume / reply / agent-dispatch handlers (around lines 437, 479, 660) get **transient-context renderer** rewrites — these are net new logic, ~30–50 LOC each. New `/api/dispatches/{id}/output`, `/diff`, `/timeline` endpoints. Existing `/api/tasks/*` URLs either stay as proxy shims or are renamed (Decided section says rename; mechanical pass). | ~250–350 LOC delta |
| [governor.py](../../backend/governor.py) | 418 | Datasource swap from tasks_resolved to dispatches; one rename of the recent-tasks helper. Otherwise unchanged. | ~20 LOC delta |
| [governor_routes.py](../../backend/governor_routes.py) | 97 | Minor — references to task_id in finding execution paths. | ~10 LOC delta |
| [feed_routes.py](../../backend/feed_routes.py) | 53 | Minor — feed already commit-keyed. | ~5 LOC delta |
| [git_routes.py](../../backend/git_routes.py) | 115 | `enqueue_task` call in post-commit hook (line 106) — rename. | ~5 LOC delta |
| [mcp_server.py](../../backend/mcp_server.py) | 588 | `MAISTRO_TASK_ID` env var → `MAISTRO_DISPATCH_ID` (lines 15, 45, 257). `dispatch_task` MCP tool keeps its name (it's agent-to-job dispatch, semantically distinct from the entity rename). | ~10 LOC delta |
| [governor_mcp.py](../../backend/governor_mcp.py) | 341 | Mostly internal references; small. | ~10 LOC delta |
| [mcp_config.py](../../backend/mcp_config.py) | 108 | `MAISTRO_TASK_ID` injection at line 58 → rename. | ~3 LOC delta |
| [main.py](../../backend/main.py) | 76 | `include_router` lines unchanged unless module renames cascade. | ~3 LOC delta |
| [events.py](../../backend/events.py) | 108 | **No schema change** to event types. Storage location moves from `chat_events` to `dispatch_outputs`, but on-the-wire event shape is stable. | ~0 LOC delta |
| [matching.py](../../backend/matching.py) | 42 | No change. | 0 |

#### Frontend

| File | Lines | Nature of change | Size |
|---|---|---|---|
| [api.js](../../frontend/src/api.js) | 351 | ~28 functions. Mostly param renames. Logic changes in `getTaskOutput` (now hits `/api/dispatches/{id}/output`), `streamTask` (channel keyed by dispatch_id), `getTaskDiff` (sourced from dispatch). Per Decided section URLs rename to `/api/triggers/*` and `/api/dispatches/*`, so URL strings throughout get touched. | ~80 LOC delta |
| [Queue.jsx](../../frontend/src/components/Queue.jsx) | 1326 | Resolved column rendering becomes dispatch-primary (Decided strategy C). Pending/queued columns stay trigger-primary. Subordinates display becomes the static "consumed triggers" list per dispatch card. ~50–80 LOC of real refactor; everything else is mechanical (`task` → `trigger` in identifiers). | ~120 LOC delta |
| [Tasks.jsx](../../frontend/src/components/Tasks.jsx) | 803 | Drawer renders dispatch_events timeline instead of task_events; consumed-triggers list replaces the inverted-coalescing subordinate list. Output reads from dispatch_outputs. | ~80 LOC delta |
| [Dashboard.jsx](../../frontend/src/components/Dashboard.jsx) | 489 | Backend response shape unchanged; mostly text/label renames. | ~10 LOC delta |
| [Governor.jsx](../../frontend/src/components/Governor.jsx) | 214 | Findings reference dispatches; URL changes. | ~15 LOC delta |
| [App.jsx](../../frontend/src/App.jsx) | 344 | Comments and rail labels. | ~10 LOC delta |
| [App.css](../../frontend/src/App.css) | 3054 | No semantic class names tied to entities; spot-rename comments only. | ~0–5 LOC delta |
| [util.js](../../frontend/src/util.js) | 172 | `getTaskStatus` → `getTriggerStatus` / `getDispatchStatus`; status label maps split per entity. | ~30 LOC delta |

#### Documentation

| File | Density | Treatment | Size |
|---|---|---|---|
| [CLAUDE.md](../../CLAUDE.md) | High — Data Model, Task Lifecycle, MCP sections | Restructured: "Triggers" and "Dispatches" subsections; lifecycle split into two state machines; `db_tasks` → `db_triggers` and `db_dispatches` in module table. | ~200 LOC delta |
| [STRATEGY.md](../../STRATEGY.md) | Light | Spot rename. | ~10 LOC delta |
| [REMEDIATION.md](../../REMEDIATION.md) | Light — historical references to R5 | Spot rename in shipped table. | ~5 LOC delta |
| [architecture/storage.md](../storage.md) | Heavy — 51 occurrences of "task" | Restructured: tables list, coalescing explainer, `tasks_resolved` removal. | ~100 LOC delta |
| [architecture/task-lifecycle.md](../task-lifecycle.md) | Very heavy | **Rename** to `trigger-and-dispatch-lifecycle.md` (or split into two). Two state machines documented separately. | ~150 LOC delta |
| [architecture/dispatch-engine.md](../dispatch-engine.md) | Heavy — 105 occurrences | Crystallization step documented; transient-context renderers; `dispatch_triggers` prompt assembly. | ~120 LOC delta |
| [architecture/streaming-and-sessions.md](../streaming-and-sessions.md) | Medium | `dispatch_outputs` schema; `cli_session_id` move to dispatch row. | ~60 LOC delta |
| [architecture/governor.md](../governor.md) | Light | Helper rename + datasource note. | ~30 LOC delta |
| [architecture/git-integration.md](../git-integration.md) | Light | Worktree/branch columns now on dispatches. | ~25 LOC delta |
| [architecture/frontend.md](../frontend.md) | Medium | Queue/Tasks component shape; new `/api/dispatches/*` endpoints. | ~40 LOC delta |
| [architecture/cli-bridge.md](../cli-bridge.md) | Light | Output handler now writes `dispatch_outputs`. | ~15 LOC delta |
| [architecture/prompt-assembly.md](../prompt-assembly.md) | Medium | Prompt assembly via `dispatch_triggers` join. | ~20 LOC delta |
| [architecture/trigger-system.md](../trigger-system.md) (already exists) | Medium | Trigger semantics already documented here; reconcile with new lifecycle and crystallization. | ~50 LOC delta |
| [architecture/job-configuration.md](../job-configuration.md) | Light | `coalesce_tasks` property name (rename or keep — operator-facing string). | ~5 LOC delta |
| [architecture/project-lifecycle.md](../project-lifecycle.md) | Light | Spot rename. | ~5 LOC delta |
| [architecture/tool-mediation.md](../tool-mediation.md) | Light | Spot rename. | ~5 LOC delta |

### What does *not* need to change

Worth being explicit about — these stay put:

- **`backend/git.py`** — git is the source of truth for project content; its abstraction doesn't know about tasks vs. dispatches. Worktree path / branch are passed in by the caller. 0 LOC delta.
- **`backend/events.py`** — wire format is stable. The events flow from CLI → worker → SSE consumers unchanged.
- **`backend/matching.py`** — file-glob matching for watch triggers. 0 LOC delta.
- **`backend/scheduler.py`** — cron evaluation, just one rename of the enqueue helper.
- **`backend/mcpb_import.py`** — bundle import is orthogonal.
- **MCP tool catalog** — `dispatch_task`, `read_file`, `list_jobs`, etc. all keep their names. Internal env vars rename mechanically; agents see the same surface.
- **Job EAV property system** (R6) — completely orthogonal. The recent R6 minimal changes stay as-is.
- **Governor logic** — the analysis prompt, finding shape, suggestion execution paths — only the datasource changes.

### Recommendation

**Ship Stage 1 now.** Defer Stages 2+ until a forcing function arrives.

Stage 1 captures the conceptual clarity — code names finally match how we think and talk about the model — at ~5% of the total refactor cost and near-zero risk. It validates the framing, surfaces any naming collisions, and makes the structural Stage 2+ cheaper if/when it happens.

Stage 2+ is **large but mostly mechanical**. The risky parts are well-contained: one new transaction in the worker, one rewrite of the output path, one migration script. Everything else is read-path repointing. But the current model — with its sharp edges (inverted coalescing for reply/resume; the dashboard double-count tax; `tasks_resolved` as read-time fixup) — *works*, and those sharp edges are understood and documented. Until parallel dispatch becomes a goal, or the `chat_*` dual-write bug class bites again, or cost analytics queries get painful, "defer until forced" is the right call.
