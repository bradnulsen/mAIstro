# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop and the first layer of feedback intelligence. The platform dispatches through a two-stage queue (pending → queued), streams output in real time, enforces timeouts, manages approvals, and records everything. Outcome summaries and ratings close the initial feedback loop — the user can see what agents produced and signal whether it was good. The UI presents task lifecycle as spatial layout: upcoming work in a kanban staging area, active execution front and center, completed work separated into success vs. non-success columns.

What's in place:
- **Two-stage queue** — pending (staging/curation) and queued (execution runway) with drag-based merge, split, transfer, and reorder
- **History view** — two-column kanban (completed vs. non-success) with terminal state differentiation (failed, timed out, cancelled, interrupted, rejected)
- **Outcome summaries** — auto-derived from commits between `start_commit` and `result_commit`, displayed inline on completed tasks
- **Task ratings** — binary positive/negative signal on completed tasks, creating a feedback dataset
- **Outcome in dependency context** — downstream agents receive upstream outcome summaries in their trigger context
- **Job colors** — visual identifiers distinguishing jobs across all surfaces
- **Four trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`, circular chains permitted)
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses
- **Dispatch continuity** — resume (CLI `--resume`) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — trigger-specific and global modes; manual queue composition (merge/split) in pending
- **Internal MCP server** — platform-hosted, context-aware tool surface with git operations, file access, and task info. Every tool call is observable and auditable
- **Tool control** — per-task `allowed_tools` and `mcp_servers` with discoverable inventories and full MCP server lifecycle
- **Chat, Files, Feed, Settings** — complete supporting surfaces

## Diagnosis

The platform is operationally complete and the first feedback loop is closed. Agents dispatch, produce outcomes, and those outcomes are summarized, rateable, and forwarded to downstream agents. The two-stage queue gives users curation control; the history view makes success and failure spatially distinct.

The remaining gap is **operational scale**. The platform works well when the user is actively watching — dispatching jobs, scanning the queue, reading history. But as task volume grows through automated triggers (watches, schedules, dependency chains), the system generates more activity than a single person can track by inspection. There is no aggregated view of system health, no way to spot patterns across tasks, and no mechanism for agents to coordinate beyond the existing trigger primitives.

The second gap is **agent coordination**. Agents operate in isolation. They can trigger downstream work through commits and dependencies, but they cannot reason about the broader system — they don't know what's in the queue, can't request work from other agents, and have no channel for lateral communication. The trigger system is declarative and reactive; agents need imperative coordination tools for cases where the right response isn't predetermined.

## Guiding Policy

**Scale the operator.** The platform works for a hands-on user running a handful of agents. Now it needs to work for a user running a fleet — where attention is the scarce resource and the platform must surface what matters without being asked. Each priority either reduces the operator's attention burden or expands what agents can do without operator intervention.

This is a shift from operational completeness to operational leverage. The infrastructure is proven. Now multiply the user's capacity.

## Priority 1: Activity Dashboard

**The problem:** Understanding system behavior requires clicking through individual tasks. No aggregated view of health, performance, or patterns. The user manages agents but cannot see the forest. With outcome summaries and ratings now in place, the data exists to surface meaningful signals — but there is no surface to show them.

**Why first:** This is the highest-leverage improvement for operator scale. The data foundation exists (summaries, ratings, terminal states, timestamps). The dashboard turns per-task data into system-level insight with no schema changes — pure read-only aggregation. It also reveals where the system is struggling, which directly informs whether instruction tuning or coordination (P2) is more urgent.

**Specifically:**
- **Summary panel** — success/fail/timeout counts per job over configurable time windows (today, 7d, 30d). Answers "how are my agents doing?" at a glance
- **Job health indicators** — surface recurring failures, timeout patterns, jobs with low ratings. Highlight jobs that need attention without the user hunting through history
- **Timeline view** — when tasks ran, how long they took. Simple horizontal bars, no chart library. Reveals scheduling conflicts, long-running outliers, and idle gaps
- **Tool usage patterns** — which MCP tools each job uses most, derived from the existing audit trail. Helps the user understand agent behavior and identify tool configuration opportunities
- Data comes from `tasks` table plus MCP tool call logs — read-only aggregation, no schema changes

**Second-order effects:** The dashboard creates visibility that drives instruction tuning. When a user sees that the Engineer job fails 30% of the time on watch-triggered tasks, they know where to focus. This is the prerequisite for systematic improvement — you can't optimize what you can't measure.

## Priority 2: Inter-Agent Coordination via MCP Tools

**The problem:** Agents operate in isolation. They can trigger downstream work through commits and dependencies, but these are declarative, predetermined channels. There is no way for an agent to say "I found an issue the Engineer should address" or "the queue is backed up, I should skip non-critical work." The trigger system handles predictable cascades; agents need imperative tools for emergent coordination.

**Why second:** The MCP infrastructure is proven and stable — adding tools is a natural extension. But coordination tools carry real complexity (permission models, loop prevention, dispatch attribution) that the dashboard doesn't. The dashboard also reveals system patterns that inform *which* coordination primitives are most needed. Build visibility first, then act on it.

**Specifically:**
- **`dispatch_task` MCP tool** — allows an agent to enqueue a task for another job, with a message explaining why. Creates a trigger type `agent` attributed to the dispatching task
- **`get_queue_status` MCP tool** — read-only view of queue state. Gives agents situational awareness about what's pending, running, and backed up
- **Job-level access control** — a job property controls which other jobs an agent can dispatch. Prevents unconstrained cross-agent triggering
- **Dispatch attribution** — tasks created by agents are tagged with the originating task, creating a provenance chain visible in history
- **Loop prevention** — no self-dispatch, depth limit on agent-initiated dispatch chains, coalescing absorbs redundant enqueues

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. Requires the MCP tool surface to be comprehensive enough that agents don't need Bash for routine operations. The current tool set (git, file access, task info) isn't broad enough yet. Inter-agent coordination (P2) will expand the tool surface, making this more viable afterward.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, branch management, concurrent MCP sessions). Sequential processing isn't a proven bottleneck yet. The dashboard (P1) will reveal whether queue depth and execution time are actual pain points.
- **Auto-evaluation** — LLM judges dispatch output quality automatically. Needs the manual rating dataset to calibrate what "good" looks like. Ratings exist now but need volume before automated evaluation is meaningful. Revisit once the dashboard reveals rating patterns.
- **Prompt effectiveness tracking** — correlate job instruction changes with task success rates. Needs enough rated tasks to be statistically meaningful. Falls out naturally once ratings accumulate and the dashboard surfaces job-level health.
- **Conditional dependencies** — "only run if upstream output matches X." Adds significant complexity to the trigger model for a use case that hasn't surfaced yet.
- **Structured output passing** — typed data exchange between tasks beyond the current outcome summary in trigger context. The current approach (commit messages + diff stats forwarded as text) works for most coordination. Full structured passing requires a data contract model that adds complexity without proven demand.
- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and global `PROJECT_DIR` state.
- **Cost tracking** — token usage per dispatch. Claude CLI doesn't expose token counts cleanly yet.
- **Notifications** — alerting the user when tasks complete or fail without requiring them to watch the UI. Valuable for fleet operation but adds external integration complexity (email, system notifications, webhooks). The dashboard (P1) reduces the need by surfacing patterns; notifications become more targeted once the user knows what to watch for.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. The MCP server extends it rather than replacing it.
- **Full Bash removal** — agents need escape hatches. The goal is structured alternatives that agents prefer, not a sandbox that breaks them.

## Completed

- **Dispatch Outcome Summaries** (was P1) — auto-derived outcome summaries from commit history, binary task ratings, and outcome injection into dependency trigger context. The first feedback loop.
- **Two-Stage Queue** — pending (staging/curation) and queued (execution runway) with specialized drag operations: merge and split in pending, reorder in queued, transfer between columns.
- **History Tab** — two-column kanban (completed vs. non-success) within the Dispatch view. Terminal states (failed, timed out, cancelled, interrupted, rejected) are visually differentiated with status badges and inline error context.
- **Terminal State Differentiation** — six distinct terminal states with clear behavioral semantics. Only completed (no error) triggers downstream dependencies. Each non-success state implies a different user response.
- **Job Colors** — visual identifiers assigned on creation from a curated palette, displayed across all job-related surfaces.
- **Internal MCP Server** — platform-hosted, context-aware MCP server with git operations, read-only project context tools, tool invocation logging, and task-specific tool surfaces.
- **Files View** — project file browser with glob search, syntax highlighting for code, markdown rendering for documentation.
- **Contextual Help Tooltips** — hover tooltips on configuration fields (subscriptions, schedule, allowed tools, approval, coalescing, dependencies, timeout, auto-queueing, MCP servers, model).
- **Circular Dependencies** — `depends_on` permits cycles; coalescing absorbs redundant triggers.
- **Design Tokens** — consolidated hardcoded colors into CSS custom properties.
- **Approval Gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses.
- **Project Switch Safety** — block project switch/close during active dispatch.
- **Schema Cleanup** — definitive schema with no migrations or legacy fallbacks.
- **Trigger Context** — structured `triggers` JSON array with pre-formatted context strings.
- **Task Dependencies** — `depends_on` property, `dependency` trigger type with coalescing.
- **Watch Semantics** — subscriptions presence = watch active, no separate toggle.
- **Settings and Configuration UI** — queue behavior (auto-queueing/manual), default model, default timeout, MCP server management.
- **Tool Discoverability** — platform presents available CLI tools, internal MCP tools, and external MCP server tools as selectable options.
- **External MCP Server Lifecycle** — full registration, connection, tool discovery, and per-job assignment.
- **Route Modularization** — separate routers, shared state module, Pydantic models.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
