# Strategy

## Current State

mAistro is a mature multi-agent orchestration platform with a complete operational loop. The platform dispatches through a two-stage queue (pending → queued), streams output in real time, enforces timeouts, manages approvals, and records everything. Outcome summaries close the feedback loop — the user can see what agents produced, and downstream agents receive that context. The UI presents task lifecycle as spatial layout: upcoming work in a kanban staging area, active execution front and center, completed work separated into success vs. non-success columns.

What's in place:
- **Two-stage queue** — pending (staging/curation) and queued (execution runway) with drag-based merge, split, transfer, and reorder
- **History view** — two-column kanban (completed vs. non-success) with terminal state differentiation (failed, timed out, cancelled, interrupted, rejected)
- **Outcome summaries** — auto-derived from commits between `start_commit` and `result_commit`, displayed inline on completed tasks
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

The platform is operationally complete and the first feedback loop is closed. Agents dispatch, produce outcomes, and those outcomes are summarized and forwarded to downstream agents. The two-stage queue gives users curation control; the history view makes success and failure spatially distinct.

A significant milestone: the design specifications for session interrogation, tool governance, branch operations, and inter-agent coordination are now formalized in DESIGN.md. The architecture has absorbed them. Three of the four priorities are now implementation-ready with complete behavioral contracts. This shifts the strategic calculus from "what to design next" to "what to build next" — and implementation readiness matters for ordering.

Three gaps remain:

**Outcome interrogation.** When a task completes, the user can see the outcome summary and diff, but cannot ask follow-up questions. The agent's session — its full reasoning, decisions, and context — is locked behind the streamed output log. The user wants to have a conversation with the agent that did the work, not just read what it produced. Critically, this conversation must be read-only: interrogation is not a license to make more changes. All new work flows through new tasks. **Design complete** — session resume via `--resume`, read-only tool stripping, UI integration in chat tray, all specified.

**Agent autonomy and blast radius control.** Agents operate in isolation with a fixed internal tool surface. They cannot manage branches, cannot be selectively restricted from internal tools, and have no imperative coordination channel. The trigger system handles predictable cascades, but agents need richer primitives — particularly git branch management — for cases like orchestrating feature sprints where an upstream job creates a branch, dispatches work on it, and merges results. **Design complete** — `allowed_internal_tools`, three branch tools, `dispatch_task`, `get_queue_status`, `allowed_dispatch_targets`, `agent` trigger type, loop prevention, all specified.

**Operational visibility.** The platform works well when the user is actively watching — dispatching jobs, scanning the queue, reading history. But as task volume grows through automated triggers, the system generates more activity than a single person can track by inspection. There is no aggregated view of system health, no way to spot patterns across tasks. **Not yet designed** — scope is clear but no formal specification exists.

## Guiding Policy

**Build what's designed, then design what's next.** The design specifications for session interrogation, tool governance, and inter-agent coordination are complete. These are not aspirational — they have behavioral contracts, property definitions, safety constraints, and UI integration points. The strategic imperative shifts from "decide what to do" to "execute in the right order." Implementation readiness now factors into priority ordering alongside leverage. A fully-designed feature with moderate leverage ships before an undesigned feature with high leverage, because the design pipeline is not free — it takes a full cycle through Designer and Architect before implementation can begin.

## Priority 1: Task Session Interrogation

**The problem:** Completed tasks produce outcome summaries and diffs, but the user cannot ask follow-up questions. "Why did you change this file?" "What alternatives did you consider?" "What did the test output look like?" These questions require conversational access to the agent's session — the same context, the same reasoning chain. Currently the user must read the raw streamed output log, which is noisy and non-interactive.

**Why first:** This is the highest-leverage item that is also implementation-ready. The design is complete in DESIGN.md. The infrastructure already exists — every task creates a chat session, the CLI supports `--resume`. The only new work is (1) a UI surface to initiate read-only chat with a completed task's session, and (2) a tool restriction that strips write capabilities from the resumed session. Low implementation cost, high operator leverage, complete specification. The previous P1 (Activity Dashboard) is higher-leverage in theory but has no design specification — it would require a full design cycle before implementation could begin.

**Specifically:**
- **Session resume from History** — completed tasks in the History tab gain a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. No `Edit`, `Write`, `Bash` (or Bash in a read-only mode), no `git_commit`. The agent can read files, search code, and reason about its prior work, but cannot modify the project. This is the critical constraint: interrogation does not produce side effects.
- **Gating new work through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.
- **UI integration** — the session chat appears in the same chat tray used for standalone conversation, but with a visual indicator that this is a task session (job color, task reference) and that it is read-only.

**Second-order effects:** This changes how the operator relates to completed work. Instead of treating outcomes as final artifacts to accept or reject, the user can interrogate the reasoning, build understanding, and make better decisions about what to dispatch next. It also provides a natural feedback channel — the user's questions reveal what information the agent should have surfaced proactively, which directly informs dashboard design when that comes.

## Priority 2: Platform Tool Governance

**The problem:** The internal MCP tool surface is currently all-or-nothing — every agent gets every internal tool. The user can control CLI tools via `allowed_tools` and external MCP servers via `mcp_servers`, but internal tools (`git_commit`, `git_diff`, `git_log`, `git_status`, `list_files`, `read_file`, `list_jobs`) are always present. Meanwhile, the tool surface itself is too narrow for emerging use cases — there are no branch management operations, which blocks the orchestrating-job pattern where a coordinator creates a branch, dispatches work, and merges results.

**Why second:** Design is complete. This is the prerequisite for safe agent autonomy (P3). Before agents can coordinate and manage their own blast radius, the platform needs two things: a richer internal tool surface (branch operations) and per-job control over that surface. Expanding tools without governance is reckless; governance without the tools is academic. They ship together. Also enables P1's read-only restriction to be implemented cleanly — session interrogation's write-tool stripping is a special case of the general tool governance mechanism.

**Specifically:**
- **Internal tool control** — a new `allowed_internal_tools` job property selects which internal MCP tools the job's agent can access. When set, only the specified tools are presented. When empty, all internal tools are available (backward compatible). A read-only job might be restricted to `list_files`, `read_file`, `git_log`, `git_diff`. A writer job gets the full set.
- **Git branch tools** — three new internal MCP tools:
  - `git_branch_create` — create a new branch from a specified base (defaults to current HEAD). Enforced naming conventions (e.g., `<job-id>/<description>`).
  - `git_branch_switch` — switch the working directory to a named branch. The platform tracks which branch a task is operating on for audit purposes.
  - `git_branch_merge` — merge a source branch into the current branch. Surfaces merge conflicts as structured tool output rather than silent failures.
- **Branch audit trail** — branch operations are logged in the task session like all other MCP tool calls. The platform can reconstruct which branches a task created, switched to, and merged.
- **Three-dimensional tool composition** — `allowed_tools` (CLI), `allowed_internal_tools` (internal MCP), and `mcp_servers` (external MCP) compose into the complete tool surface at dispatch time. Each is independently configurable with "everything available" as default.

**Second-order effects:** These tools are the building blocks for the orchestrating-job pattern. Internal tool control also makes session interrogation's read-only mode a natural configuration rather than a special case — it's just an `allowed_internal_tools` restriction applied to the resumed session.

## Priority 3: Inter-Agent Coordination

**The problem:** Agents operate in isolation. They can trigger downstream work through commits and dependencies, but these are declarative, predetermined channels. There is no way for an agent to say "I found an issue the Engineer should address" or "the queue is backed up, I should skip non-critical work." The trigger system handles predictable cascades; agents need imperative tools for emergent coordination.

**Why third:** Design is complete. Depends on P2 — the orchestrating-job pattern requires both branch tools and dispatch tools, and coordination without governance would allow agents to dispatch unconstrained work on uncontrolled branches. P2 provides the blast-radius containment; P3 provides the coordination primitives that operate within those boundaries.

**Specifically:**
- **`dispatch_task` MCP tool** — allows an agent to enqueue a task for another job, with a message explaining why. Creates a trigger type `agent` attributed to the dispatching task
- **`get_queue_status` MCP tool** — read-only view of queue state. Gives agents situational awareness about what's pending, running, and backed up
- **`allowed_dispatch_targets` job property** — controls which other jobs an agent can dispatch. When empty, the agent cannot dispatch other jobs. Prevents unconstrained cross-agent triggering
- **Dispatch attribution** — tasks created by agents are tagged with the originating task, creating a provenance chain visible in history
- **Loop prevention** — no self-dispatch, depth limit on agent-initiated dispatch chains, coalescing absorbs redundant enqueues
- **Orchestration pattern** — with P2's branch tools and P3's dispatch tools, a coordinator job can: create a feature branch → dispatch scoped tasks on that branch → monitor queue for completion → merge the branch. This is the full vision of containerized feature sprints.

## Priority 4: Activity Dashboard

**The problem:** Understanding system behavior requires clicking through individual tasks. No aggregated view of health, performance, or patterns. The user manages agents but cannot see the forest.

**Why fourth:** Highest-leverage operator tool in isolation, but lacks a design specification. P1-P3 are fully designed with behavioral contracts — they can move straight to implementation. The dashboard needs a design pass through Designer and Architect before the Engineer can build it. By the time P1-P3 ship, the dashboard design can be developed in parallel, and the platform will have richer data to aggregate (branch operations, agent dispatch provenance, session interrogation patterns).

**Specifically:**
- **Summary panel** — success/fail/timeout counts per job over configurable time windows (today, 7d, 30d). Answers "how are my agents doing?" at a glance
- **Job health indicators** — surface recurring failures, timeout patterns. Highlight jobs that need attention without the user hunting through history
- **Timeline view** — when tasks ran, how long they took. Simple horizontal bars, no chart library. Reveals scheduling conflicts, long-running outliers, and idle gaps
- **Tool usage patterns** — which MCP tools each job uses most, derived from the existing audit trail. Helps the user understand agent behavior and identify tool configuration opportunities
- Data comes from `tasks` table plus MCP tool call logs — read-only aggregation, no schema changes

**Second-order effects:** The dashboard creates visibility that drives instruction tuning. When a user sees that the Engineer job fails 30% of the time on watch-triggered tasks, they know where to focus. By shipping after P1-P3, the dashboard can also surface branch operation patterns, agent dispatch chains, and interrogation frequency — richer signals than task success/failure alone.

## Deferred

Valuable but deliberately postponed:

- **Bash restriction** — structured MCP tools as the default, with Bash as a governed escape hatch. Requires the MCP tool surface to be comprehensive enough that agents don't need Bash for routine operations. P2 (tool governance) and P3 (coordination) will expand the tool surface, making this more viable afterward.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, branch management, concurrent MCP sessions). P2's branch tools are a prerequisite — parallel dispatch on a single branch is unsafe. Revisit once branch management is proven and the dashboard reveals whether sequential processing is a real bottleneck.
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
- **External MCP Server Lifecycle** — full registration, connection, tool discovery, and per-job assignment.
- **Route Modularization** — separate routers, shared state module, Pydantic models.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Product Formalization** — DESIGN.md with requirements and constraints, architecture subsystem descriptions.
