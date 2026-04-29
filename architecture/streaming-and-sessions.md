# Streaming and Sessions

This system handles real-time event delivery to the frontend and durable storage of task output. There is one streaming flow — task output streaming — backed by a chat-session storage primitive that predates the platform's evolution into a task-only model. The interactive chat surface that historically used the same storage has been removed; the Governor (see [Governor](governor.md)) replaces it as the meta-aware agent surface.

## Task Streaming

### Live Broadcast

The worker maintains an in-memory subscriber registry (`backend/pubsub.py`): per-task subscriber sets of `asyncio.Queue` instances, plus a global queue-change notifier for the queue list. When a task is processing, the worker pushes every translated event to all subscribers of that task.

**Subscribe**: the SSE endpoint creates a queue, adds it to the task's subscriber set, and reads from it.
**Unsubscribe**: on client disconnect (SSE stream teardown), the queue is removed.
**Completion**: the worker broadcasts a `done` sentinel, then clears all subscribers for that task.

### SSE Transport

`GET /api/tasks/{task_id}/stream` returns an `EventSourceResponse` (via `sse-starlette`). The wire schema is owned by `backend/events.py` — the single source of truth for event types and SSE serialization (`to_sse()`).

| Event type | Data |
|------------|------|
| `text` | `{"content": "..."}` — streaming text delta |
| `thinking` | `{"content": "..."}` — extended thinking delta |
| `tool_use` | `{"tool": "...", "input": {...}}` — tool invocation start |
| `assistant_complete` | `{"content": "..."}` — full assistant turn (authoritative) |
| `result_meta` | execution metadata (stop_reason, num_turns, cost_usd) |
| `result` | final result event from the CLI |
| `session_id` | CLI session identifier |
| `error` | error details |
| `done` | `{"status": "..."}` — terminal sentinel |
| `task` | task lifecycle update (status change) |
| `queue_changed` | broadcast on the global queue stream when any task's queue position or status changes |

A 30-second keepalive ping prevents proxy/browser timeouts.

If the task is already terminal when the stream endpoint is hit, the stream returns an immediate `done` event — the client should fall back to the stored output endpoint.

`GET /api/queue/stream` is the global queue-change channel — it carries `queue_changed` events when tasks are added, transitioned, reordered, coalesced, or removed. The frontend uses this to refresh the Dispatch view without polling.

### Stored Output

`GET /api/tasks/{task_id}/output` returns the durable record: chat messages for the task's session plus task metadata. This is the cold path — used for viewing past tasks or reconnecting after the live stream ended.

`GET /api/tasks/{task_id}/outcome` returns the derived outcome summary (commits between `start_commit` and `result_commit`). `GET /api/tasks/{task_id}/diff` returns the structured diff for the same commit range. Both are derived on demand from git state, not stored.

For coalesced subordinate tasks, all three endpoints read through the `tasks_resolved` view so the user sees the root's outcome regardless of which subordinate they clicked. See [Storage — Coalescing-Aware Reads](storage.md#coalescing-aware-reads-tasks_resolved-view).

## Task Output Sessions

Every task creates a linked chat session (`chat_sessions` table) for durable output storage. The session holds:

- `id`: UUID
- `job_id`: which job
- `task_id`: which task record
- `title`: display name (job name + task ID)
- `cli_session_id`: Claude CLI's session identifier, captured from stream events — enables resume

Chat messages on the session are role + content pairs. The worker stores one clean assistant message per task — preferring the authoritative `assistant_complete` content over concatenated streaming deltas.

### Raw Event Audit Trail

Every NDJSON line from the CLI is stored as a `chat_event` (session_id, event_type, raw_json). This is the complete, unprocessed record — useful for debugging, replay, dashboard tool-usage analysis, and Governor analysis.

Additionally, tool invocations that flow through the platform's internal MCP server are recorded as structured `mcp_tool_use` events in the same session. These are richer than NDJSON-parsed tool_use events because they capture the actual operation the platform performed (see [Tool Mediation](tool-mediation.md)).

### Incremental Persistence

Chat events are persisted incrementally during streaming — each event is written to the database as it arrives, not accumulated in memory and flushed at the end. A crash or failure mid-stream must not discard all events captured up to that point.

The stored session is the authoritative record of what the agent did. Its durability cannot depend on clean termination. This applies to all event types: text, thinking, tool_use, result, error, session_id.

Batch insert helpers (`add_chat_events_batch`) amortize transaction overhead when many events arrive in quick succession, but flushes happen frequently enough that a crash loses at most a small batch.

## Task Session Interrogation

Completed tasks produce outcome summaries and diffs, but the user often needs follow-up: "Why did you change this file?" "What alternatives did you consider?" This requires conversational access to the agent's original session — the same context, the same reasoning chain.

### Session Resume from Resolved Tasks

Completed tasks in the Dispatch view's Resolved column provide a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact via the CLI's `--resume` capability.

The infrastructure exists today (the CLI bridge supports `--resume`, jobs can be configured with `allowed_internal_tools`, the `chat_sessions.cli_session_id` column captures the resume target). The remaining work is the UI surface and a read-only tool-stripping pass on dispatch — see `STRATEGY.md` priority P2.

### Read-Only Tool Restriction

The resumed session is dispatched with write tools removed. No `Edit`, `Write`, no `git_commit`, no branch operations. `Bash` is either removed or restricted to read-only mode. The agent can read files, search code, and reason about its prior work, but cannot modify the project. Interrogation does not produce side effects.

This is enforced through the same tool scoping mechanism used for normal dispatch — `allowed_tools` and `allowed_internal_tools` are set to read-only subsets for interrogation sessions.

### Work Flows Through Tasks

If the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) drives the broadcast during task processing
- [CLI Bridge](cli-bridge.md) yields the events that feed both live streaming and durable storage
- [Storage](storage.md) holds the `chat_sessions`, `chat_messages`, and `chat_events` tables
- [Frontend](frontend.md) connects via SSE for live streaming, polls the output endpoint for stored results, and initiates task session interrogation from the Dispatch view's Resolved column
- [Tool Mediation](tool-mediation.md) provides the tool scoping mechanism used to enforce read-only interrogation sessions
- [Governor](governor.md) replaces the standalone chat interface — the system's meta-aware surface is now Governor findings, not interactive conversation
