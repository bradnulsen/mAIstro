# Strategy

## Current State

mAistro is a working orchestration platform with a clean, well-factored codebase. The core loop is closed: tasks get configured, queued, dispatched via Claude CLI, and results committed to git. Downstream tasks trigger automatically via watch or schedule. The architecture is sound — queue-first dispatch, sequential processing, git as source of truth.

What's in place:
- **Queue-first dispatch** — background worker with auto/manual processing, stale sweep on startup, cancellation flag
- **Watch mode** — post-commit hook triggers tasks based on subscription glob matching, per-task toggle, cooldown
- **Task-to-task dispatch** — `task_queue` trigger with context passing and handoff awareness in the prompt
- **Cron scheduling** — tasks can run on a cron schedule with fire tracking that survives restarts
- **Coalesced dispatches** — multiple triggers for the same task merge into one queue entry
- **Rich Queue UI** — dispatch output rendered as markdown, detail panel with scroll, status polling
- **Polished task config** — two-column layout, markdown preview, auto-resize textareas, subscription toggles
- **Chat with session persistence** — SSE streaming, session resume, dispatch-linked audit trail
- **Git feed** — live polling, commit diffs with file/line stats
- **Clean module structure** — chat, state, dispatch, worker, scheduler all extracted into focused modules

The coordination loop is self-sustaining. The codebase is ready for the next phase of work.

## What Matters Now

The platform works but you can't trust it unsupervised. The next phase is about **observability** (seeing what's happening), **control** (stopping what shouldn't happen), and **continuity** (recovering from failures). These three capabilities are the gate to autonomous operation.

## Priority 1: Dispatch Observability — Live Streaming

**The problem:** Running dispatches are black boxes. The Queue shows "Running" with no visibility into what the agent is doing. Users can't tell if a task is productive, stuck, or going sideways until it finishes. This is the single biggest barrier to leaving mAistro running unattended.

**The solution:** Stream dispatch events to the frontend in real-time via SSE. The worker already yields events from `run_dispatch` and the chat system already proves the SSE pattern works end-to-end. The missing piece is a live endpoint that the Queue detail panel subscribes to for running dispatches.

**Specifically:**
- Add `/api/dispatch/{id}/stream` SSE endpoint that taps into the worker's event stream for the active dispatch
- Queue detail panel subscribes to this endpoint when viewing a running dispatch
- Show text output, tool calls (file reads/writes), and thinking indicators as they happen
- On completion, seamlessly transition to the stored session output
- Show elapsed time on running dispatches

**Why first:** Everything else (kill, timeout, retry) is less useful without being able to see what's happening. Observability informs all other decisions.

## Priority 2: Process Control — Kill and Timeout

**The problem:** Cancel sets an event flag but doesn't kill the Claude CLI subprocess. A hung dispatch blocks the entire queue indefinitely. There's no timeout. These are prerequisites for unattended operation — without them, one bad dispatch can halt all work.

**Specifically:**
- Track CLI subprocess PID in the worker; cancel sends SIGTERM then SIGKILL after grace period
- Add configurable per-task timeout as a task property (default: 15 minutes)
- Timed-out dispatches marked as errors with partial output preserved
- Stale dispatch sweep on startup should also kill orphaned subprocesses (best-effort on Windows)

**Design note:** On Windows, `SIGTERM` doesn't work the same way. Use `process.terminate()` / `process.kill()` which map to `TerminateProcess`. The worker should store the `Popen` object, not just the PID.

## Priority 3: Dispatch Continuity — Resume and Retry

**The problem:** If a dispatch fails partway through — tool error, context limit, timeout — the only option is re-dispatching from scratch. This loses all progress and context. Claude CLI supports `--resume` with a session ID, and the worker already stores session IDs per dispatch.

**Specifically:**
- "Resume" action on failed/completed dispatches: re-invokes CLI with `--resume` and the stored session ID, continuing where the agent left off
- "Retry" action: creates a fresh dispatch with the same trigger context
- Queue UI shows both actions on completed/failed dispatches
- Resume creates a new queue entry linked to the same chat session

## Priority 4: Settings and Configuration UI

**The problem:** MCP server management, model defaults, and project-level config have backend support but no UI. The settings button in the rail is a dead end. Users must know the API to configure these.

**Specifically:**
- Settings view accessible from the rail with sections for:
  - Queue behavior (auto-dispatch toggle, default timeout)
  - Default model selection
  - MCP server management (add/remove/edit)
  - Project path display and recent projects
- Keep it simple — a single scrollable page with sections, not a tabbed interface

**Why before dependencies:** Settings UI unlocks existing backend capabilities with minimal new code. Task dependencies require new architecture. Ship the easy win first.

## Priority 5: Task Dependencies and Workflows

**The problem:** Task-to-task dispatch works via curl from within a running task, but there's no declarative way to express "Architect always runs after Strategist." This limits the platform to ad-hoc chains rather than repeatable workflows.

**Specifically:**
- Add optional `depends_on` task property — a list of task IDs
- When a task completes successfully, auto-enqueue any tasks that declare it as a dependency
- Pass the completing dispatch's context downstream
- Queue UI shows chain relationships
- Watch triggers and dependency triggers coexist: a task can trigger on both file changes and upstream completion

**Design consideration:** This should compose with, not replace, the existing watch and schedule triggers. A task with `depends_on: ["strategist"]` and `watch_enabled: true` fires on either condition. Deduplication via coalescing handles the overlap.

## Deferred

Valuable but not blocking the current phase:

- **Multi-project orchestration** — cross-project dispatch from one mAistro instance. App DB tracks recent projects; cross-project would need careful design around DB isolation.
- **Parallel dispatch** — running multiple dispatches concurrently. Valuable for independent tasks but introduces git conflict complexity. Sequential is correct until it's demonstrably the bottleneck.
- **Inline file editor** — editing project files through the UI. Most users have their IDE open alongside.
- **Cost tracking** — token usage and API costs per dispatch. Useful for budgeting but Claude CLI doesn't expose this cleanly yet.
- **Dark mode** — the monospace aesthetic is clean. Nice-to-have.
- **Dispatch diff view** — showing the git diff produced by a dispatch directly in the Queue detail panel, rather than requiring users to check the Feed. Would close the feedback loop.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient. Don't add complexity until there's a real problem.
- **Test suite** — the codebase is small and changing fast. Tests would slow iteration without proportional value at this stage.
- **Electron/Tauri packaging** — Vite + Python works for the target user. Distribution is a later concern.
- **Custom agent runtime** — Claude CLI subprocess model works. Building a custom LLM invocation layer would be massive engineering for marginal gain.
