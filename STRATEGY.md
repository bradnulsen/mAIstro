# Strategy

## Current State

mAistro is a working, self-sustaining orchestration platform. The core loop is closed, observable, and now **robust**: tasks configure, queue, dispatch via Claude CLI, stream output in real-time, enforce timeouts, and commit results to git. Watch, schedule, and task-to-task triggers fire automatically. Live SSE streaming gives full visibility into running dispatches.

What's in place:
- **Queue-first dispatch** — background worker, auto/manual processing, stale sweep, cancellation flag
- **Live dispatch streaming** — SSE endpoint with real-time text, tool use, and thinking indicators in the Queue detail panel
- **Timeout enforcement** — configurable per-task timeout with watchdog, graceful terminate then kill, partial output preserved
- **Watch mode** — post-commit hook, glob subscription matching, per-task toggle, cooldown
- **Task-to-task dispatch** — `task_queue` trigger with context passing and upstream summary injection
- **Cron scheduling** — timed dispatch with fire tracking that survives restarts
- **Coalesced dispatches** — multiple triggers merge into one queue entry
- **Rich Queue UI** — upcoming/past filtering, editable context, live streaming, stored output as markdown
- **Task config** — two-column layout, markdown preview, auto-resize textareas, subscription file preview
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Persistent DB connection** — pooled async SQLite, eliminates connection-per-call overhead

## What Matters Now

The platform is observable and stable. Dispatches run, stream, timeout, and cancel correctly. The next phase is about **resilience** (recovering from failures without losing progress), **visibility** (seeing what agents actually did to the code), and **control** (exposing existing configuration through UI).

The overarching theme: **make mAistro trustworthy enough to leave running unattended.** That requires knowing dispatches won't silently fail without recourse, being able to see exactly what changed, and configuring behavior without touching the API.

## Priority 1: Dispatch Continuity — Resume and Retry

**The problem:** If a dispatch fails partway through — tool error, context limit, timeout — the only option is re-dispatching from scratch. This wastes all progress. Claude CLI supports `--resume` with a session ID, and dispatch records already store session IDs.

**Why first:** This is the biggest reliability gap. A single timeout or transient failure shouldn't negate 10 minutes of agent work. The infrastructure is 80% ready — CLI supports `--resume`, database has session IDs, chat sessions link to dispatches. The remaining work is wiring.

**Specifically:**
- "Resume" action on failed/timed-out dispatches: creates a new queue entry that invokes CLI with `--resume` and the stored session ID
- "Retry" action: creates a fresh dispatch with the same trigger context
- Queue detail panel shows both actions on completed/failed dispatches
- Resume dispatch linked to the same chat session; retry creates a new session

**Implementation path:** Add `resume_session_id` column to `dispatch_queue`. When present, `run_dispatch` passes `--resume <session_id>` to the CLI. The worker creates a new queue entry but reuses the chat session. Frontend adds Resume/Retry buttons to the dispatch detail panel for non-pending dispatches.

## Priority 2: Dispatch Diff View

**The problem:** When a dispatch completes, you see the agent's text output but not the actual code changes. To see what changed, you have to switch to the Feed view and find the right commits. This breaks the feedback loop — the most important thing about a dispatch is what it did to the code.

**Why before settings:** This is about the core product experience. The diff view is what turns mAistro from "a thing that runs agents" into "a thing that shows you what agents did." Settings UI is housekeeping; diff view is the product.

**Specifically:**
- Queue detail panel shows a collapsible diff section for completed dispatches with a `result_commit`
- Diff computed between the commit before dispatch started and `result_commit` (may span multiple commits)
- File-level summary (files changed, insertions, deletions) with expandable per-file diffs
- Reuse the git diff infrastructure already in `backend/git.py`

## Priority 3: Settings and Configuration UI

**The problem:** MCP server management, model defaults, and project-level config all have working backend routes but no UI. The settings button in the rail is a dead end. Users must know the API to configure these things.

**Specifically:**
- Settings view accessible from the rail with sections for:
  - Queue behavior (auto-dispatch toggle — already in Queue header, but should also be in Settings)
  - Default model selection
  - MCP server management (add/remove/edit server configs)
  - Default timeout
  - Project path display
- Single scrollable page with sections, not tabs

## Priority 4: Task Dependencies and Workflows

**The problem:** Task-to-task dispatch works ad-hoc (a running task can queue another via context), but there's no declarative way to say "Architect runs after Strategist." This limits workflows to what individual tasks remember to do, rather than what the system enforces.

**Specifically:**
- Add optional `depends_on` task property (list of task IDs)
- When a task's dispatch completes successfully, auto-enqueue tasks that declare it as a dependency
- Pass the completing dispatch's summary downstream as context
- Queue UI shows dependency chain relationships
- Compose with existing triggers: a task with `depends_on: ["strategist"]` and `watch_enabled: true` fires on either condition, with coalescing handling overlap

**Design consideration:** Dependencies should be a new trigger type (`dependency`), not a replacement for watch or schedule. The enqueue happens in `_process_dispatch` after successful completion, checking `depends_on` across all tasks. This keeps the trigger model uniform.

## Priority 5: Approval Gates

**The problem:** As mAistro becomes more autonomous (watch + schedule + dependencies), there's no way to require human review before a dispatch executes. A watch-triggered dispatch on a sensitive task could make unwanted changes with no checkpoint.

**Specifically:**
- Add optional `require_approval` boolean task property (default: false)
- Approved-gated dispatches enter a `pending_approval` status instead of being immediately processable
- Queue UI shows approval-pending items with approve/reject actions
- Approved dispatches proceed normally; rejected dispatches are marked as skipped
- Manual dispatches bypass approval (you already chose to run it)

**Why this matters:** This is the safety valve that makes unattended operation trustworthy. Without it, increasing automation means increasing risk. With it, you can run watch + schedule on everything and still maintain control over what actually executes.

## Deferred

Valuable but not blocking the current phase:

- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and the global `PROJECT_DIR` state.
- **Parallel dispatch** — concurrent dispatch execution. Valuable for independent tasks but introduces git conflict complexity. Sequential is correct until it's the bottleneck.
- **Cost tracking** — token usage and API costs per dispatch. Claude CLI doesn't expose this cleanly yet.
- **Route modularization** — `main.py` is the largest file (550+ lines). Breaking out dispatch, queue, task, and git routes into separate routers would improve maintainability. Not urgent at current scale.
- **Structured error types** — dispatch errors are strings. An error classification system (timeout, cancelled, context_limit, tool_error, unknown) would improve observability and enable smarter retry logic.
- **Dark mode** — the monospace aesthetic works. Nice-to-have.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. Building a custom LLM layer is massive effort for marginal gain.

## Completed

- **Dispatch Timeout Enforcement** (was P1) — configurable per-task `timeout` property via EAV system, watchdog in worker enforces it, graceful terminate + kill after 5s grace period, partial output preserved with "timed out" indicator. Shipped across commits `52d1722` through `d0e4426`.
