# Strategy

## Current State

mAistro is a working, self-sustaining orchestration platform. The core loop is closed and observable: tasks configure, queue, dispatch via Claude CLI, stream output in real-time, and commit results to git. Watch, schedule, and task-to-task triggers fire automatically. Live SSE streaming gives full visibility into running dispatches.

What's in place:
- **Queue-first dispatch** — background worker, auto/manual processing, stale sweep, cancellation flag
- **Live dispatch streaming** — SSE endpoint with real-time text, tool use, and thinking indicators in the Queue detail panel
- **Watch mode** — post-commit hook, glob subscription matching, per-task toggle, cooldown
- **Task-to-task dispatch** — `task_queue` trigger with context passing and upstream summary injection
- **Cron scheduling** — timed dispatch with fire tracking that survives restarts
- **Coalesced dispatches** — multiple triggers merge into one queue entry
- **Rich Queue UI** — upcoming/past filtering, editable context, live streaming, stored output as markdown
- **Task config** — two-column layout, markdown preview, auto-resize textareas, subscription file preview
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Clean module structure** — chat, state, dispatch, worker, scheduler, cli all in focused modules

## What Matters Now

The platform is observable but not yet controllable. You can see what's happening, but you can't reliably stop a runaway dispatch, recover from a failure, or configure the system without hitting the API directly. The next phase is about **control** (stopping what shouldn't happen), **continuity** (recovering from failures), and **configuration** (exposing what's already built).

## Priority 1: Process Control — Kill and Timeout

**The problem:** Cancel sets an event flag but doesn't reliably kill the Claude CLI subprocess. A hung or runaway dispatch blocks the entire queue indefinitely. There's no timeout. Without these, one bad dispatch can halt all autonomous work.

**Specifically:**
- Store the `Popen` object in the worker (not just the PID); cancel calls `process.terminate()` then `process.kill()` after a grace period
- Add configurable per-task `timeout` property (default: 15 minutes)
- Worker enforces timeout — marks timed-out dispatches as errors with partial output preserved
- Stale dispatch sweep on startup should attempt to kill orphaned subprocesses (best-effort on Windows)

**Design note:** On Windows, `SIGTERM` doesn't work. Use `process.terminate()` / `process.kill()` which map to `TerminateProcess`. Store the `Popen` object on the worker module, not the PID. The cancel flow: set event → terminate → wait 5s → kill.

**Known bug to fix:** `scheduler.py:83` checks `task["properties"].get("running")` — this property was removed. Should check for active/pending dispatches via the database instead (e.g., `db.has_pending_dispatch(task_id)` or checking `worker.get_active_dispatch_id()` against the task's dispatches).

## Priority 2: Dispatch Continuity — Resume and Retry

**The problem:** If a dispatch fails partway through — tool error, context limit, timeout — the only option is re-dispatching from scratch. This wastes all progress. Claude CLI supports `--resume` with a session ID, and dispatch records already store session IDs.

**Specifically:**
- "Resume" action on failed/timed-out dispatches: creates a new queue entry that invokes CLI with `--resume` and the stored session ID
- "Retry" action: creates a fresh dispatch with the same trigger context
- Queue detail panel shows both actions on completed/failed dispatches
- Resume dispatch linked to the same chat session; retry creates a new session

**Implementation path:** Add `resume_session_id` column to `dispatch_queue`. When present, `run_dispatch` passes `--resume <session_id>` to the CLI. The worker creates a new queue entry but reuses the chat session. Frontend adds Resume/Retry buttons to the dispatch detail panel for non-pending dispatches.

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

**Why before dependencies:** Settings UI unlocks existing backend capabilities with zero new architecture. Task dependencies require schema changes and new dispatch logic.

## Priority 4: Task Dependencies and Workflows

**The problem:** Task-to-task dispatch works ad-hoc (a running task can queue another via context), but there's no declarative way to say "Architect runs after Strategist." This limits workflows to what individual tasks remember to do, rather than what the system enforces.

**Specifically:**
- Add optional `depends_on` task property (list of task IDs)
- When a task's dispatch completes successfully, auto-enqueue tasks that declare it as a dependency
- Pass the completing dispatch's summary downstream as context
- Queue UI shows dependency chain relationships
- Compose with existing triggers: a task with `depends_on: ["strategist"]` and `watch_enabled: true` fires on either condition, with coalescing handling overlap

**Design consideration:** Dependencies should be a new trigger type (`dependency`), not a replacement for watch or schedule. The enqueue happens in `_process_dispatch` after successful completion, checking `depends_on` across all tasks. This keeps the trigger model uniform.

## Priority 5: Dispatch Diff View

**The problem:** When a dispatch completes, you see the agent's text output but not the actual code changes. To see what changed, you have to switch to the Feed view and find the right commits. This breaks the feedback loop — the most important thing about a dispatch is what it did to the code.

**Specifically:**
- Queue detail panel shows a collapsible diff section for completed dispatches with a `result_commit`
- Diff computed between the commit before dispatch started and `result_commit` (may span multiple commits)
- File-level summary (files changed, insertions, deletions) with expandable per-file diffs
- Reuse the git diff infrastructure already in `backend/git.py`

## Deferred

Valuable but not blocking the current phase:

- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation.
- **Parallel dispatch** — concurrent dispatch execution. Valuable for independent tasks but introduces git conflict complexity. Sequential is correct until it's the bottleneck.
- **Cost tracking** — token usage and API costs per dispatch. Claude CLI doesn't expose this cleanly yet.
- **Dark mode** — the monospace aesthetic works. Nice-to-have.
- **Inline file editor** — most users have their IDE open alongside.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. Building a custom LLM layer is massive effort for marginal gain.
