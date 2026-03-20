# Streaming and Sessions

This system handles real-time event delivery to the frontend and durable storage of task and chat output. Two distinct flows share underlying infrastructure: task streaming (execution output) and chat streaming (interactive conversation).

## Task Streaming

### Live Broadcast

The worker maintains an in-memory subscriber registry: `_subscribers` maps task IDs to sets of `asyncio.Queue` instances. When a task is processing, the worker pushes every translated event (except `_raw`) to all subscriber queues.

**Subscribe**: the SSE endpoint creates a queue, adds it to the task's subscriber set, and reads from it.
**Unsubscribe**: on client disconnect (SSE stream teardown), the queue is removed.
**Completion**: the worker broadcasts a `_done` sentinel, then clears all subscribers for that task.

### SSE Transport

`GET /api/dispatch/{task_id}/stream` returns an `EventSourceResponse` (via `sse-starlette`). The stream translates platform events to SSE event types:

| Platform event | SSE event type | Data |
|---------------|----------------|------|
| `text` | `text` | `{"content": "..."}` |
| `thinking` | `thinking` | `{"content": "..."}` |
| `tool_use` | `tool_use` | `{"tool": "...", "input": {...}}` |
| `error` | `error` | error details |
| `session_id` | `session_id` | CLI session identifier |
| `_done` | `done` | `{"status": "completed"}` |

A 30-second keepalive ping prevents proxy/browser timeouts.

If the task is already completed when the stream endpoint is hit, it returns an immediate `done` event — the client should use the stored output endpoint instead.

### Stored Output

`GET /api/dispatch/{task_id}/output` returns the durable record: all chat messages for the task's session plus the task metadata. This is the cold path — used for viewing past tasks or reconnecting after the live stream ended.

## Chat Streaming

The chat interface (`backend/chat.py`) runs an independent streaming flow for interactive conversations.

### Flow

1. `POST /api/chat/` creates (or resumes) a chat session, stores the user message, and spawns a background asyncio task
2. The background task invokes the CLI, pushes events to a session-specific `asyncio.Queue`
3. The SSE response reads from that queue and delivers to the client
4. When the CLI finishes, the background task saves the full response to the database regardless of client connection state

### Chat System Prompt

The chat system prompt is dynamically built with live project state:
- All job names, descriptions, watch/manual status, running state
- Recent task queue entries (last 10)
- Recent git log (compact one-line format)

This gives the chat agent awareness of the operational state without requiring it to query for basic information.

**TTL cache**: The rendered system prompt is cached for 8 seconds (`time.monotonic`-based). Within the TTL window, repeated chat messages reuse the cached prompt — eliminating 3 SQL queries and 1 subprocess per cache hit. The underlying state (jobs, queue, git log) changes at most every few seconds, so brief staleness is acceptable. The cache is explicitly invalidated on project close/switch.

### Decoupling from Client

The background task (`_run_cli`) runs independently of the SSE stream. If the client disconnects, the task continues to completion and saves output to the database. The `_active_chats` dict tracks which sessions have running background tasks.

## Task Session Interrogation

Completed tasks produce outcome summaries and diffs, but the user often needs follow-up: "Why did you change this file?" "What alternatives did you consider?" This requires conversational access to the agent's original session — the same context, the same reasoning chain.

### Session Resume from Resolved Tasks

Completed tasks in the Dispatch view's Resolved column provide a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact via the CLI's `--resume` capability.

### Read-Only Tool Restriction

The resumed session is dispatched with write tools removed. No `Edit`, `Write`, no `git_commit`, no branch operations. `Bash` is either removed or restricted to read-only mode. The agent can read files, search code, and reason about its prior work, but cannot modify the project. Interrogation does not produce side effects.

This is enforced through the same tool scoping mechanism used for normal dispatch — `allowed_tools` and `allowed_internal_tools` are set to read-only subsets for interrogation sessions.

### Work Flows Through Tasks

If the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.

### UI Integration

The session chat appears in the same chat tray used for standalone conversation, but with visual indicators: the job's color, a task reference, and a read-only badge. The user sees immediately that this is an interrogation session, not a live dispatch.

## Chat Sessions

Every task creates a linked chat session (`chat_sessions` table). Standalone chat conversations also create sessions, but without a task link.

Session data:
- `id`: UUID
- `job_id`: which job (if task-linked)
- `task_id`: which task record (if task-linked)
- `title`: display name (job name + task ID, or first message for standalone)
- `cli_session_id`: Claude CLI's session identifier, captured from stream events — enables resume

### Message Storage

Chat messages are simple role + content pairs. The worker stores one clean assistant message per task — preferring the authoritative `assistant_complete` content over concatenated streaming deltas.

### Raw Event Audit Trail

Every NDJSON line from the CLI is stored as a `chat_event` (session_id, event_type, raw_json). This is the complete, unprocessed record — useful for debugging, replay, or analysis.

Additionally, tool invocations that flow through the platform's internal MCP server are recorded as structured events in the same session. These are richer than NDJSON-parsed tool_use events because they capture the actual operation the platform performed (see [Tool Mediation](tool-mediation.md)).

### Incremental Persistence

Chat events are persisted incrementally during streaming — each event is written to the database as it arrives, not accumulated in memory and flushed at the end. A crash or failure mid-stream must not discard all events captured up to that point.

The stored session is the authoritative record of what the agent did. Its durability cannot depend on clean termination. This applies to all event types: text, thinking, tool_use, result, error, session_id.

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) drives the broadcast during task processing
- [CLI Bridge](cli-bridge.md) yields the events that feed both live streaming and durable storage
- [Storage](storage.md) holds the chat_sessions, chat_messages, and chat_events tables
- [Frontend](frontend.md) connects via SSE for live streaming, polls the output endpoint for stored results, and initiates task session interrogation from the Dispatch view's Resolved column
- [Tool Mediation](tool-mediation.md) provides the tool scoping mechanism used to enforce read-only interrogation sessions
