# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop. The platform dispatches through a two-stage queue (pending → queued), streams output in real time, enforces timeouts, manages approvals, and records everything. Outcome summaries close the feedback loop — the user can see what agents produced, and downstream agents receive that context. The UI presents task lifecycle as spatial layout: upcoming work in a kanban staging area, active execution front and center, completed work separated into success vs. non-success columns.

What's in place:
- **Two-stage queue** — pending (staging/curation) and queued (execution runway) with drag-based merge, split, transfer, and reorder
- **History view** — two-column kanban (completed vs. non-success) with terminal state differentiation (failed, timed out, cancelled, interrupted, rejected)
- **Outcome summaries** — auto-derived from commits between `start_commit` and `result_commit`, displayed inline on completed tasks
- **Outcome in dependency context** — downstream agents receive upstream outcome summaries in their trigger context
- **Job colors** — visual identifiers distinguishing jobs across all surfaces
- **Five trigger types** — manual, commit (watch via subscriptions), schedule (cron), dependency (declarative `depends_on`, circular chains permitted), agent (imperative cross-job dispatch)
- **Approval gates** — per-task `require_approval`, pending/approved/rejected lifecycle, manual dispatch bypasses
- **Dispatch continuity** — resume (CLI `--resume`) and retry for failed/timed-out dispatches
- **Dispatch diff view** — `start_commit`/`result_commit` tracking with inline diff display
- **Live dispatch streaming** — SSE with real-time text, tool use, and thinking indicator
- **Timeout enforcement** — configurable per-task with watchdog, graceful terminate then kill
- **Coalescing** — trigger-specific and global modes; manual queue composition (merge/split) in pending; agent triggers coalesce like other types
- **Internal MCP server** — platform-hosted, context-aware tool surface with git operations, file access, task info, branch management, and inter-agent dispatch. Every tool call is observable and auditable
- **Three-dimensional tool control** — `allowed_tools` (CLI), `allowed_internal_tools` (internal MCP), `mcp_servers` (external MCP) compose independently with discoverable inventories
- **Branch operations** — agents can create, switch, and merge branches with enforced naming conventions and audit trail
- **Inter-agent dispatch** — agents can enqueue tasks for other jobs via `dispatch_task`, query queue state via `get_queue_status`, governed by `allowed_dispatch_targets` with loop prevention
- **Chat, Files, Feed, Settings** — complete supporting surfaces

## Diagnosis

The platform has crossed a critical capability threshold. Tool governance and inter-agent coordination are now live — agents can manage branches, dispatch work to other agents, query queue state, and be selectively restricted from internal tools. The orchestrating-job pattern is now possible: a coordinator creates a branch, dispatches scoped tasks, monitors completion, and merges results. This is the first time agents can do more than react to predetermined triggers.

Two gaps remain:

**Outcome interrogation.** When a task completes, the user can see the outcome summary and diff, but cannot ask follow-up questions. The agent's session — its full reasoning, decisions, and context — is locked behind the streamed output log. The user wants to have a conversation with the agent that did the work, not just read what it produced. Critically, this conversation must be read-only: interrogation is not a license to make more changes. All new work flows through new tasks. **Design complete** — session resume via `--resume`, read-only tool stripping, UI integration in chat tray, all specified. Now that tool governance is implemented, the read-only restriction is a natural application of `allowed_internal_tools` — no special-case code needed.

**Operational visibility.** The platform works well when the user is actively watching — dispatching jobs, scanning the queue, reading history. But as task volume grows through automated triggers and now agent-initiated dispatches, the system generates more activity than a single person can track by inspection. There is no aggregated view of system health, no way to spot patterns across tasks. Agent dispatch chains add a new dimension of activity that is invisible without dedicated tooling. **Not yet designed** — scope is clear but no formal specification exists.

## Guiding Policy

**Close the loop on what's designed, then expand the surface.** Tool governance and inter-agent coordination shipped in a single implementation pass. Session interrogation is the last designed-but-unbuilt feature — it completes the operator's ability to understand what agents did and why. After that, the platform needs its first design cycle for operational visibility: the dashboard that turns growing task volume into legible patterns. The guiding constraint: ship the remaining designed feature first, then invest in the design work that unblocks the next wave.

## Priority 1: Task Session Interrogation

**The problem:** Completed tasks produce outcome summaries and diffs, but the user cannot ask follow-up questions. "Why did you change this file?" "What alternatives did you consider?" "What did the test output look like?" These questions require conversational access to the agent's session — the same context, the same reasoning chain. Currently the user must read the raw streamed output log, which is noisy and non-interactive.

**Why first:** This is the highest-leverage item that is also implementation-ready. The design is complete in DESIGN.md. The infrastructure already exists — every task creates a chat session, the CLI supports `--resume`. The only new work is (1) a UI surface to initiate read-only chat with a completed task's session, and (2) a tool restriction that strips write capabilities from the resumed session. Low implementation cost, high operator leverage, complete specification. The previous P1 (Activity Dashboard) is higher-leverage in theory but has no design specification — it would require a full design cycle before implementation could begin.

**Specifically:**
- **Session resume from History** — completed tasks in the History tab gain a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. No `Edit`, `Write`, `Bash` (or Bash in a read-only mode), no `git_commit`. The agent can read files, search code, and reason about its prior work, but cannot modify the project. This is the critical constraint: interrogation does not produce side effects.
- **Gating new work through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.
- **UI integration** — the session chat appears in the same chat tray used for standalone conversation, but with a visual indicator that this is a task session (job color, task reference) and that it is read-only.

**Second-order effects:** This changes how the operator relates to completed work. Instead of treating outcomes as final artifacts to accept or reject, the user can interrogate the reasoning, build understanding, and make better decisions about what to dispatch next. It also provides a natural feedback channel — the user's questions reveal what information the agent should have surfaced proactively, which directly informs dashboard design when that comes.

## Priority 2: Activity Dashboard

**The problem:** Understanding system behavior requires clicking through individual tasks. No aggregated view of health, performance, or patterns. The user manages agents but cannot see the forest.

**Why second:** The highest-leverage operator tool remaining. Now that tool governance and agent coordination are live, the system generates significantly more activity — agent-initiated dispatches, branch operations, cross-job coordination chains. This activity is invisible without aggregation. The dashboard needs a design pass through Designer and Architect, but should enter the design pipeline now so it's implementation-ready by the time P1 ships. The platform now has richer data to aggregate: branch operations, agent dispatch provenance, and tool access patterns alongside task success/failure.

**Specifically:**
- **Summary panel** — success/fail/timeout counts per job over configurable time windows (today, 7d, 30d). Answers "how are my agents doing?" at a glance
- **Job health indicators** — surface recurring failures, timeout patterns. Highlight jobs that need attention without the user hunting through history
- **Timeline view** — when tasks ran, how long they took. Simple horizontal bars, no chart library. Reveals scheduling conflicts, long-running outliers, and idle gaps
- **Tool usage patterns** — which MCP tools each job uses most, derived from the existing audit trail. Helps the user understand agent behavior and identify tool configuration opportunities
- **Agent dispatch chains** — trace provenance from agent-initiated dispatches. Show which jobs dispatch which other jobs and how deep chains go. This data now exists via the `agent` trigger type
- Data comes from `tasks` table plus MCP tool call logs — read-only aggregation, no schema changes

**Second-order effects:** The dashboard creates visibility that drives instruction tuning. When a user sees that the Engineer job fails 30% of the time on watch-triggered tasks, they know where to focus. Branch operation patterns, agent dispatch chains, and tool access distributions are all now available as data — the dashboard is the surface that makes them legible.

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. The tool surface is now broad enough (branch ops, dispatch, queue status) that agents can do most coordination without Bash. The `allowed_internal_tools` mechanism makes restriction possible. Revisit once real usage patterns reveal whether agents still rely on Bash for operations that could be structured tools.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, concurrent MCP sessions). Branch tools are now implemented, which removes the safety prerequisite. Revisit once the dashboard reveals whether sequential processing is a real bottleneck and once the orchestration pattern sees real use.
- **Auto-evaluation** — LLM judges dispatch output quality automatically. Needs a calibration dataset. Revisit once the dashboard reveals performance patterns and task session interrogation (P1) gives the operator a way to validate agent reasoning.
- **Prompt effectiveness tracking** — correlate job instruction changes with task success rates. Needs enough task history to be statistically meaningful. Falls out naturally once the dashboard surfaces job-level health.
- **Conditional dependencies** — "only run if upstream output matches X." Adds significant complexity to the trigger model for a use case that hasn't surfaced yet.
- **Structured output passing** — typed data exchange between tasks beyond the current outcome summary in trigger context. The current approach (commit messages + diff stats forwarded as text) works for most coordination. Full structured passing requires a data contract model that adds complexity without proven demand.
- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and global `PROJECT_DIR` state.
- **Cost tracking** — token usage per dispatch. Claude CLI doesn't expose token counts cleanly yet.
- **Notifications** — alerting the user when tasks complete or fail without requiring them to watch the UI. Valuable for fleet operation but adds external integration complexity. The dashboard (P4) reduces the need by surfacing patterns.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. The MCP server extends it rather than replacing it.
- **Full Bash removal** — agents need escape hatches. The goal is structured alternatives that agents prefer, not a sandbox that breaks them.

## Completed

- **Dispatch Outcome Summaries** (was P1) — auto-derived outcome summaries from commit history and outcome injection into dependency trigger context. The first feedback loop.
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
- **Platform Tool Governance** (was P2) — `allowed_internal_tools` per-job property, three-dimensional tool composition (CLI + internal MCP + external MCP), git branch operations (`git_branch_create` with naming conventions, `git_branch_switch`, `git_branch_merge`), branch audit trail via MCP tool call logging.
- **Inter-Agent Coordination** (was P3) — `dispatch_task` MCP tool with `agent` trigger type, `get_queue_status` for situational awareness, `allowed_dispatch_targets` per-job property (empty = deny-all), dispatch attribution to originating task, self-dispatch prohibition and loop prevention.
- **External MCP Server Lifecycle** — full registration, connection, tool discovery, and per-job assignment.
- **Route Modularization** — separate routers, shared state module, Pydantic models.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
