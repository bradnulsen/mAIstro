# Strategy

## Current State

mAistro is a fully operational multi-agent orchestration platform with a complete trigger and coordination layer. Five trigger types — manual, commit (watch), schedule (cron), task_queue, and dependency — create a flexible automation system where tasks can be composed into workflows declaratively. The codebase is cleanly modularized, the UI exposes all backend capabilities, and the dispatch pipeline is resilient.

What's in place:
- **Queue-first dispatch** — background worker, auto/manual processing, stale sweep, cancellation
- **Five trigger types** — manual, commit (watch via subscriptions), schedule (cron), task_queue (ad-hoc from running tasks), dependency (declarative `depends_on`)
- **Dispatch continuity** — resume (CLI `--resume`) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — commit triggers always coalesce; schedule always coalesces globally; `coalesce_dispatches` coalesces all trigger types; dependency coalesces same-type
- **Task config** — tabbed UI (definition/triggers), dependency checkboxes with cycle prevention, subscription file preview, markdown preview
- **Settings** — queue behavior, default model, default timeout, MCP server management
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Clean architecture** — extracted route modules, shared state layer, Pydantic models, persistent DB connection

## What Matters Now

The coordination layer is functionally complete. The system can compose tasks into declarative workflows via dependencies, trigger them from multiple sources, and process them reliably. The remaining gaps are about **trust, visibility, and throughput**:

1. **Trust** — automatic dispatches run without human checkpoint. As automation scales, this becomes the limiting factor.
2. **Visibility** — understanding what agents accomplished requires clicking through individual dispatches. No aggregated view.
3. **Throughput** — sequential processing bottlenecks dependency chains. Independent tasks wait unnecessarily.

## Priority 1: Approval Gates

**The problem:** With five trigger types creating automatic dispatches, the only safety control is the global `auto_dispatch` toggle — all-or-nothing. There's no way to say "auto-process watch triggers but require approval for dependency chains" or "let Scribe run unattended but gate Architect."

**Why first:** This is the feature that lets users safely increase automation. Without it, scaling up triggers means scaling up risk. With it, users can enable aggressive automation on low-risk tasks while keeping human oversight on high-stakes ones.

**Specifically:**
- Add `require_approval` boolean task property (default: false)
- Approval-gated dispatches enter a `pending_approval` status — queued but not processable until approved
- Queue UI shows approval-pending items with approve/reject actions
- Approved dispatches become normal pending items; rejected dispatches are marked as skipped
- Manual dispatches bypass approval (explicit intent already expressed)
- Approval status is a dispatch_queue column, not a separate table — keeps the model simple

**Design consideration:** The approval check belongs in the worker loop, not in the enqueue path. Enqueue always succeeds (preserving the uniform queue-first model). The worker skips `pending_approval` items when pulling the next dispatch. This means the queue shows everything — approved, pending approval, and processing — giving full visibility into the pipeline.

## Priority 2: Activity Dashboard

**The problem:** The Queue view shows individual dispatches. The Feed shows individual commits. Neither answers "what did my agents accomplish today?" or "which tasks are failing?" As dispatch volume grows with dependencies and scheduling, users lose the thread.

**Why second:** Observability is what makes autonomous operation sustainable. Without it, users stop trusting the system — not because it's broken, but because they can't tell if it's working.

**Specifically:**
- **Summary panel** on the Queue view (or a new Dashboard view): success/fail/timeout counts per task over configurable time windows (today, 7d, 30d)
- **Task health indicators** — surface recurring failures (same task timing out repeatedly, same error pattern)
- **Timeline view** — when dispatches ran, how long they took, gaps between runs. Simple horizontal bars, not a complex chart library
- Data is already in `dispatch_queue` — this is a read-only aggregation, no schema changes needed

## Priority 3: Parallel Dispatch

**The problem:** The worker processes one dispatch at a time. With dependency chains, independent branches of the workflow wait in line behind each other. Two tasks that both depend on Strategist must run sequentially even though they have no relationship to each other.

**Why third:** Sequential processing was the right starting point — it eliminates git conflicts and simplifies the mental model. But as workflows grow, it becomes the throughput bottleneck. The mitigation: parallel dispatch with git worktree isolation.

**Specifically:**
- Allow N concurrent dispatches (configurable, default: 1 to preserve current behavior)
- Each concurrent dispatch operates in a git worktree, isolating file changes
- On completion, the worktree's branch is merged back to the main branch
- Conflict detection: if a merge has conflicts, mark the dispatch as needing manual resolution
- Worker becomes a pool: N slots, each can run one dispatch independently
- Worktree lifecycle is managed by the worker — create on start, merge + clean up on completion

**Design consideration:** This is the hardest feature on the roadmap. Git worktrees add real complexity — merge conflicts, branch management, cleanup on failure. The tracer bullet should be: two concurrent dispatches in separate worktrees, auto-merge on clean completion, error on conflict. Fancy conflict resolution comes later.

## Deferred

Valuable but not blocking the current phase:

- **Conditional dependencies** — "only run if upstream output matches X." Useful for branching workflows but adds significant complexity to the trigger model. Wait until linear dependency chains prove insufficient.
- **Richer dependency context** — upstream dispatch output summary injected into downstream context. Currently dependencies pass only the trigger type and upstream task ID. Enriching this requires reading the upstream dispatch's stored output and summarizing or truncating it.
- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and global `PROJECT_DIR` state.
- **Cost tracking** — token usage per dispatch. Claude CLI doesn't expose this cleanly yet.
- **Structured error types** — error classification (timeout, cancelled, context_limit, tool_error) would improve observability and enable smarter retry. Wait for the activity dashboard to surface which error patterns matter.
- **Status bar click-through** — task chips navigate to the running dispatch in Queue. Small UX win.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. Building a custom LLM layer is massive effort for marginal gain.

## Completed

- **Task Dependencies** (was P1) — `depends_on` JSON property, `dependency` trigger type with same-type coalescing, auto-enqueue on upstream completion, circular dependency prevention, frontend checkboxes in Triggers tab. Shipped in `43d1ac7` through `429259e`.
- **Watch Semantics Cleanup** — removed `watch_enabled` toggle; subscriptions presence = watch active. Commit coalescing is now automatic (no longer requires `coalesce_dispatches`). Shipped in `8727171`.
- **Settings and Configuration UI** — queue behavior, default model, default timeout, MCP server management. Shipped in `afc7f5c`.
- **Route Modularization** — `task_routes.py`, `queue_routes.py`, `chat.py` as separate routers; `state.py` for shared mutable state; Pydantic models throughout. Shipped across `b35b3cc` through `86c7ab6`.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display. Shipped in `def9824`.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue. Shipped in `fdd357b`.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill. Shipped across `52d1722` through `d0e4426`.
