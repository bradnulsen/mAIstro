# Strategy

## Current State

mAistro is a working, self-sustaining orchestration platform with a closed, robust core loop. Tasks configure, queue, dispatch via Claude CLI, stream output in real-time, enforce timeouts, and commit results to git. Watch, schedule, and task-to-task triggers fire automatically. The dispatch lifecycle is **resilient** — failed dispatches can be resumed (preserving CLI session state) or retried from scratch. **Dispatch diffs** now show exactly what code an agent changed, directly in the Queue detail panel.

What's in place:
- **Queue-first dispatch** — background worker, auto/manual processing, stale sweep, cancellation with confirmation UX
- **Dispatch continuity** — resume (via CLI `--resume` with session ID) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display in Queue detail panel
- **Live dispatch streaming** — SSE endpoint with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task timeout with watchdog, graceful terminate then kill, partial output preserved
- **Watch mode** — post-commit hook, glob subscription matching, per-task toggle, cooldown
- **Task-to-task dispatch** — `task_queue` trigger with context passing and upstream summary injection
- **Cron scheduling** — timed dispatch with fire tracking that survives restarts
- **Coalesced dispatches** — multiple triggers merge into one queue entry
- **Rich Queue UI** — upcoming/past filtering, editable context, live streaming, stored output as markdown, cancel confirmation, resume/retry actions, diff view
- **Task config** — two-column layout, markdown preview, auto-resize textareas, subscription file preview, dispatch spinner with animation
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Persistent DB connection** — pooled async SQLite, eliminates connection-per-call overhead
- **Clean route architecture** — dispatch/queue routes extracted to `queue_routes.py`, chat routes in `chat.py`, Pydantic request models throughout

## What Matters Now

The core loop is complete: dispatches run, stream, show diffs, timeout, cancel, resume, and retry. The frontend is polished enough to use without friction. The code architecture is clean and modular.

The platform has reached a point where remaining work is **additive, not foundational**. The next phase is about three things:

1. **Configuration** — exposing existing backend capabilities through UI (settings are hidden behind raw API calls)
2. **Composition** — declarative task relationships that turn a "collection of tasks" into "coordinated workflows"
3. **Safety** — approval gates that make unattended operation trustworthy

## Priority 1: Settings and Configuration UI

**The problem:** MCP server management, model defaults, and project-level config all have working backend routes but no UI. There's no way to configure these things without knowing the API.

**Why first:** This is the lowest-effort, highest-polish priority. Backend capabilities already exist that users can't reach. A settings view turns hidden API endpoints into discoverable features. It makes the product feel complete rather than developer-only. And it unblocks the next priorities — dependency config and approval toggles will need a place to live.

**Specifically:**
- Settings view accessible from a new rail icon, rendering as a full view (like Feed, Tasks, Queue)
- Sections:
  - **Queue behavior** — auto-dispatch toggle (duplicates Queue header control, but discoverable here too)
  - **Default model** — select from available models
  - **Default timeout** — numeric input in seconds
  - **MCP servers** — list, add, remove server configurations (backend CRUD already exists)
  - **Project info** — path display, git status summary
- Single scrollable page with section headers, not tabs
- API routes already exist: `/api/mcp/servers`, `/api/config/`, `/api/config/{key}`

## Priority 2: Task Dependencies and Workflows

**The problem:** Task-to-task dispatch works ad-hoc (a running task can queue another via context), but there's no declarative way to say "Architect runs after Strategist." This limits workflows to what individual tasks remember to do, rather than what the system enforces.

**Why second:** With settings in place, the system is visible and configurable. Dependencies make it *composable* — the leap from "collection of tasks" to "coordinated workflow." This is the feature that makes mAistro qualitatively different from just running CLI agents manually.

**Specifically:**
- Add optional `depends_on` task property (list of task IDs)
- When a task's dispatch completes successfully, auto-enqueue tasks that declare it as a dependency
- Pass the completing dispatch's summary downstream as context
- Queue UI shows dependency chain relationships
- Compose with existing triggers: a task with `depends_on: ["strategist"]` and `watch_enabled: true` fires on either condition, with coalescing handling overlap

**Design consideration:** Dependencies should be a new trigger type (`dependency`), not a replacement for watch or schedule. The enqueue happens in `_process_dispatch` after successful completion, checking `depends_on` across all tasks. This keeps the trigger model uniform.

## Priority 3: Approval Gates

**The problem:** As mAistro becomes more autonomous (watch + schedule + dependencies), there's no way to require human review before a dispatch executes. A watch-triggered dispatch on a sensitive task could make unwanted changes with no checkpoint.

**Specifically:**
- Add optional `require_approval` boolean task property (default: false)
- Approval-gated dispatches enter a `pending_approval` status instead of being immediately processable
- Queue UI shows approval-pending items with approve/reject actions
- Approved dispatches proceed normally; rejected dispatches are marked as skipped
- Manual dispatches bypass approval (you already chose to run it)

**Why this matters:** This is the safety valve that makes unattended operation trustworthy. Without it, increasing automation means increasing risk. With it, you can run watch + schedule on everything and still maintain control over what actually executes.

## Deferred

Valuable but not blocking the current phase:

- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and the global `PROJECT_DIR` state.
- **Parallel dispatch** — concurrent dispatch execution. Valuable for independent tasks but introduces git conflict complexity. Sequential is correct until it's the bottleneck.
- **Cost tracking** — token usage and API costs per dispatch. Claude CLI doesn't expose this cleanly yet.
- **Structured error types** — dispatch errors are strings. An error classification system (timeout, cancelled, context_limit, tool_error, unknown) would improve observability and enable smarter retry logic.
- **Status bar click-through** — task chips in the status bar could navigate to the running dispatch in Queue view. Small UX win.
- **Dark mode** — the monospace aesthetic works. Nice-to-have.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. Building a custom LLM layer is massive effort for marginal gain.

## Completed

- **Dispatch Diff View** (was P1) — `start_commit` recorded when dispatch begins, `result_commit` on completion. Queue detail panel shows collapsible diff section with file-level summary and per-file expandable diffs. Reuses `git.diff()` infrastructure. Shipped in `def9824`.
- **Dispatch Continuity — Resume and Retry** (was P1) — Resume reuses CLI session via `--resume`, retry re-enqueues with original context. New trigger types `resume` and `retry`. `resume_session_id` column on `dispatch_queue`, chat session reuse for resume. Queue detail panel shows Resume/Retry buttons on completed dispatches. Shipped in `fdd357b`, refined in `11b225d` and `b35b3cc`.
- **Route Modularization** (was Deferred) — Dispatch and queue routes extracted to `queue_routes.py` with Pydantic request models. `main.py` down to project, task, feed, git, hook, MCP, and config routes. Shipped in `b35b3cc` and `b18ea99`.
- **Dispatch Timeout Enforcement** (was P1) — configurable per-task `timeout` property via EAV system, watchdog in worker enforces it, graceful terminate + kill after 5s grace period, partial output preserved with "timed out" indicator. Shipped across commits `52d1722` through `d0e4426`.
