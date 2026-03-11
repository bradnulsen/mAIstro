# Strategy

## Current State

mAistro has crossed from "functional prototype" to "working orchestration platform." The core dispatch loop is solid: tasks get configured, queued, dispatched via Claude CLI, and results are committed to git. The queue-first architecture works — manual, watch, and task-to-task triggers all funnel through `dispatch_queue`, a background worker processes them sequentially, and the UI shows live status and stored output.

What's been delivered since the last strategy:
- **Queue-first dispatch** is the backbone. Background worker with auto/manual processing, stale sweep on startup, cancellation support.
- **Watch mode** works end-to-end. Post-commit hook triggers tasks based on subscription glob matching. Auto-queue toggles per-task.
- **Task-to-task dispatch** via `task_queue` trigger type with context passing and handoff awareness in the prompt.
- **Coalesced dispatches** — multiple triggers for the same task merge into one queue entry.
- **Rich Queue UI** — dispatch output rendered as markdown, detail panel with scroll, status polling, error display.
- **Polished task config** — two-column layout, markdown preview for instructions, auto-resize textareas, inline subscription toggles.
- **Chat with session persistence** — each dispatch creates a linked chat session as an audit trail.
- **Feed view** with live git activity polling.

The system is now self-sustaining: a human or task commit can trigger downstream tasks, which commit their own changes, which trigger further tasks. The coordination loop is closed.

## What Matters Now

The platform works. The next phase is about making it **trustworthy enough to leave running** and **expressive enough for real workflows**. The priorities below reflect this shift from building infrastructure to building confidence.

## Priority 1: Dispatch Observability

**Why:** The biggest barrier to leaving mAistro running autonomously is not knowing what it's doing. The Queue shows stored output after completion, but there's no live streaming view during execution. Users can't see tool calls happening in real-time, can't judge whether a task is productive or stuck, and can't intervene intelligently.

**Approach:** Stream dispatch events to the frontend in real-time via SSE. The worker already yields events from `run_dispatch`; the missing piece is a live endpoint that the Queue detail panel subscribes to when a dispatch is running.

**Deliverable:** When a dispatch is in-progress, the Queue detail view shows a live stream of text output and tool calls. The user sees what the agent is reading, writing, and thinking as it happens. After completion, the stored session output replaces the stream seamlessly.

## Priority 2: Process Control — Timeouts and Kill

**Why:** Cancel sets an event flag but doesn't kill the Claude CLI subprocess. A hung or runaway dispatch blocks the entire queue indefinitely. There's no timeout mechanism. These are prerequisites for unattended operation.

**Deliverable:**
- CLI subprocess PID tracked by the worker; cancel sends SIGTERM/SIGKILL
- Configurable per-task timeout (default: 15 minutes) as a task property
- Timed-out dispatches marked as errors with partial output preserved
- Queue UI shows elapsed time on running dispatches

## Priority 3: Dispatch Continuity (Resume/Retry)

**Why:** Tasks frequently need multiple turns to complete complex work, especially when they hit tool errors or context limits. Currently, each dispatch is a one-shot invocation. If a dispatch fails partway through, the only option is to re-dispatch from scratch — losing all progress and context.

**Approach:** Claude CLI supports `--resume` with a session ID. The worker already stores session IDs per dispatch. Add a "resume" action on failed/completed dispatches that re-invokes with the same session, letting the agent continue where it left off. Separately, add a "retry" that re-dispatches with the same context but a fresh session.

**Deliverable:** Queue UI shows "Resume" and "Retry" actions on completed/failed dispatches. Resume continues the CLI session; retry creates a fresh dispatch with the same trigger context.

## Priority 4: Task Dependencies and Workflows

**Why:** Task-to-task dispatch works via curl from within a running task, but there's no declarative way to express "Architect always runs after Strategist" or "Frontend runs after Architect on the same trigger." This limits mAistro to ad-hoc chains rather than repeatable workflows.

**Approach:** Add optional `depends_on` task property — a list of task IDs. When a task completes, auto-enqueue any tasks that list it in `depends_on` (with the completing dispatch's commit as context). This is simpler and more predictable than having agents curl endpoints.

**Deliverable:** Tasks can declare dependencies. Completion of an upstream task auto-enqueues downstream dependents. The Queue shows the chain relationship. Watch triggers and dependency triggers coexist cleanly.

## Priority 5: Settings and Configuration UI

**Why:** MCP server management, model defaults, and queue settings have backend support but no UI. Users must know the API to configure these. A settings view turns hidden capabilities into accessible ones.

**Deliverable:** A settings view accessible from the rail with sections for:
- Queue behavior (auto-dispatch toggle, default timeout)
- Default model selection
- MCP server management (add/remove/test)
- Project-level configuration

## Deferred

These are valuable but not blocking the current phase:

- **Multi-project orchestration** — Running tasks across multiple project directories from one mAistro instance. The app DB already tracks recent projects, but cross-project dispatch would need careful design.
- **Parallel dispatch** — Running multiple dispatches concurrently instead of sequentially. Valuable for independent tasks but introduces git conflict complexity. Sequential is correct for now.
- **Inline file editor** — Editing project files through the UI. Most users will use their IDE; the feed and queue views provide sufficient visibility into changes.
- **Cost tracking** — Tracking token usage and API costs per dispatch. Useful for budgeting but Claude CLI doesn't expose this cleanly yet.
- **Dark mode** — The monospace aesthetic is clean. Dark mode is a nice-to-have.

## Non-Goals

- **TypeScript migration** — The frontend is small and React 19 JSX is fine. Type safety isn't the bottleneck.
- **State management library** — The app's state is simple enough for useState/useEffect. Don't add Redux/Zustand until there's a real problem.
- **Test suite** — The codebase is small and changing fast. Tests would slow iteration without proportional value at this stage.
- **Electron/Tauri packaging** — The Vite dev server + Python backend works fine for the target user (developers). Desktop packaging is a distribution concern, not a capability concern.
- **Custom agent runtime** — The Claude CLI subprocess model works. Building a custom LLM invocation layer would be massive engineering for marginal gain at this stage.
