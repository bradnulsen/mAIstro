# Strategy

## Current State

mAistro is a fully operational multi-agent orchestration platform with platform-mediated tool access. The coordination layer is complete: four trigger types, approval gates, retry/resume, timeout enforcement, coalescing, queue-first dispatch, and an internal MCP server providing structured tool alternatives with full audit trails. The codebase is cleanly modularized with extracted routers, shared state, and a definitive schema. Product requirements and constraints are formalized in DESIGN.md; architecture is documented across subsystem descriptions.

What's in place:
- **Queue-first dispatch** — background worker, auto-processing/paused modes, stale sweep, cancellation
- **Four trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`, circular chains permitted)
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses
- **Dispatch continuity** — resume (CLI `--resume`) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — commit and dependency coalesce same-type; schedule coalesces globally; `coalesce_dispatches` coalesces all trigger types; manual/resume never coalesce
- **Trigger context** — structured `triggers` JSON array with pre-formatted context strings, injected into dispatch prompts
- **Task config** — tabbed UI (definition/triggers), dependency checkboxes, subscription file preview, markdown preview, search filter
- **Settings** — queue behavior, default model, default timeout, MCP server management
- **Chat with session persistence** — SSE streaming, session list, dispatch-linked audit trail
- **Git feed** — commit diffs with file/line stats, live polling
- **Project safety** — project switch blocked during active dispatch
- **Internal MCP server** — platform-hosted, context-aware tool surface with git operations, file access, and task info as structured tools. Every tool call is observable and auditable
- **Tool control** — per-task `allowed_tools` and `mcp_servers` properties with discoverable inventories and full MCP server lifecycle management
- **Files view** — project file browser with glob search, syntax highlighting, markdown rendering
- **Contextual help** — hover tooltips on configuration fields explaining syntax and behavior

## Diagnosis

The platform works end-to-end. Tasks dispatch, agents operate through mediated tools, commits trigger cascades, dependencies chain. The MCP server provides the accountability layer — structured tool access with audit trails. The orchestration story is solid.

The gap is now **agent effectiveness**. The platform dispatches reliably and mediates tool access, but has no mechanism to understand whether agents are doing *good work*. A dispatch completes, commits land, but: Was the output correct? Did the agent waste time? Should the instructions be tuned? The platform is operationally sound but strategically blind — it can tell you *what* happened but not *whether it should have happened differently*.

The second gap is **operational awareness**. As dispatch volume grows through dependencies, schedules, and watch triggers, understanding the system's behavior requires clicking through individual dispatches. There's no summary view, no health indicators, no way to spot patterns without manual inspection.

## Guiding Policy

**Close the feedback loop.** The platform can dispatch and mediate — now it needs to evaluate and improve. The next phase builds the mechanisms that let users (and eventually the system itself) assess agent output quality and tune the system accordingly. Each priority produces data or surfaces that make the next one more effective.

This is a shift from infrastructure to intelligence. The MCP server gave us the audit trail. Now we use it.

## Priority 1: Dispatch Outcome Summaries

**The problem:** When a dispatch completes, the user sees raw streamed output and a diff. Understanding what the agent actually accomplished — and whether it was good — requires reading through everything manually. At scale, this doesn't work.

**Why first:** This is the lowest-cost, highest-signal improvement available. No schema changes to core tables, no new views — just surfacing information that already exists in a more useful form. It also establishes the data patterns that the dashboard (P2) will aggregate.

**Specifically:**
- **Auto-generated outcome summary** — when a dispatch completes, derive a short summary from the diff stat and commit messages produced during the dispatch window (between `start_commit` and `result_commit`). Display this in the queue list so users can scan results without opening each dispatch.
- **Dispatch rating** — simple thumbs up/down on completed dispatches. Stored in `dispatch_queue`. This creates the first feedback signal that can later be correlated with task instructions, models, and patterns.
- **Outcome in trigger context** — when a dispatch triggers dependents, include the outcome summary in the downstream trigger context. This gives dependent tasks richer information about what their upstream actually did.

**Second-order effects:** Ratings create a dataset. Once you have enough rated dispatches, you can correlate success with instruction changes, model choices, and trigger types. This is the foundation for prompt effectiveness tracking without building it explicitly.

## Priority 2: Activity Dashboard

**The problem:** Understanding system behavior requires clicking through individual dispatches. No aggregated view of health, performance, or patterns. The user manages agents but cannot see the forest.

**Why second:** With outcome summaries and ratings from P1, the dashboard has meaningful data to aggregate — not just counts, but quality signals. Building it after P1 means it ships useful from day one rather than showing bare dispatch counts.

**Specifically:**
- **Summary panel** — success/fail/timeout counts per task over configurable time windows (today, 7d, 30d)
- **Task health indicators** — surface recurring failures, timeout patterns, tasks with low ratings
- **Timeline view** — when dispatches ran, how long they took. Simple horizontal bars, no chart library
- **Tool usage patterns** — which MCP tools each task uses most, visible from the existing audit trail
- Data comes from `dispatch_queue` plus MCP tool call logs — read-only aggregation, no schema changes to core tables

## Priority 3: Inter-Task Coordination via MCP Tools

**The problem:** Tasks cannot communicate with or trigger other tasks except through the existing trigger system (commit-watch, dependency, schedule). There's no way for an agent to say "I found an issue the Engineer should address" or "this needs review before I proceed." The only coordination primitive is git commits triggering subscription-matched dispatches.

**Why third:** The MCP infrastructure is proven and stable. Adding coordination tools is a natural extension — agents using platform tools to orchestrate other agents. But it carries real complexity (permission models, loop prevention, dispatch attribution) that the simpler P1 and P2 items don't. Get the feedback loop working first, then expand what agents can do.

**Specifically:**
- **`dispatch_task` MCP tool** — allows an agent to enqueue a dispatch for another task, with a message explaining why. Creates a trigger attributed to the dispatching task
- **`get_queue_status` MCP tool** — read-only view of the queue. Gives agents situational awareness
- **Task-level access control** — a task property controls which tasks an agent can dispatch
- **Dispatch attribution** — queue entries created by agents are tagged with the originating task and dispatch, creating a provenance chain
- **Loop prevention** — no self-dispatch, depth limit on dispatch chains, coalescing absorbs redundant enqueues

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. Requires the MCP tool surface to be comprehensive enough that agents don't need Bash for routine operations. The current tool set (git, file access, task info) isn't broad enough yet.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, branch management, concurrent MCP sessions). Sequential processing isn't a proven bottleneck yet.
- **Auto-evaluation** — LLM judges dispatch output quality automatically. Needs the manual rating dataset from P1 to calibrate what "good" looks like. Build the human feedback loop first, then automate it.
- **Prompt effectiveness tracking** — correlate task instruction changes with dispatch success rates. Needs enough rated dispatches to be statistically meaningful. Falls out naturally once P1 ratings accumulate.
- **Conditional dependencies** — "only run if upstream output matches X." Adds significant complexity to the trigger model for a use case that hasn't surfaced yet.
- **Richer dependency context** — upstream dispatch output summary injected into downstream context. Partially addressed by P1's outcome-in-trigger-context; full implementation (structured output passing) is more complex.
- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and global `PROJECT_DIR` state.
- **Cost tracking** — token usage per dispatch. Claude CLI doesn't expose token counts cleanly yet.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. The MCP server extends it rather than replacing it.
- **Full Bash removal** — agents need escape hatches. The goal is structured alternatives that agents prefer, not a sandbox that breaks them.

## Completed

- **Internal MCP Server** (was P1) — platform-hosted, context-aware MCP server with git operations (`git_commit`, `git_diff`, `git_log`, `git_status`), read-only project context tools (`list_files`, `read_file`, `list_tasks`), tool invocation logging, and task-specific tool surfaces.
- **Files View** — project file browser with glob search, syntax highlighting for code, markdown rendering for documentation.
- **Contextual Help Tooltips** — hover tooltips on configuration fields (subscriptions, schedule, allowed tools, approval, coalescing, dependencies, timeout, auto-dispatch, MCP servers, model).
- **Circular Dependencies** — `depends_on` permits cycles; coalescing absorbs redundant triggers.
- **Design Tokens** — consolidated hardcoded colors into CSS custom properties.
- **Approval Gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses.
- **Project Switch Safety** — block project switch/close during active dispatch.
- **Schema Cleanup** — definitive schema with no migrations or legacy fallbacks.
- **Trigger Context** — `dispatch_queue.triggers` JSON array with structured entries.
- **Task Dependencies** — `depends_on` property, `dependency` trigger type with coalescing.
- **Watch Semantics** — subscriptions presence = watch active, no separate toggle.
- **Settings and Configuration UI** — queue behavior (auto-processing/paused), default model, default timeout, MCP server management.
- **Tool Discoverability** — platform presents available CLI tools, internal MCP tools, and external MCP server tools as selectable options. Users configure from known inventory, not free-text.
- **External MCP Server Lifecycle** — full registration, connection, tool discovery, and per-job assignment. Health visibility for connected servers.
- **Route Modularization** — separate routers, shared state module, Pydantic models.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
