# Strategy

## Current State

mAistro's core loop works: tasks configured in the UI get dispatched, Claude CLI runs headlessly, commits results, and the feed shows what happened. Chat works with session persistence. The architect has cleanly separated concerns (git.py, cli.py, appstate.py, dispatch.py). The frontend has three views — Feed, Tasks, Queue — plus a slide-out chat tray.

What exists today is a functional but minimal orchestration UI. The gap between "works" and "useful" is where the next work should focus.

## Priority 1: Task-to-Task Dispatch

**Why:** The entire value proposition of mAistro is coordinated multi-agent work. Right now tasks run in isolation. The spec describes `queue_agent` as an MCP tool, but since we invoke Claude CLI (not a custom runtime), the practical path is simpler: give tasks the ability to queue other tasks via a CLI tool or by writing to a well-known file.

**Approach:** Add a `/api/dispatch/queue` POST endpoint that tasks can call via `Bash` tool (curl). Inject the endpoint URL and available task IDs into the dispatch system prompt. No MCP server needed — the CLI already has Bash access.

**Deliverable:** A task can write `curl -X POST localhost:8420/api/dispatch/queue -d '{"task_id":"architect","context":"..."}'` and the target task runs with that context. The dispatch prompt already includes a task registry; it just needs the instruction and endpoint.

## Priority 2: Watch Mode (Commit Triggers)

**Why:** Manual dispatch is useful for development but the real power is reactive automation. The backend has `check_watch_triggers` and the post-commit hook installer, but the hook endpoint (`/api/hooks/post-commit`) is missing from `main.py`.

**Deliverable:** Wire the post-commit hook endpoint. When a commit lands, match changed files against watch-mode task subscriptions, respect cooldowns, and auto-enqueue. This makes the system self-sustaining — human or task commits trigger downstream work.

## Priority 3: Dispatch Stream Visibility

**Why:** When a task runs, the only feedback is the final commit in the feed. The user can't see what's happening during execution. The SSE stream exists but the Tasks view shows a basic text dump. The Queue view shows status but no live output.

**Deliverable:** Route live dispatch SSE output to the Queue view for running dispatches. Show tool calls, text output, and progress in real-time. This is critical for trust — users need to see what autonomous tasks are doing.

## Priority 4: Error Recovery and Robustness

**Why:** Several failure modes are unhandled:
- If the backend crashes mid-dispatch, `running` stays true forever (task is stuck)
- No timeout on CLI invocations — a hung process blocks indefinitely
- The `cancel` endpoint updates the DB but doesn't kill the subprocess
- No retry mechanism for failed dispatches

**Deliverable:** Add a startup sweep that resets stale `running` flags. Add a configurable timeout to CLI invocation. Make cancel actually kill the subprocess (requires tracking PIDs). These are table stakes for leaving the system running unattended.

## Priority 5: Settings View

**Why:** The settings rail icon exists but does nothing. Config management (MCP servers, global model defaults, project settings) has no UI surface despite having backend support.

**Deliverable:** A settings view with sections for MCP server management, default model selection, and project-level config. Low effort, high polish.

## Deferred

These are valuable but not blocking:

- **Auto mode polling loop** — Useful for periodic tasks, but watch mode covers the primary use case. Implement after watch mode proves out.
- **Authored files view** — The spec describes showing files a task has committed to (via git log --author). Nice for understanding task output, but the feed already provides this.
- **Inline file editor** — The spec wants human edits through the UI. Useful eventually but most users will edit in their IDE and commit normally.
- **Chat-per-task** — Currently chat is a global assistant. Scoping chat to a specific task context would be more powerful but the current implementation works.
- **Dark mode** — The monospace aesthetic is clean. Dark mode is a nice-to-have.

## Non-Goals

- **TypeScript migration** — The frontend is small and React 19 JSX is fine. Type safety isn't the bottleneck.
- **State management library** — The app's state is simple enough for useState/useEffect. Don't add Redux/Zustand until there's a real problem.
- **Test suite** — The codebase is small and changing fast. Tests would slow iteration without proportional value at this stage.
- **Electron/Tauri packaging** — The Vite dev server + Python backend works fine for the target user (developers). Desktop packaging is a distribution concern, not a capability concern.
