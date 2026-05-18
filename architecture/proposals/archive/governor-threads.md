# Proposal: Governor Threads

## Status

Draft.

## Summary

Replace the Governor's one-way findings feed with a thread-based message board. Each thread is a meta-management discussion between the Governor and the human operator. Two invocation types: **surveys** (the existing 10-task auto trigger, reframed) and **replies** (any human action in any thread). Approvals collapse into replies — there are no Approve/Decline buttons; the human just writes back, and the Governor decides whether to execute pending proposals based on the conversation. Closing a thread mutes it from the Governor's context entirely; reopening is human-only.

## Goals

- Turn Governor↔Human interaction into a two-way conversation without reintroducing the chat surface that earlier versions had.
- Preserve the "Governor is a batch agent" property — every interaction is a discrete, isolated CLI invocation with an assembled context packet, never a long-running session.
- Let humans ask questions and reply to Governor proposals, and have those replies actually get reasoned about.
- Keep all existing trigger and worker behavior intact. The cadence (every 10 terminal tasks), counter logic, and worker integration do not change.

## Non-goals (scope guard)

The following are explicitly **out of scope** for this proposal. If the implementation pulls in changes to any of these, treat it as a red flag:

- The job/task system (workers, dispatch, queue, coalescing).
- The worker's auto-trigger logic in `_check_governor_trigger`. The interface (`run_governor("auto")`) stays the same; only what happens *inside* that function changes.
- The internal MCP server (`mcp_server.py`). The Governor's MCP server (`governor_mcp.py`) is the only one that gets refactored.
- The CLI bridge. Governor invocations continue to use `cli.invoke` unchanged.
- Schema for `jobs`, `tasks`, `task_events`, `chat_*`. None of those tables are touched.

## Conceptual Model

### Thread

A persistent, ordered sequence of messages between Governor and Human about a meta-management topic. Threads have:

- `id` (int)
- `title` (set on creation — by Governor for survey-opened threads, by Human for human-opened threads)
- `status` — `open` or `closed`. **Closed means muted from the Governor's context** — closed threads are excluded entirely from every invocation's context packet (not even titles). They remain visible to the human for reading, but the Governor cannot see them and cannot post in them.
- `opener` — `governor` or `human`
- `created_at`, `last_activity_at`, `closed_at`
- `unread_for_human` — boolean, set when Governor posts, cleared when human views the thread

Lifecycle:
- The human can close a thread at any time via a button. The Governor cannot close threads — closure is a human-only mute action.
- The human can reopen a closed thread. **Reopening is human-only.** The Governor cannot reopen, structurally — there is no MCP write tool that touches thread status, and no route the Governor can reach that re-opens a thread.
- Closed threads block both directions: no new posts (compose disabled), no Governor visibility. The thread is archived for reading.

### Message

A single post in a thread. Messages have:

- `id` (int)
- `thread_id`
- `author` — `governor` or `human`
- `body` (text — the post itself)
- `action_payload` (json, nullable) — optional structured proposal attached to a Governor message (see below). Used for UI rendering and to give the next reply invocation a precise target if the human assents.
- `run_id` (nullable) — references the `governor_runs` row that produced this message (null for human messages)
- `created_at`

Messages are append-only. Edits, deletions, reactions: all out of scope for v1.

### Action Payload

A Governor message may carry a structured proposal as `action_payload`. This is the precise, machine-readable form of a suggestion ("set Job 42's max_turns to 20"). Payloads are typed:

| Action type | Payload |
|---|---|
| `update_job_properties` | `{job_id, properties: {...}}` |
| `create_job` | `{name, description, properties}` |
| `update_queue_settings` | `{settings: {...}}` |

**The Governor's write surface is constructive only.** No `delete_job`, no `disable_job`. Removing or pausing a job is an operator-only action performed in the Jobs view; the Governor can flag a job as low-value in prose, but it cannot propose or execute removal.

**There is no `approved`/`declined`/`executed` state machine.** Approvals are not a separate UI surface — there are no buttons. When the Governor posts a message with an `action_payload`, the UI renders it as a structured "proposed change" card under the message body, but the only response affordance is the thread's normal compose box. The human just writes back: "yes, do it" or "I disagree because..." or anything else.

What happens next is a reply invocation. Every reply invocation has write tools available. The reply's context includes the thread (so it sees both the prior proposal and the human's response) and any unexecuted `action_payload`s from prior Governor messages in the thread. The Governor's system prompt instructs it to: read the conversation, decide whether the human's intent on a still-unexecuted proposal is clearly affirmative, and if so, apply the change via the write tool and post a result message describing what it did. If intent is unclear, ambivalent, or contradicted, it does not execute and posts a clarifying message instead.

When a reply invocation coalesces multiple human messages, the **latest message wins** — the Governor's reading of intent prioritizes the most recent input.

Whether a proposal has been executed is determinable from the thread narrative: a subsequent Governor message that says "I applied this change" carries the execution result, and the UI can mark the proposal card as completed by inspecting subsequent messages. There is no `action_status` column to maintain.

Closing a thread is a pure human UI action — a button in the thread view, no Governor proposal involved. The Governor can suggest closure in prose ("I think this is resolved — feel free to close.") but cannot itself trigger it.

## Invocation Types

Every Governor activity is one of two invocation types. Each is a **fresh, isolated CLI invocation** with its own context packet and system prompt. None resume a session.

### 1. Survey (auto trigger, unchanged cadence)

Triggered by the worker's existing 10-task counter. Replaces the current "produce a batch of findings."

**System prompt directive**: review project state and open threads. Either post updates in existing open threads, or open new threads for new concerns. Prefer updating an existing thread when the new observation is in scope of one — *check open threads before opening a new one*. Closed threads are not in scope; if a topic on a closed thread becomes relevant again, open a new thread (the closed one was muted by the operator for a reason).

**Context packet**:
- Project state: `list_jobs()`, `get_recent_tasks_for_governor(50)`, `dashboard_health(7)`, `git log --oneline -30`
- **Thin list of open threads** — id, title, opener, last_activity_at, last 1–2 message bodies for context. Not full thread history (cost). The Governor can fetch full history of any specific thread via the `get_thread` MCP tool when needed.
- Unexecuted action payloads across open threads (so it knows what's unresolved)
- **No closed threads** — they're invisible. Not even titles.

**MCP mode**: read.

**Output contract**: a JSON array of operations, where each operation is one of:
```json
{"op": "post", "thread_id": <int>, "body": "...", "action_payload": {...} | null}
{"op": "open_thread", "title": "...", "body": "...", "action_payload": {...} | null}
```

The parser applies operations in order. Invalid operations are silently dropped (same robustness as the current finding parser). A survey may produce zero operations — that's fine; silent surveys are allowed.

### 2. Reply (human acted in a thread)

A reply invocation is the Governor responding to a human action inside a specific thread. It is the only invocation type that produces messages bound to a single thread, and it covers every form of human-Governor exchange: posting text, opening a new thread, or assenting to a Governor proposal.

There is no separate "approval" invocation. Approvals are just text replies — the Governor's reply invocation reads the conversation, sees an unexecuted proposal and the human's affirmative response to it, and decides to execute. Equivalently, the Governor sees a refusal or a question and decides not to execute. Reply invocations are uniformly write-capable; using the write tools is a judgment call the Governor makes per-invocation based on context.

**Triggers (any of):**
- Human posts text in the thread compose box.
- Human creates a new thread (this auto-queues a reply invocation on the new thread, where the human's opening text is the only message so far).

**System prompt directive**: continue this thread. Read its full history and the current project state. The latest human message is the operative input — earlier coalesced messages are context, but the latest expresses the current intent. Decide:

1. If a prior Governor message in this thread carries an unexecuted `action_payload` and the human's latest message clearly affirms it (or asks you to proceed), apply the change via the write tool. **Verify before writing** — read the current state of the target entity first, and if the world has moved on since the proposal was made (the property already changed, the job no longer exists, etc.), do not blindly apply; post a clarifying message describing what changed and what you'd do instead.
2. Otherwise, post a single response message. You may attach a new `action_payload` if your response is a fresh proposal.

Do not chain proposals onto an execution: when you execute, the response message describes what you did and `action_payload` is null.

**Context packet**:
- The thread: title, full message history, including any unexecuted `action_payload`s from prior Governor messages
- Project state: jobs, recent tasks, health, git log
- Open threads list (titles only, for awareness — but no other threads' content; replies stay focused on the active thread)
- **No** closed-thread visibility

**MCP mode**: write (always). The Governor's discretion — informed by the system prompt and conversation — determines whether write tools are actually used.

**Output contract**: a single message:
```json
{"body": "...", "action_payload": {...} | null}
```

A reply invocation is bound to its thread — it cannot post elsewhere or open new threads. Surveys are the only invocation type that can create threads on the Governor's initiative.

**Coalescing**: replies are thread-scoped and coalesce. If a reply invocation is already queued or in flight for thread X and the human posts again in thread X, the new message merges into the pending invocation's context rather than enqueueing a second invocation. The Governor sees the timeline of human messages and treats the latest as operative. One response covers everything.

## Concurrency Model

### Lock

The existing module-level `_run_lock` stays. **At most one Governor invocation in flight, system-wide**, regardless of type. Surveys and replies serialize against each other.

The reasoning is the same as today: Governor reasoning across threads benefits from observing a coherent snapshot, and replies (which may write) should never race against surveys.

### Queue

Unlike today's "drop on collision" behavior for auto triggers:

- **Survey** (auto): if locked, drop. The next 10-task counter cycle picks it up. (Unchanged from today.)
- **Reply** (thread action): queue, **per-thread coalesced**. If a reply invocation already exists in the queue or is in flight for thread X, the new human message merges into that pending invocation's context rather than enqueueing a second invocation. The Governor produces one response that addresses everything; the latest human message is the operative one.

The queue is a simple FIFO of `(invocation_type, thread_id_if_any, payload)` tuples in `governor.py`. When the lock frees, the next item is picked. Reply coalescing is implemented by checking whether a queued reply already targets the same thread before enqueueing.

### Backpressure

The queue is an internal implementation detail — it is **not** exposed in the UI. The human sees the thread and the messages that materialize in it; they never see queue depth, "your reply is queued behind N others," or any other plumbing. If a human replies to thread X while a reply for X is already queued, the new input merges into that pending invocation (per-thread coalescing) and the next Governor message in the thread covers everything. That's the whole user-visible story. From the operator's vantage point, they wrote something in a thread, and a Governor message appeared in that thread some time later. No queue, no scheduler, no diagnostics surfaced.

The status endpoint and the rail badge expose only conversation-level state — open threads with unread Governor messages, threads with proposed actions awaiting approval. They never expose `queue_depth` or anything analogous.

## Schema Changes

### New tables

```sql
CREATE TABLE governor_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    opener TEXT NOT NULL CHECK(opener IN ('governor','human')),
    unread_for_human INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    last_activity_at DATETIME NOT NULL DEFAULT (datetime('now')),
    closed_at DATETIME
);

CREATE INDEX idx_governor_threads_status ON governor_threads(status, last_activity_at DESC);

CREATE TABLE governor_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES governor_threads(id) ON DELETE CASCADE,
    author TEXT NOT NULL CHECK(author IN ('governor','human')),
    body TEXT NOT NULL,
    action_payload TEXT,                    -- JSON, nullable; structured proposal on Governor messages
    run_id INTEGER REFERENCES governor_runs(id),
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_governor_messages_thread ON governor_messages(thread_id, id ASC);
```

There is no `action_status` column. Whether a proposal has been executed is determinable from the thread narrative: a later Governor message describes the execution. The UI computes "executed/pending/superseded" by walking the thread, not by maintaining state.

### Dropped table

`governor_findings` is removed. Per the project's "no migration system" policy, the dev DB is recreated. Existing project DBs that had findings can either be recreated (preferred) or run through a one-shot script in `migrate_db.py` that converts each finding into a single-message thread. The script is optional, not part of the running backend.

### Touched table

`governor_runs` keeps its current shape but its semantics broaden — every invocation writes a run row. The `trigger` value set becomes `auto`, `reply`. The previous `manual` and `execution` values are gone: manual triggers don't exist anymore (the human creates a thread instead), and execution collapses into reply. Existing rows with old trigger values are historical and can stay; the read path tolerates them.

## Routes

### Removed

- `GET /api/governor/findings`
- `POST /api/governor/findings/{id}/approve`
- `POST /api/governor/findings/{id}/decline`
- `POST /api/governor/findings/{id}/read`
- `POST /api/governor/findings/{id}/dismiss`
- `POST /api/governor/trigger` — no manual trigger; humans create threads to talk to the Governor
- `POST /api/governor/messages/{id}/approve` — approvals are not a separate route; humans assent in prose via thread reply
- `POST /api/governor/messages/{id}/decline` — same; declines are prose

### Added

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/governor/threads` | List threads, optional `?status=open\|closed`, ordered by `last_activity_at` |
| `GET` | `/api/governor/threads/{id}` | Get a thread + its full message list |
| `POST` | `/api/governor/threads` | Human opens a thread. Body: `{title, body}`. Auto-queues a reply invocation on the new thread. |
| `POST` | `/api/governor/threads/{id}/reply` | Human posts in an open thread. Body: `{body}`. Spawns or coalesces into a reply invocation. |
| `POST` | `/api/governor/threads/{id}/close` | Close (mute) the thread. Human-only. |
| `POST` | `/api/governor/threads/{id}/reopen` | Reopen a closed thread. **Human-only** — there is no MCP write tool that touches thread status. |
| `POST` | `/api/governor/threads/{id}/mark-read` | Clear `unread_for_human` |

### Changed

| Method | Path | Change |
|---|---|---|
| `POST` | `/api/governor/trigger` | Removed. There is no manual trigger anymore — the human starts conversations by creating threads. |
| `GET` | `/api/governor/status` | Returns: `{unread_threads, pending_proposals}`. `pending_proposals` counts open threads where the latest Governor message has an unexecuted `action_payload`. The 10-task counter, `last_run`, queue depth, and running-flag are all internal — surfaced only inside the debug drawer (see Frontend), never on the conversation surface. |
| `GET` | `/api/governor/runs` | Unchanged shape; `trigger` field values now restricted to `auto`, `reply`. Used only by the debug drawer. |
| `GET` | `/api/governor/recent-tasks` | Unchanged. Still used by the MCP server. |
| `GET` | `/api/governor/debug` | New. Returns operator-debug state: counter, last_run, queue_depth, running flag. Backs the collapsed-by-default debug drawer. |

## MCP Server Changes (`governor_mcp.py`)

### Read tools (always available)

Existing tools stay:
- `list_jobs`
- `get_recent_tasks`
- `get_git_log`
- `get_job_health`

`get_prior_findings` is **removed**. Replaced by:

| New tool | Backed by |
|---|---|
| `list_open_threads` | `GET /api/governor/threads?status=open` — thin list (id, title, opener, last_activity_at) |
| `get_thread` | `GET /api/governor/threads/{id}` — full message history of one thread |

There is **no** `list_recent_closed_threads`. Closed threads are invisible to the Governor by design — they were muted by the operator. The MCP surface does not provide any way for a Governor invocation to read them.

The survey context packet pre-loads the thin open-threads list. `get_thread` is for when the Governor decides one specific thread is relevant and needs the full history. This is the equivalent of a "drill-down" tool — keeps the upfront context cost bounded but lets the agent expand on demand. Reply invocations don't need `list_open_threads` because their context already includes the focal thread; they receive the open-threads list as titles in the prompt for awareness only.

### Write tools (reply invocations only)

- `update_job_properties` — kept
- `create_job` — kept
- `update_queue_settings` — kept

`delete_job` and `disable_job` are **not** in the write surface. The Governor's writes are constructive only — modify or create configuration, never remove or pause it. Operators perform deletion and disabling manually in the Jobs view; the Governor can flag a job as low-value in prose but cannot act on it.

Action payloads in Governor messages are typed (`update_job_properties`, `create_job`, `update_queue_settings`). There is no `close_thread` action type — closure is a pure UI action.

### Mode env

`MAISTRO_GOVERNOR_MODE` stays. `read` mode is used for surveys; `write` mode is used for replies (always — the Governor's discretion within the reply, informed by the system prompt and conversation, determines whether write tools are actually invoked).

## Frontend Reshape (`Governor.jsx`)

### Layout

Two-pane layout, with a third collapsible debug drawer on the same page:

- **Left pane (≈30% width)** — thread list. Open threads at top, ordered by `last_activity_at`. Each row shows title, last message preview (40 chars), unread dot, message count, and a "proposal" pill if the latest Governor message in the thread carries an unexecuted `action_payload`. Closed threads collapse below in a "Closed (N)" disclosure — humans can click into them to read, but they don't show unread badges or proposal pills.
  - At the top of the left pane: a **"+ New Thread"** button. Clicking it opens an inline composer (title + body), and Submit creates the thread + auto-queues a reply invocation. This is how humans initiate Governor conversations — there is no separate "Ask the Governor" question box.
- **Right pane (≈70% width)** — selected thread. Chronological message list (oldest → newest). Governor and Human messages styled distinctly. When a Governor message has an `action_payload`, it renders as a structured "proposed change" card under the body, showing the action type and parameters. **There are no Approve / Decline buttons.** The card is a visual aid; the human's response is the compose box.
  - At the bottom: a compose textarea + Send button when the thread is open. The Send button is disabled when there's no text, and shows a subtle "Governor is thinking..." indicator after submit until the Governor's response arrives. When the thread is closed, the compose area is replaced by a Reopen button.
  - A header on the thread view shows the title and a Close button (when open).

### Debug drawer (collapsed by default)

A third pane, collapsed by default, accessible via a small "Debug" toggle in a corner of the page. When expanded, it shows operator-internal state:

- The 10-task auto-trigger counter
- Last run timestamp and trigger type
- Queue depth and running flag
- Recent runs list with type, message-count produced, and any error

This is the operator escape hatch for "is the Governor working?" questions. It's deliberately out of the way — the conversation is the primary surface, the drawer is a diagnostic.

### Rail badge

The Governor rail item shows a single number: open threads with either unread Governor messages or unexecuted proposals. Nothing about queue depth, counter, or run state is surfaced on the rail.

### State

The `Governor.jsx` component fetches threads + status on a 5s poll (same cadence as today). Clicking a thread loads its full message list and calls `mark-read`. Submitting a reply optimistically appends the human message and shows a "Governor is thinking..." indicator until the next poll picks up the Governor's response.

### Removed UX

- Findings feed (replaced by thread list + thread view)
- "Read" / "Dismiss" actions on observations (closure mutes the whole thread instead)
- Approve / Decline buttons (gone — humans assent in prose via the compose box)
- "Ask the Governor" question box (gone — humans create threads instead)
- "Run analysis" manual trigger button (gone — same)

## Backend Module Changes

Strictly Governor-scoped — no edits outside these files.

### `backend/db_governor.py`

Add CRUD helpers:
- `create_thread(title, opener)` → thread_id
- `get_thread(thread_id)` → row + messages
- `list_threads(status=None, limit)` → rows
- `set_thread_status(thread_id, status)` (close/reopen; route is the only caller and is human-only)
- `mark_thread_read(thread_id)`
- `add_message(thread_id, author, body, action_payload, run_id)` → message_id; updates `last_activity_at` and (when author=`governor`) `unread_for_human`
- `count_unexecuted_proposals(thread_id_or_none)` (for status badge — walks open threads, finds messages with `action_payload` whose subsequent thread messages don't reference an execution)

Remove: `create_governor_finding`, `get_governor_finding`, `get_governor_findings`, `update_governor_finding`.

Keep as-is: `create_governor_run`, `complete_governor_run`, `get_governor_runs`, `get_governor_status` (with shape changes), counter helpers, `get_recent_tasks_for_governor`.

### `backend/governor.py`

Refactor along invocation-type lines:

```
run_governor(trigger, ...)             # entrypoint, dispatches by type
  ├─ _run_survey()                     # auto trigger — current behavior reframed
  └─ _run_reply(thread_id)             # new — human action in thread; uniformly write-capable
```

Each builds a different context packet via separate helpers (`_build_survey_context`, `_build_reply_context`) and uses a different system prompt constant (`SURVEY_SYSTEM_PROMPT`, `REPLY_SYSTEM_PROMPT`). The reply system prompt instructs the Governor to verify before writing and to interpret the latest human message as the operative input. The current `execute_suggestion` function disappears — its behavior is folded into `_run_reply` (the Governor's discretion, informed by the conversation, decides whether to write).

Output parsing: separate helpers for each invocation type — survey produces a JSON array of operations (`post`/`open_thread`); reply produces a single message object. Both robust to malformed output. **Reply fallback**: if the model produces no parseable message, the system writes a fallback Governor message into the thread ("[The Governor failed to produce a response. The run is logged for debugging.]") and the run is marked as errored. Failures are loud — the human always sees something happen in the thread, and the debug drawer surfaces the error.

Add a queue + dispatcher:
- `_invocation_queue: list[QueueItem]`
- `_dispatcher_task: asyncio.Task` — long-running coroutine that pulls from the queue under the lock and runs invocations
- Per-thread coalescing: before enqueueing a reply, check whether a reply for the same thread is already queued or in flight; if so, attach the new human message to that pending invocation's payload and do not enqueue a second invocation.

The current `spawn` helper for fire-and-forget tasks stays.

### `backend/governor_mcp.py`

- Remove `get_prior_findings`. Add `list_open_threads`, `get_thread`. **Do not add a closed-threads tool** — closed threads are invisible by design.
- Remove `delete_job` from write tools. Do not add `disable_job`. The write surface stays purely constructive: `update_job_properties`, `create_job`, `update_queue_settings`.
- Mode gating logic unchanged. Surveys run in `read` mode; replies run in `write` mode (the Governor decides whether to actually call write tools per invocation).

### `backend/governor_routes.py`

- Remove findings routes.
- Remove `/trigger` and message approve/decline routes — gone with the buttons.
- Add thread routes (list, get, create, reply, close, reopen, mark-read).
- Add `/debug` route (operator-internal state).
- Update `/status` shape.
- Close and reopen routes are the only paths to thread-status changes, and neither is reachable from any MCP write tool — structurally enforces human-only mute/unmute.

### Architecture doc

`architecture/governor.md` is rewritten to describe threads, the two invocation types, and the queue. Findings terminology is removed. Document explicitly states: *only the human can close or reopen threads, and this is enforced by the MCP tool surface, not just by prompt instruction.*

## Migration

Per the project's no-migration-system policy:

1. Edit `SCHEMA_SQL` in `db_core` to drop `governor_findings` and add `governor_threads`, `governor_messages`.
2. Recreate the dev DB.
3. Optional one-shot `migrate_db.py` script for any existing project DB the user wants to preserve: each finding becomes a single-message thread (`title=finding.title`, `opener=governor`, single message with `body=finding.body`, `action_payload` mapped from finding fields if applicable).

`db_migrations.py` is **not** the place for this — that module is reserved for legacy DB shape conversions, not for feature migrations. The user's preference (per memory) is bespoke scripts.

## Open Questions

These remain genuinely unresolved — defaults are listed but worth a deliberate call.

1. **Closed-thread completeness.** Closed threads are invisible to the Governor. But: closed threads are still in the DB and could in principle be re-included if the human reopens. Reopening adds it back to surveys' open-threads list. That's the simple model. **Recommended default: closed = invisible, full stop. Reopen restores visibility.** No partial states.
2. **Run history detail in the debug drawer.** The drawer shows a list of recent runs. How much detail? Counts only? Per-run message-list links? Full prompt/output dumps? Recommended default: list with type, timestamp, message-count produced, error if any. Drill-down to per-run prompt/output is a v2 add.
3. **Thread search.** Out of scope for v1.
4. **Proposal staleness boundary.** The reply system prompt instructs the Governor to verify current state before executing a stale proposal. There's no automatic time bound — a 10-day-old proposal and a 10-second-old proposal are treated identically (verify, then act). Could add an "if proposal older than N hours, always re-confirm with the human" rule. Recommended default: no time bound; trust the verify-before-write directive.

## Sequencing

Suggested order:

1. Schema + db helpers (`db_governor.py`) — `governor_threads`, `governor_messages`, helpers; drop findings table
2. Routes (`governor_routes.py`) — thread CRUD, reply, close/reopen, mark-read, debug; remove findings, trigger, message approve/decline
3. Refactor `governor.py` into two invocation subfunctions (`_run_survey`, `_run_reply`) + queue dispatcher with per-thread coalescing + reply fallback
4. Refactor `governor_mcp.py` — remove findings tools, add `list_open_threads` and `get_thread`, drop `delete_job`
5. Frontend — replace `Governor.jsx` with the two-pane + collapsed-debug-drawer layout; remove buttons; add "+ New Thread"
6. Architecture doc rewrite (`architecture/governor.md`)
7. Optional: migration script in `migrate_db.py`

Each step is independently reviewable. Steps 1–4 leave the system in a working state at every commit (the old findings code can be removed in step 1; the new infra is in by step 4). Step 5 swaps the UI; the backend is ready before then.
