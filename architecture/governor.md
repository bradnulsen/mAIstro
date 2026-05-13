# Governor

The Governor is an autonomous meta-management agent. It reviews how jobs and tasks are performing within the platform and corresponds with the operator through a thread-based message board. It does not touch project code, does not participate in task dispatch, and does not converse outside its threads.

The Governor replaced the standalone chat interface earlier versions exposed; the current threads model in turn replaced the one-way findings feed. **Only the human can close or reopen threads** — there is no MCP tool that touches thread status, so the mute/unmute boundary is structurally enforced, not merely prompt-instructed.

**Modules**:
- `backend/governor.py` — orchestration: invocation queue + dispatcher, survey and reply prompt assembly, CLI invocation, output parsing, persistence
- `backend/governor_mcp.py` — stdio MCP server providing read tools (and write tools in reply mode)
- `backend/governor_routes.py` — REST endpoints for threads, messages, runs, status, debug
- `backend/db_governor.py` — persistence (counters, runs, threads, messages)

## Conceptual Model

A **thread** is a persistent meta-management discussion with a status (`open` / `closed`), an opener (`governor` or `human`), and an ordered append-only list of **messages**. Messages are written by either side. A Governor message may carry a structured `action_payload` (a typed proposal — `update_job_properties`, `create_job`, or `update_queue_settings`); the only response affordance is the thread's compose box. The next reply invocation reads the conversation and decides whether the human's intent is clearly affirmative; if so, it applies the change via the write tool and posts a result message describing what it did.

Closed threads are **invisible** to the Governor. They are excluded from every invocation's context packet — not even titles. Reopening restores visibility.

## Invocation Types

Every Governor activity is one of two invocation types. Each is a fresh, isolated CLI invocation with its own context packet and system prompt. Neither resumes a session.

### 1. Survey (auto trigger)

The worker increments a `governor_task_counter` config key on every executed terminal transition (`completed`, `exhausted`, `failed`, `timed_out`). When it reaches 10, the worker resets it and calls `run_governor("auto", task_count=N)`, which delegates to `_run_survey`. `cancelled` is skipped (user intent, not system behavior); `rejected` never reaches the worker.

The survey reads project state and the thin list of open threads (id, title, opener, last_activity_at, last 1–2 message bodies). It outputs a JSON array of operations:

```json
{"op": "post", "thread_id": <int>, "body": "...", "action_payload": {...} | null}
{"op": "open_thread", "title": "...", "body": "...", "action_payload": {...} | null}
```

The parser applies operations in order. Posts to non-open threads are silently dropped. A silent survey (`[]`) is acceptable. MCP mode = `read`.

### 2. Reply (human acted in a thread)

A reply runs whenever a human creates a thread or posts in an existing one. The route layer calls `governor.enqueue_reply(thread_id)`, which is idempotent — if a reply for that thread is already queued or in flight, it's a no-op. If a new message arrives while a reply is in flight, calling `enqueue_reply` again re-queues so a follow-up invocation addresses the new message after the in-flight one completes.

The reply reads the focal thread (full message history, including any prior `action_payload`s) plus a snapshot of project state. It outputs a single JSON object:

```json
{"body": "...", "action_payload": {...} | null}
```

If the model produces no parseable response, a fallback Governor message (`"[The Governor failed to produce a parseable response. The run is logged for debugging — try posting again or check the debug drawer.]"`) is written into the thread and the run is marked errored. **Failures are loud** — the human always sees something happen in the thread.

MCP mode = `write` (always). The Governor's discretion, informed by the reply system prompt and the conversation, decides whether write tools are actually called.

## Concurrency

A module-level `asyncio.Lock` (`_run_lock`) ensures at most one invocation in flight system-wide, regardless of type. Surveys and replies serialize against each other so the Governor reasons across threads against a coherent snapshot.

The reply queue is a `list[int]` of thread ids managed by `_dispatcher_loop`. `enqueue_reply` appends if the thread isn't already in the queue and ensures the dispatcher coroutine is running. The dispatcher pulls one thread id at a time, runs `_run_reply`, then pops it from the queue. Auto surveys go through `run_governor` directly under the same lock; if locked, they're skipped (the next counter cycle picks up).

The queue is an internal implementation detail — it is **not** exposed on the conversation surface. `/api/governor/status` returns only `{unread_threads, pending_proposals}`. Queue depth and running flag live on `/api/governor/debug` and back the collapsed-by-default debug drawer.

## Schema

```sql
CREATE TABLE governor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,                  -- 'auto' | 'reply'
    task_count_at_trigger INTEGER,
    message_count INTEGER DEFAULT 0,
    started_at DATETIME DEFAULT (datetime('now')),
    completed_at DATETIME,
    error TEXT
);

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

CREATE TABLE governor_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES governor_threads(id) ON DELETE CASCADE,
    author TEXT NOT NULL CHECK(author IN ('governor','human')),
    body TEXT NOT NULL,
    action_payload TEXT,                    -- JSON, nullable
    run_id INTEGER REFERENCES governor_runs(id),
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);
```

There is no `action_status` column. Whether a proposal has been executed is determinable from the thread narrative: a later Governor message describes the execution. The status badge (`pending_proposals`) walks open threads and counts those whose newest message is a Governor message with a non-null `action_payload` — when the Governor posts an "I applied this" follow-up, the count drops naturally.

## Routes

`backend/governor_routes.py` (`prefix="/api/governor"`):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/threads` | List threads (`?status=open` or `closed`), with last-message preview |
| `GET` | `/threads/{id}` | Get a thread + its full message list |
| `POST` | `/threads` | Human opens a thread (`{title, body}`); auto-queues a reply invocation |
| `POST` | `/threads/{id}/reply` | Human posts in an open thread (`{body}`); spawns or coalesces a reply |
| `POST` | `/threads/{id}/close` | Human-only mute |
| `POST` | `/threads/{id}/reopen` | Human-only restore |
| `POST` | `/threads/{id}/mark-read` | Clear `unread_for_human` |
| `GET` | `/status` | `{unread_threads, pending_proposals}` |
| `GET` | `/debug` | Operator-debug state: counter, last_run, queue_depth, running flag |
| `GET` | `/runs` | Recent runs with type / message_count / error |
| `GET` | `/recent-tasks` | Used by the Governor MCP server's `get_recent_tasks` tool |

The close and reopen routes are the **only** paths to thread-status changes. No MCP write tool touches status, structurally enforcing the human-only mute boundary.

## MCP Server (`governor_mcp.py`)

A dedicated stdio MCP server, gated by mode (`MAISTRO_GOVERNOR_MODE` env var):

### Read Tools (always available)

| Tool | Backed by |
|---|---|
| `list_jobs` | `GET /api/jobs/` — full configuration |
| `get_recent_tasks` | `GET /api/governor/recent-tasks?limit=N` — terminal tasks via `tasks_resolved` (includes `start_commit`/`result_commit`) |
| `git_status`, `git_log`, `git_diff`, `git_show` | Shared handlers in `backend/git_tools.py` (project-dir scoped). Same surface task agents have. `git_diff` accepts `base`+`head` so the Governor can pass `task.start_commit..result_commit` to see exactly what files a task touched. |
| `get_job_health` | `GET /api/dashboard?window=N` — per-job aggregates |
| `list_open_threads` | `GET /api/governor/threads?status=open` — thin list, no closed threads |
| `get_thread` | `GET /api/governor/threads/{id}` — full message history of one thread |

There is **no** tool to read closed threads. The MCP surface gives the Governor no way to bypass the operator's mute action.

### Write Tools (reply mode only)

| Tool | Backed by |
|---|---|
| `update_job_properties` | `PATCH /api/jobs/{id}` |
| `create_job` | `POST /api/jobs/` |
| `update_queue_settings` | `POST /api/queue/settings` |

The write surface is **constructive only**. There is no `delete_job`, no `disable_job`, and no thread-status tool. Removing or pausing jobs is operator-only; closure of threads is operator-only.

Mode is selected by `_write_mcp_config(project_dir, mode)` — survey runs use `read`, reply runs use `write`. The server's `tools/list` returns the appropriate set, and `tools/call` rejects write-tool invocations in read mode.

Tool calls reach the backend over HTTP (`localhost:8420`) rather than direct DB access — the MCP server is a separate process and routing through HTTP keeps the trust boundary clean.

## Context Assembly

### Survey context (`_build_survey_context`)

- Jobs (full configuration via `db.list_jobs()`)
- Recent terminal tasks via `db.get_recent_tasks_for_governor(50)` (reads through `tasks_resolved` so coalesced subordinates inherit their root's metrics)
- Job health via `db.dashboard_health(7)`
- Thin list of open threads via `db.list_open_threads_thin(50)` (id, title, opener, last_activity_at, last 1–2 message bodies)
- Git log (last 30 commits)

### Reply context (`_build_reply_context`)

- The focal thread: full message history, including any prior `action_payload`s
- Project state: jobs, recent tasks, health, git log (same as survey)
- Other open threads (titles only, for awareness — replies stay focused on the active thread)
- **No** closed-thread visibility

Both context formatters include an explicit note about coalescing semantics — that subordinates' metrics are inherited from their root and should not be summed — to prevent the Governor from misclassifying coalesce groups as "many cheap runs."

There is no persistent session. Each invocation is a fresh context window. Continuity comes from including thread state in the context, not from CLI session resume.

## System Prompts

Two distinct system prompts in `backend/governor.py`:

- **`SURVEY_SYSTEM_PROMPT`** — directs the Governor to either post in existing open threads or open new ones. Prefer updating an existing thread when in scope. Defines the operations array output contract.
- **`REPLY_SYSTEM_PROMPT`** — directs the Governor to read the focal thread, treat the latest human message as operative, **verify before writing** (read the current state of the target entity; if the world has moved on since a proposal, do not blindly apply — post a clarifying message instead), and output a single message object. When the Governor executes a previous proposal, the response message describes what was done and `action_payload` is null.

## Output Parsing

Both invocation types use robust JSON extraction (`_extract_json_array` for surveys, `_extract_json_object` for replies):

1. Direct `json.loads` on the trimmed text
2. Fenced code-block extraction
3. Bracket-matching fallback (first `[`/`{` to last `]`/`}`)

Survey output: invalid operations are silently dropped. The run completes with `message_count` reflecting how many ops actually applied.

Reply output: parse failure produces the explicit fallback message in the thread. The run is recorded with an error so the debug drawer surfaces it.

## Frontend (deferred)

The frontend rewrite for `Governor.jsx` is part of the in-flight P1 work and lands in a follow-up commit. Target shape:

- **Two-pane layout** — left pane: thread list (open at top by `last_activity_at`, "Closed (N)" disclosure below); right pane: selected thread message list with a compose box at the bottom.
- **Action payload cards** — a Governor message with an `action_payload` renders the payload as a structured "proposed change" card under the message body. **No Approve / Decline buttons.** The human's response is the compose box.
- **"+ New Thread"** at the top of the left pane — inline composer, Submit creates the thread and auto-queues a reply.
- **Close button** in the thread header (when open); **Reopen button** replacing the compose box (when closed).
- **Debug drawer** — collapsed-by-default pane with the counter, last run, queue depth, running flag, and recent runs list.
- **Rail badge** counts open threads with unread Governor messages or unexecuted proposals.

Until the frontend ships, the Governor view in the UI will not function — the new backend uses different routes and shapes than the existing component expects.

## Constraints

- **Meta-scoped**: write tools restricted to job config and queue settings. Cannot modify project files, dispatch tasks, or commit code. No `delete_job`, no `disable_job`.
- **Human-only thread status**: close and reopen are reachable only through the route layer, not through any MCP tool.
- **Closed = invisible**: closed threads are excluded from every invocation's context packet, not just deprioritized.
- **No persistent session**: each invocation is fresh context; continuity is via in-context thread state.
- **Lock-serialized**: at most one invocation in flight system-wide. Surveys that arrive during a running invocation are skipped.
- **Independent of the worker**: Governor runs do not block, queue behind, or interact with task dispatch. The two systems share the database but not the execution path.
- **Fixed cadence**: 10 executed terminal tasks. Not user-configurable.
- **Coalescing-aware**: recent-tasks reads through `tasks_resolved`; both system prompts explicitly warn against summing metrics across coalesced subordinates.

## Relationship to Other Systems

- [Storage](storage.md) — `governor_runs`, `governor_threads`, `governor_messages` tables; the `governor_task_counter` config key
- [Dispatch Engine](dispatch-engine.md) — the worker increments the survey counter on every executed terminal transition
- [CLI Bridge](cli-bridge.md) — Governor invocations use the same CLI bridge as job dispatches, with a separate MCP config
- [Tool Mediation](tool-mediation.md) — the Governor MCP server is structurally similar to the internal MCP server but scoped to meta-operations
- [Frontend](frontend.md) — the Governor view will be a thread-based message board (rewrite pending)
