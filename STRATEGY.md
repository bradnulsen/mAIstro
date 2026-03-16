# Strategy

## Current State

mAistro is a fully operational multi-agent orchestration platform. The coordination layer is complete: four trigger types (manual, commit, schedule, dependency), approval gates, retry/resume, timeout enforcement, coalescing, and a queue-first dispatch model. The codebase is ~5K lines across backend and frontend, cleanly modularized with extracted routers, shared state, and a definitive schema. Product requirements and constraints are formalized in DESIGN.md; architecture is documented across ten subsystem descriptions.

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
- **Tool control** — per-task `base_tools`, `disallowed_tools`, and `mcp_servers` properties; external MCP server registration

## Diagnosis

The automation layer works. Tasks dispatch, agents commit, dependencies cascade. But the agents themselves operate as black boxes with unconstrained tool access. The platform dispatches reliably — the problem is what happens *inside* the dispatch.

Today, agents invoke tools through the Claude CLI's built-in capabilities — Bash, file operations, git — with no platform-level mediation. The platform can restrict the tool *list* (via `base_tools` / `disallowed_tools`), but it cannot observe, shape, or control what those tools actually do. An agent's git commit is indistinguishable from a human's. A Bash invocation is invisible until it shows up in the NDJSON stream after the fact.

This is the accountability gap. The platform orchestrates work but cannot enforce *how* work gets done. And without that enforcement, scaling up agents means scaling up trust assumptions.

## Guiding Policy

**Control at the point of action, not observation after the fact.** Instead of building dashboards to understand what agents did (observability-first), we build infrastructure that shapes what agents *can* do (accountability-first). Observability becomes a natural byproduct — when operations flow through a controlled pipeline, logging is trivial.

The mechanism: an **internal MCP server** that the platform hosts and agents connect to. This server is context-aware — it presents different tools to different tasks based on their configuration. It replaces unconstrained CLI tool access with platform-mediated operations where every action is observable, auditable, and policy-governed.

This is a concentration bet. It advances multiple goals simultaneously: accountability (every operation is mediated), observability (every operation is logged), inter-task coordination (tasks can queue other tasks), and quality control (deterministic tool behavior replaces open-ended Bash).

## Priority 1: Internal MCP Server — Platform-Mediated Tool Infrastructure

**The problem:** Agents operate through the Claude CLI's native tools (Bash, file I/O, git) with no platform-level mediation. The platform controls *when* agents run but not *how* they work. Tool invocations are opaque — visible in the NDJSON stream but not governed, shaped, or auditable at the platform level.

**Why first:** This is the highest-leverage change on the roadmap. It creates the infrastructure layer that every future accountability, observability, and coordination feature builds on. An MCP server that mediates agent operations gives us:
- **Observable operations** — every tool call flows through platform code, making logging and metrics trivial
- **Context-aware tooling** — different tasks get different tool surfaces based on their configuration. A Strategist doesn't need Bash; an Engineer doesn't need queue controls
- **Deterministic control** — platform-defined tools with precise behavior, replacing open-ended Bash for operations that should be structured (git commits, file reads within subscription scope, queue management)
- **Policy enforcement** — the server can enforce invariants (e.g., agents only modify files within their subscription patterns, commit messages follow the task's tag format)

**Specifically:**
- **mAistro hosts an MCP server** that is started alongside the backend and registered with dispatched CLI sessions. The server runs as a sidecar to the FastAPI app (or within the same process via stdio bridge)
- **Git operations as MCP tools** — `git_commit`, `git_diff`, `git_log`, `git_status` as structured tools with platform-enforced conventions (commit author, message format, allowed paths). This replaces agents shelling out to `git` via Bash
- **Context-aware tool surface** — the server reads the dispatching task's configuration and presents only relevant tools. Task properties (`base_tools`, subscription patterns, a new `mcp_tools` property) control what the server offers
- **Tool invocation logging** — every MCP tool call is recorded as a structured event in the dispatch session, creating an audit trail richer than NDJSON parsing
- **Phase 1 scope**: git tools + read-only project context tools (list files, read file, get task info). Bash restriction is *not* in phase 1 — agents keep Bash access but gain structured alternatives they'll prefer. Phase 1 proves the architecture

**Second-order effects:** Once agents operate through MCP tools, several deferred features become straightforward: cost tracking (tool calls are countable), structured error types (tool-level errors are typed), and dispatch evaluation (tool call patterns are analyzable).

## Priority 2: Queue Tools — Inter-Task Coordination

**The problem:** Tasks cannot communicate with or trigger other tasks except through the existing trigger system (commit-watch, dependency, schedule). There's no way for an agent to say "I found an issue that the Engineer should address" or "this needs Architect review before I proceed." The only coordination primitive is git commits triggering subscription-matched dispatches.

**Why second:** This is the first *capability* built on the MCP infrastructure from P1. It transforms the platform from a dispatch-and-forget system into one where agents actively coordinate. It also provides an immediate, visceral demonstration that the MCP architecture works — agents using platform tools to orchestrate other agents.

**Specifically:**
- **`dispatch_task` MCP tool** — allows an agent to enqueue a dispatch for another task, with a message explaining why. Creates a `manual`-like trigger attributed to the dispatching task
- **`get_queue_status` MCP tool** — read-only view of the queue (what's pending, what's running, what recently completed). Gives agents situational awareness
- **Task-level access control** — a task property controls which tasks an agent can dispatch. Not every agent should be able to trigger every other agent
- **Dispatch attribution** — queue entries created by agents are tagged with the originating task and dispatch, creating a provenance chain

**Design consideration:** This is the first place where agent actions have *platform-level side effects* beyond git commits. The MCP server must validate permissions and prevent loops (task A dispatches B dispatches A). Start with simple safeguards: no self-dispatch, depth limit on dispatch chains, and the existing coalescing logic absorbs redundant enqueues.

## Priority 3: Activity Dashboard

**The problem:** Understanding what agents accomplished requires clicking through individual dispatches. No aggregated view of system health, agent performance, or operational patterns. As dispatch volume grows with dependencies, scheduling, and now inter-task coordination, users lose the thread.

**Why third:** With P1 and P2 in place, there's significantly richer data to display — not just dispatch success/fail, but tool call patterns, coordination events, and policy violations. Building the dashboard after the MCP infrastructure means building it once with the right data model, rather than retrofitting.

**Specifically:**
- **Summary panel** — success/fail/timeout counts per task over configurable time windows (today, 7d, 30d)
- **Task health indicators** — surface recurring failures, timeout patterns, common error types
- **Tool usage patterns** — which tools each task uses most, which operations flow through MCP vs. native CLI (this data only exists after P1)
- **Coordination graph** — which tasks dispatch which other tasks, how dependency chains flow (this data only exists after P2)
- **Timeline view** — when dispatches ran, how long they took. Simple horizontal bars, no chart library
- Data comes from `dispatch_queue` plus the new MCP tool call log — read-only aggregation, no schema changes to core tables

## Priority 4: Dispatch Evaluation

**The problem:** No feedback loop on agent output quality. A dispatch completes, commits are made, but there's no mechanism to assess whether the work was good or whether task instructions need tuning.

**Why fourth:** With the MCP audit trail (P1), coordination events (P2), and dashboard (P3), evaluation has rich signals to work with. Tool call patterns, commit quality, downstream task reactions — all become inputs to evaluation that don't exist today.

**Specifically:**
- **Dispatch rating** — thumbs up/down on completed dispatches, stored in dispatch_queue
- **Outcome summary** — auto-generated summary of what the dispatch changed (diff stat + commit messages), shown in queue list
- **Prompt effectiveness tracking** — correlate task instruction changes with dispatch success rates
- Start with manual rating; auto-evaluation (LLM judges output quality) is a future extension

## Deferred

Valuable but not blocking the current phase:

- **Bash restriction** — moving from open-ended Bash access to structured MCP tools as the default path for agent operations. As MCP tools mature and cover more operational needs, agents will prefer their precision and auditability. This is the natural end state of the accountability story but requires P1 to be comprehensive. Restricting too early breaks agents before they have viable alternatives.
- **Parallel dispatch** — concurrent execution via git worktrees. High complexity (merge conflicts, branch management, cleanup on failure), and the sequential constraint is documented in DESIGN.md. The MCP server architecture actually makes this harder (server must handle concurrent sessions with different tool contexts). Defer until sequential processing is a proven bottleneck, not a theoretical one.
- **Conditional dependencies** — "only run if upstream output matches X." Useful for branching workflows but adds significant complexity to the trigger model.
- **Richer dependency context** — upstream dispatch output summary injected into downstream context.
- **Multi-project orchestration** — cross-project dispatch. Needs careful design around DB isolation and global `PROJECT_DIR` state.
- **Cost tracking** — token usage per dispatch. Becomes feasible once MCP tool calls are logged (tool calls are the unit of cost), but Claude CLI doesn't expose token counts cleanly yet.
- **Structured error types** — error classification becomes natural once tool operations are platform-mediated, but wait for P1 to land first.

## Non-Goals

- **TypeScript migration** — the frontend is small. Type safety isn't the bottleneck.
- **State management library** — useState/useEffect is sufficient at this scale.
- **Test suite** — codebase is small and changing fast. Tests would slow iteration without proportional value.
- **Electron/Tauri packaging** — Vite + Python works for the target user.
- **Custom agent runtime** — Claude CLI subprocess model works. The MCP server extends it rather than replacing it.
- **Full Bash removal** — agents need escape hatches. The goal is structured alternatives that agents prefer, not a sandbox that breaks them.

## Completed

- **Approval Gates** (was P1) — per-task `require_approval` property, `dispatch_queue.approval` column (null/pending/approved/rejected), manual dispatch bypasses, queue UI with pending indicator and approve/reject actions.
- **Project Switch Safety** — block project switch/close during active dispatch.
- **Schema Cleanup** — stripped backward compatibility, consolidated to definitive schema with no migrations or legacy fallbacks.
- **Trigger Context Migration** — `dispatch_queue.triggers` JSON array replaces flat columns. Each entry has `trigger`, `detail`, `context`.
- **Task Dependencies** — `depends_on` JSON property, `dependency` trigger type with same-type coalescing, auto-enqueue on upstream completion, circular dependency prevention.
- **Watch Semantics Cleanup** — removed `watch_enabled` toggle; subscriptions presence = watch active.
- **Settings and Configuration UI** — queue behavior, default model, default timeout, MCP server management.
- **Route Modularization** — separate routers, shared state module, Pydantic models throughout.
- **Dispatch Diff View** — `start_commit`/`result_commit` with inline diff display.
- **Dispatch Continuity** — resume via `--resume`, retry as re-enqueue.
- **Dispatch Timeout Enforcement** — per-task timeout, watchdog, graceful terminate + kill.
- **Product Formalization** — DESIGN.md with requirements and constraints. Architecture descriptions across ten subsystems.
