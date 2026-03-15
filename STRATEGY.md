# Strategy

## Current State

mAistro is a fully operational multi-agent orchestration platform. The coordination layer is complete: four trigger types (manual, commit, schedule, dependency), approval gates, retry/resume, timeout enforcement, coalescing, and a queue-first dispatch model. The codebase is ~5K lines across backend and frontend, cleanly modularized with extracted routers, shared state, and a definitive schema (no migrations, no legacy fallbacks).

What's in place:
- **Queue-first dispatch** — background worker, auto/manual processing, stale sweep, cancellation
- **Four trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`)
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses
- **Dispatch continuity** — resume (CLI `--resume`) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — commit and dependency coalesce same-type; schedule coalesces globally; `coalesce_dispatches` coalesces all trigger types; manual/resume never coalesce
- **Trigger context** — structured `triggers` JSON array with pre-formatted context strings, injected into dispatch prompts
- **Task config** — tabbed UI (definition/triggers), dependency checkboxes with cycle prevention, subscription file preview, markdown preview, search filter
- **Settings** — queue behavior, default model, default timeout, MCP server management
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Project safety** — project switch blocked during active dispatch

## What Matters Now

The automation layer is functionally complete. The system can compose tasks into workflows, gate them for approval, trigger from multiple sources, and process them reliably. The platform is being used to develop itself — five agents (Architect, Frontend, Scribe, Strategist, Designer) plus an on-demand Prototyper coordinate through git to build mAistro.

The product definition is now formalized in DESIGN.md — requirements, constraints, and behavioral contracts captured as a stable reference. This means the platform's operational invariants (queue-first dispatch, sequential execution, project isolation, git as content truth) are documented and deliberate. Future features that relax these constraints should do so explicitly, not accidentally.

The remaining gaps are about **observability, throughput, and agent quality**:

1. **Observability** — understanding what agents accomplished requires clicking through individual dispatches. No aggregated view of system health or agent performance.
2. **Throughput** — sequential processing bottlenecks dependency chains. Independent tasks wait unnecessarily.
3. **Agent quality** — the platform dispatches well, but the agents themselves have no feedback loop. No way to evaluate whether a dispatch produced good output or to tune prompts based on outcomes.

## Priority 1: Activity Dashboard

**The problem:** The Queue view shows individual dispatches. The Feed shows individual commits. Neither answers "what did my agents accomplish today?" or "which tasks are failing repeatedly?" As dispatch volume grows with dependencies and scheduling, users lose the thread.

**Why first:** Observability is what makes autonomous operation sustainable. Without it, users stop trusting the system — not because it's broken, but because they can't tell if it's working. This is especially acute now that approval gates exist: users approving dispatches need context about recent success/failure rates.

**Specifically:**
- **Summary panel** on the Queue view (or a new Dashboard view): success/fail/timeout counts per task over configurable time windows (today, 7d, 30d)
- **Task health indicators** — surface recurring failures (same task timing out repeatedly, same error pattern)
- **Timeline view** — when dispatches ran, how long they took, gaps between runs. Simple horizontal bars, not a complex chart library
- Data is already in `dispatch_queue` — this is a read-only aggregation, no schema changes needed

## Priority 2: Parallel Dispatch

**The problem:** The worker processes one dispatch at a time. With dependency chains, independent branches of the workflow wait in line behind each other. Two tasks that both depend on Strategist must run sequentially even though they have no relationship to each other.

**Why second:** Sequential processing was the right starting point — it eliminates git conflicts and simplifies the mental model. But as workflows grow, it becomes the throughput bottleneck. The mitigation: parallel dispatch with git worktree isolation.

**Specifically:**
- Allow N concurrent dispatches (configurable, default: 1 to preserve current behavior)
- Each concurrent dispatch operates in a git worktree, isolating file changes
- On completion, the worktree's branch is merged back to the main branch
- Conflict detection: if a merge has conflicts, mark the dispatch as needing manual resolution
- Worker becomes a pool: N slots, each can run one dispatch independently
- Worktree lifecycle is managed by the worker — create on start, merge + clean up on completion

**Design consideration:** This is the hardest feature on the roadmap and the only priority that relaxes a documented design constraint (DESIGN.md states "exactly one dispatch runs at a time"). Git worktrees add real complexity — merge conflicts, branch management, cleanup on failure. The tracer bullet should be: two concurrent dispatches in separate worktrees, auto-merge on clean completion, error on conflict. Fancy conflict resolution comes later. When this ships, DESIGN.md's sequential execution constraint should be updated to reflect the new concurrency model.

## Priority 3: Dispatch Evaluation

**The problem:** There's no feedback loop on agent output quality. A dispatch completes, commits are made, but there's no mechanism to assess whether the work was good, whether the prompt produced the intended result, or whether the task instructions need tuning. The only signal is "did it error or not."

**Why third:** The platform is now reliable enough that the bottleneck shifts from "does it work" to "does it work well." As the system develops itself, the quality of each dispatch compounds — a bad Architect dispatch creates tech debt that other agents inherit. Evaluation closes the loop.

**Specifically:**
- **Dispatch rating** — simple thumbs up/down on completed dispatches, stored in dispatch_queue
- **Outcome summary** — auto-generated one-line summary of what the dispatch changed (diff stat + commit messages), shown in queue list without requiring click-through
- **Prompt effectiveness tracking** — correlate task instruction changes with dispatch success rates over time
- Start with manual rating; auto-evaluation (LLM judges output quality) is a future extension

## Deferred

Valuable but not blocking the current phase:

- **Conditional dependencies** — "only run if upstream output matches X." Useful for branching workflows but adds significant complexity to the trigger model. Wait until linear dependency chains prove insufficient.
- **Richer dependency context** — upstream dispatch output summary injected into downstream context. Currently dependencies pass trigger type, upstream task ID, and commit info. Enriching further requires reading/summarizing upstream dispatch output.
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

- **Approval Gates** (was P1) — per-task `require_approval` property, `dispatch_queue.approval` column (null/pending/approved/rejected), manual dispatch bypasses, queue UI with pending indicator and approve/reject actions. Shipped across `9bdc8aa` through `df1c67a`.
- **Project Switch Safety** — block project switch/close during active dispatch (409 from `/api/project/open`). Shipped in `be79408`.
- **Schema Cleanup** — stripped backward compatibility, consolidated to definitive schema with no migrations or legacy fallbacks. Shipped in `4f4d1f7`.
- **Trigger Context Migration** — `dispatch_queue.triggers` JSON array replaces flat `trigger`/`trigger_detail`/`context` columns. Each entry has `trigger`, `detail`, `context`. Shipped across `adf3be9` through `8be2810`.
- **Task Dependencies** (was P1) — `depends_on` JSON property, `dependency` trigger type with same-type coalescing, auto-enqueue on upstream completion, circular dependency prevention, frontend checkboxes in Triggers tab. Shipped in `43d1ac7` through `429259e`.
- **Watch Semantics Cleanup** — removed `watch_enabled` toggle; subscriptions presence = watch active. Commit coalescing is now automatic. Shipped in `8727171`.
- **Settings and Configuration UI** — queue behavior, default model, default timeout, MCP server management. Shipped in `afc7f5c`.
- **Route Modularization** — `task_routes.py`, `queue_routes.py`, `chat.py` as separate routers; `state.py` for shared mutable state; Pydantic models throughout. Shipped across `b35b3cc` through `86c7ab6`.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display. Shipped in `def9824`.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue. Shipped in `fdd357b`.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill. Shipped across `52d1722` through `d0e4426`.
