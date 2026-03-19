# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop and operational visibility. The platform dispatches through a two-stage queue (pending → queued), streams output in real time, enforces timeouts, manages approvals, and records everything. Outcome summaries close the feedback loop — the user can see what agents produced, and downstream agents receive that context. The Activity Dashboard aggregates system health, task timelines, agent dispatch chains, and tool usage patterns into a single view. The UI presents task lifecycle as spatial layout: upcoming work in a kanban staging area, active execution front and center, completed work separated into success vs. non-success columns.

What's in place:
- **Two-stage queue** — pending (staging/curation) and queued (execution runway) with drag-based merge, split, transfer, and reorder
- **History view** — two-column kanban (completed vs. non-success) with terminal state differentiation (failed, timed out, cancelled, interrupted, rejected)
- **Activity Dashboard** — job health summaries, task timeline, agent dispatch chain visualization, tool usage patterns, configurable time windows
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
- **Inter-agent dispatch** — agents can enqueue tasks for other jobs via `dispatch_task`, query queue state via `get_queue_status`, governed by `allowed_dispatch_targets` with depth limiting and loop prevention
- **Chat, Files, Feed, Settings** — complete supporting surfaces

## Diagnosis

The platform now has both execution capability and operational visibility. Agents can manage branches, dispatch work to other agents, query queue state, and be selectively restricted from tools. The Activity Dashboard surfaces job health, task timing, dispatch chains, and tool usage — the operator can see patterns across tasks without clicking through history one by one. Dispatch depth limiting prevents runaway agent cascades.

Two designed-but-unbuilt gaps remain, both now fully specified:

**Outcome interrogation.** When a task completes, the user can see the outcome summary, diff, and dashboard aggregates — but cannot ask follow-up questions. The agent's session — its full reasoning, decisions, and context — is locked behind the streamed output log. The user wants to have a conversation with the agent that did the work, not just read what it produced. Critically, this conversation must be read-only: interrogation is not a license to make more changes. All new work flows through new tasks. **Design complete** — session resume via `--resume`, read-only tool stripping, UI integration in chat tray, all specified. The `allowed_internal_tools` mechanism makes read-only restriction a configuration concern, not special-case code.

**Proactive alerting.** The dashboard answers "how are my agents doing?" when the user looks — but doesn't tell them when something needs attention. As the system runs more autonomously (scheduled jobs, agent-initiated dispatch chains, watch triggers), the operator is increasingly absent from the UI during task execution. Failures, timeouts, and anomalies go unnoticed until the next manual check. **Design complete** — in-app feed with read/unread state, desktop notifications via browser API, per-job notification rules with conservative defaults, and outbound webhook integration, all specified in DESIGN.md. Safety constraints are explicit: notifications are read-only signals that never create or modify tasks.

## Guiding Policy

**Ship the last designed features — reactive first, then proactive.** Both remaining features now have complete designs. Session interrogation is the final piece of reactive operator tooling — it completes the inspect-understand-decide loop. Notifications make the platform proactive — telling the operator when to look instead of requiring them to watch. The order is deliberate: interrogation first because the operator needs the investigation tool before the system starts nudging them to investigate. The guiding constraint: complete the reactive surface, then build the proactive one.

## Priority 1: Task Session Interrogation

**The problem:** Completed tasks produce outcome summaries and diffs, but the user cannot ask follow-up questions. "Why did you change this file?" "What alternatives did you consider?" "What did the test output look like?" These questions require conversational access to the agent's session — the same context, the same reasoning chain. Currently the user must read the raw streamed output log, which is noisy and non-interactive.

**Why first:** This is the highest-leverage item that is also implementation-ready. The design is complete in DESIGN.md. The infrastructure already exists — every task creates a chat session, the CLI supports `--resume`. The only new work is (1) a UI surface to initiate read-only chat with a completed task's session, and (2) a tool restriction that strips write capabilities from the resumed session. Low implementation cost, high operator leverage, complete specification.

**Specifically:**
- **Session resume from History** — completed tasks in the History tab gain a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. No `Edit`, `Write`, `Bash` (or Bash in a read-only mode), no `git_commit`. The agent can read files, search code, and reason about its prior work, but cannot modify the project. This is the critical constraint: interrogation does not produce side effects.
- **Gating new work through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.
- **UI integration** — the session chat appears in the same chat tray used for standalone conversation, but with a visual indicator that this is a task session (job color, task reference) and that it is read-only.

**Second-order effects:** This changes how the operator relates to completed work. Instead of treating outcomes as final artifacts to accept or reject, the user can interrogate the reasoning, build understanding, and make better decisions about what to dispatch next. It also provides a natural feedback channel — the user's questions reveal what information the agent should have surfaced proactively, which can inform dashboard refinements and notification rule design.

## Priority 2: Notifications

**The problem:** The platform requires the operator to be watching. Task completions, failures, timeouts, and anomalies are only visible when the user opens the UI and looks at the queue or dashboard. As the system becomes more autonomous — scheduled jobs running overnight, agent-initiated dispatch chains executing while the operator is away, watch triggers firing on every commit — the gap between "something happened" and "the operator knows" grows. The dashboard made patterns visible; notifications make them timely.

**Why second:** This is the transition from reactive to proactive operation. Session interrogation (P1) completes the reactive tooling: the operator can see, understand, and interrogate what agents did. Notifications complete the proactive tooling: the operator is told when to look. Together they close the full loop — the system alerts the operator, the operator investigates via dashboard and session interrogation, then dispatches corrective work through the queue. Without notifications, the operator must poll. Polling doesn't scale with autonomous task volume. **Design complete** — all four channels (in-app feed, desktop, rules, webhook) are specified in DESIGN.md with safety constraints. Implementation-ready.

**Specifically:**
- **In-app notification feed** — a lightweight notification center (badge + dropdown or panel) showing recent events: task completions, failures, timeouts, approval requests pending. This is the minimum viable surface — no external integrations required, works immediately.
- **Desktop notifications** — browser Notification API for critical events (failures, timeouts, approval requests). The user opts in. Low implementation cost, high leverage for the "away from tab" use case.
- **Notification rules** — per-job or global: notify on all completions, only failures, only timeouts, never. Defaults to failures and timeouts. The operator tunes signal-to-noise as task volume grows.
- **Webhook integration** — an outbound webhook (URL + optional secret) that fires on configurable events. This is the escape hatch for Slack, Discord, email, or any external system. The platform sends a structured JSON payload; the user wires it to wherever they want. One generic mechanism instead of N specific integrations.

**Second-order effects:** Notifications change the operator's relationship with the system from monitoring to supervising. The operator can leave the UI, trust that the system will surface what matters, and return only when needed. This is a prerequisite for scaling to more jobs, more triggers, and longer autonomous runs. It also makes approval gates practical at scale — an approval request that sits unseen in the queue is a blocked pipeline. A notification makes it a prompt.

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. The tool surface is now broad enough (branch ops, dispatch, queue status) that agents can do most coordination without Bash. The `allowed_internal_tools` mechanism makes restriction possible. Revisit once real usage patterns reveal whether agents still rely on Bash for operations that could be structured tools.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, concurrent MCP sessions). Branch tools are now implemented, which removes the safety prerequisite. Revisit once the dashboard reveals whether sequential processing is a real bottleneck and once the orchestration pattern sees real use.
- **Auto-evaluation** — LLM judges dispatch output quality automatically. Needs a calibration dataset. Revisit once the dashboard reveals performance patterns and task session interrogation (P1) gives the operator a way to validate agent reasoning.
- **Prompt effectiveness tracking** — correlate job instruction changes with task success rates. Needs enough task history to be statistically meaningful. The dashboard now surfaces job-level health; revisit once enough history accumulates.
- **Conditional dependencies** — "only run if upstream output matches X." Adds significant complexity to the trigger model for a use case that hasn't surfaced yet.
- **Structured output passing** — typed data exchange between tasks beyond the current outcome summary in trigger context. The current approach (commit messages + diff stats forwarded as text) works for most coordination. Full structured passing requires a data contract model that adds complexity without proven demand.
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
- **Inter-Agent Coordination** (was P3) — `dispatch_task` MCP tool with `agent` trigger type, `get_queue_status` for situational awareness, `allowed_dispatch_targets` per-job property (empty = deny-all), dispatch attribution to originating task, self-dispatch prohibition, loop prevention, and configurable dispatch depth limiting.
- **Activity Dashboard** (was P2) — job health summaries, task timeline visualization, agent dispatch chain tracing, tool usage patterns. Read-only aggregation over existing task and MCP tool call data with configurable time windows.
- **External MCP Server Lifecycle** — full registration, connection, tool discovery, and per-job assignment.
- **Route Modularization** — separate routers, shared state module, Pydantic models.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
