# Strategy

## Current State

mAistro is a fully operational, self-sustaining multi-agent orchestration platform. The core dispatch loop is resilient and complete. The UI exposes all backend capabilities — from task configuration and queue management to settings, MCP servers, and model selection. The codebase is cleanly modularized with extracted route modules, a shared state layer, and Pydantic models throughout.

What's in place:
- **Queue-first dispatch** — background worker, auto/manual processing, stale sweep, cancellation with confirmation UX
- **Dispatch continuity** — resume (via CLI `--resume` with session ID) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display in Queue detail panel
- **Live dispatch streaming** — SSE endpoint with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task timeout with watchdog, graceful terminate then kill, partial output preserved
- **Watch mode** — post-commit hook, glob subscription matching, per-task toggle, cooldown
- **Task-to-task dispatch** — `task_queue` trigger with context passing and upstream summary injection
- **Cron scheduling** — timed dispatch with fire tracking, coalesced triggers with HEAD commit context
- **Rich Queue UI** — upcoming/past filtering, editable context, live streaming, stored output as markdown, cancel confirmation, resume/retry actions, diff view, run-now for individual pending items
- **Task config** — two-column layout, markdown preview, auto-resize textareas, subscription file preview, dispatch spinner with animation
- **Settings view** — queue behavior, default model, default timeout, MCP server management, project info
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Clean route architecture** — `task_routes.py`, `queue_routes.py`, `chat.py` as separate routers; `state.py` for shared mutable state; Pydantic request models throughout

## What Matters Now

The platform is feature-complete for single-agent workflows. Every backend capability is reachable through UI. The code is modular and maintainable. The remaining gap is **multi-agent coordination** — the system runs tasks independently but has no declarative way to compose them into workflows.

The next phase is about turning "a collection of tasks" into "a coordinated system":

1. **Composition** — declarative task relationships that create workflows
2. **Safety** — approval gates that make unattended automation trustworthy
3. **Visibility** — observability into what agents are doing across the system

## Priority 1: Task Dependencies and Workflows

**The problem:** Task-to-task dispatch works ad-hoc (a running task can queue another via context), but there's no declarative way to say "Architect runs after Strategist." Workflows depend on what individual tasks remember to do, not what the system enforces.

**Why first:** This is the feature that makes mAistro qualitatively different from running CLI agents manually. Dependencies turn a "collection of tasks" into a "coordinated workflow." With settings complete, this is the next capability that changes what the system *is*, not just how it looks.

**Specifically:**
- Add optional `depends_on` task property (list of task IDs)
- When a task's dispatch completes successfully, auto-enqueue tasks that declare it as a dependency
- Pass the completing dispatch's summary downstream as context
- Queue UI shows dependency chain relationships
- Compose with existing triggers: a task with `depends_on: ["strategist"]` and `watch_enabled: true` fires on either condition, with coalescing handling overlap

**Design consideration:** Dependencies should be a new trigger type (`dependency`), not a replacement for watch or schedule. The enqueue happens in `_process_dispatch` after successful completion, checking `depends_on` across all tasks. This keeps the trigger model uniform.

## Priority 2: Approval Gates

**The problem:** As mAistro becomes more autonomous (watch + schedule + dependencies), there's no way to require human review before a dispatch executes. A watch-triggered dispatch on a sensitive task could make unwanted changes with no checkpoint.

**Specifically:**
- Add optional `require_approval` boolean task property (default: false)
- Approval-gated dispatches enter a `pending_approval` status instead of being immediately processable
- Queue UI shows approval-pending items with approve/reject actions
- Approved dispatches proceed normally; rejected dispatches are marked as skipped
- Manual dispatches bypass approval (you already chose to run it)

**Why this matters:** This is the safety valve that makes unattended operation trustworthy. Without it, increasing automation means increasing risk. With it, you can run watch + schedule + dependencies on everything and still maintain control over what actually executes.

## Priority 3: Dispatch Observability

**The problem:** The system runs tasks and stores output, but there's no aggregated view of what happened across tasks over time. You can see individual dispatch output in Queue, but there's no way to answer "what did my agents accomplish today?" or "which tasks are failing most often?"

**Specifically:**
- **Activity summary** — a dashboard or feed entry that aggregates dispatch results across tasks (success/fail counts, last run times, total commits)
- **Dispatch timeline** — visual timeline showing when dispatches ran, how long they took, and whether they overlapped with each other
- **Error patterns** — surface recurring failures (same task timing out, same error message) so the user can adjust configuration

**Why third:** The system works without this, but as dispatch volume grows (especially with dependencies and scheduling), the user needs a way to stay oriented. This is about making the autonomous system *legible*.

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

- **Settings and Configuration UI** (was P1) — Settings view with queue behavior toggle, default model selector, default timeout input, MCP server management (list/add/remove), and project info display. Accessible from rail navigation. Shipped in `afc7f5c`.
- **Route Modularization** — Task routes extracted to `task_routes.py`, dispatch/queue routes in `queue_routes.py`, chat routes in `chat.py`. Shared mutable state extracted to `state.py` to eliminate circular imports. Pydantic request models throughout. Shipped across `b35b3cc`, `b18ea99`, `c9fd29b`, `86c7ab6`.
- **Dispatch Diff View** — `start_commit` recorded when dispatch begins, `result_commit` on completion. Queue detail panel shows collapsible diff section with file-level summary and per-file expandable diffs. Shipped in `def9824`.
- **Dispatch Continuity — Resume and Retry** — Resume reuses CLI session via `--resume`, retry re-enqueues with original context. New trigger types `resume` and `retry`. Queue detail panel shows Resume/Retry buttons. Shipped in `fdd357b`.
- **Dispatch Timeout Enforcement** — configurable per-task `timeout` property, watchdog in worker, graceful terminate + kill, partial output preserved. Shipped across `52d1722` through `d0e4426`.
