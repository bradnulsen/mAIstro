# Tool Mediation

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call flows through platform code, making actions observable, auditable, and policy-governed.

## Purpose

Without mediation, agents interact with the project through unstructured shell commands (via the CLI's Bash tool). The internal MCP server provides structured alternatives with enforced conventions, audit trails, and per-job access control. Agents retain access to native CLI tools (subject to tool scoping — see below) alongside MCP tools — mediated tools are preferred alternatives, not an exclusive replacement.

## Architecture

The internal server runs as part of the backend process and is connected to the Claude CLI at invocation time. Each task gets a server instance configured for the dispatching job — the tool surface is determined by job configuration, not by agent choice.

### Context-Aware Tool Surfaces

The server reads the dispatching job's configuration (properties, subscriptions) and presents only relevant tools. Different jobs get different tool surfaces based on:

- **Job properties**: `allowed_tools`, `allowed_internal_tools`, and `mcp_servers` shape the available tool set
- **Subscriptions**: glob patterns scope which files and paths are relevant to the job

This means two jobs dispatched in sequence may see entirely different tool inventories from the same internal server.

### Opt-In Tool Scoping

Tool configuration is opt-in only. Three independent properties define what a job *can* do:

- **`allowed_tools`** — selects CLI tools (e.g. `Read`, `Edit`, `Bash`)
- **`allowed_internal_tools`** — selects which internal MCP tools are presented (e.g. `git_commit`, `list_files`)
- **`mcp_servers`** — selects which external MCP servers are connected

There are no complementary "disallowed" properties. The platform computes the inverse internally.

For CLI tools, the platform passes tools not in the allowed set via `--disallowedTools`, which removes them from the agent's environment entirely. For internal MCP tools, the server simply omits tools not in the `allowed_internal_tools` set from its tool list. When a property is empty (no selection), all tools of that type are available — backward compatible.

This matters because the platform runs with `--dangerously-skip-permissions`. In this mode, `--allowedTools` alone does not restrict tool access — all permission checks are bypassed. `--disallowedTools` is the only mechanism that actually removes CLI tools from the agent's view, regardless of permission mode.

This is not a stylistic choice — it follows from headless execution. A headless agent cannot ask for permissions, cannot negotiate tool access, cannot meaningfully be told what it cannot do. The only coherent model is to present exactly the tools the agent can use and nothing else. The platform owns the restriction surface; the agent owns only its allowed capabilities.

## Tool Categories

### Git Operations

Structured tools for git interaction with enforced conventions:

- **`git_commit`** — commits with enforced authorship (`<JobName> <<slug>@maistro.local>`) and message format (`[JobName] description`). Path restrictions can limit which files a job is allowed to commit.
- **`git_diff`** — returns structured diff output for specified paths or the working tree
- **`git_log`** — returns commit history with configurable depth and format
- **`git_status`** — returns working tree status

These replace unmediated shell-based git access. The key difference is enforcement: an agent using `git_commit` through the MCP server cannot bypass authorship conventions or commit message formatting that the platform requires.

### Git Branch Operations

Structured tools for branch management:

- **`git_branch_create`** — creates a new branch from a specified base. Enforces naming conventions (e.g. `<slug>/<description>`) to prevent namespace collisions between jobs.
- **`git_branch_switch`** — switches the working directory to a named branch. The platform tracks which branch a task operates on for audit purposes.
- **`git_branch_merge`** — merges a source branch into the current branch. Merge conflicts surface as structured tool output rather than silent failures.

Branch operations are logged identically to other MCP tool calls — the platform can reconstruct which branches a task created, switched to, and merged.

### Read-Only Project Context

Tools for querying project state, scoped by the job's subscriptions and configuration:

- **File listing** — enumerate files in the project, potentially filtered by subscription globs
- **File reading** — read file contents, with awareness of which files the job subscribes to
- **Job information** — retrieve information about other jobs in the project (names, descriptions, states)

These tools provide structured access to the same information agents could get through shell commands, but with consistent formatting and subscription-aware scoping.

### Inter-Agent Coordination

Tools for cross-agent awareness and imperative dispatch:

- **`dispatch_task`** — enqueues a task for another job with a message explaining why, creating an `agent` trigger (see [Trigger System — Agent](trigger-system.md)). The dispatching task's identity is tagged on the new task for provenance. Subject to the job's `allowed_dispatch_targets` — the agent can only dispatch jobs explicitly listed in its configuration. Self-dispatch is prohibited.
- **`get_queue_status`** — read-only view of current queue state: what's pending, running, and backed up. Gives agents situational awareness beyond their own execution context.

These tools give agents imperative coordination beyond the declarative trigger system (dependencies, subscriptions). Agent dispatch is the only mechanism where one agent can directly cause another to run — all other cross-agent triggers flow through git commits or configuration.

## Tool Invocation Logging

Every MCP tool call is recorded as a structured event in the task's chat session. This creates an audit trail that captures:

- Which tool was called
- What input was provided
- What the tool returned
- When the call occurred

This is richer than parsing tool use from the NDJSON stream — the platform records the actual operation it performed, not just the agent's request. The audit trail integrates with the existing `chat_events` storage (see [Streaming and Sessions](streaming-and-sessions.md)).

## Tool Discoverability

The platform makes the full tool inventory visible and selectable so users configure from known options rather than guessing names.

Three tool sources, each with a discovery mechanism:

- **Built-in CLI tools** — the platform maintains a canonical set of CLI tool names (`CLI_NATIVE_TOOLS` in `cli.py`). These are the tools that `allowed_tools` selects from. The configuration surface presents them as a selectable inventory — the user picks from what exists rather than typing free-text names.
- **Internal MCP tools** — the platform defines these directly (`git_commit`, `git_diff`, `git_log`, `git_status`, `git_branch_create`, `git_branch_switch`, `git_branch_merge`, `list_files`, `read_file`, `list_jobs`, `dispatch_task`, `get_queue_status`). The internal server is always connected, but individual internal tools are subject to per-job selection via `allowed_internal_tools`. When no selection is made, all internal tools are available. The configuration surface presents these as a selectable inventory.
- **External MCP server tools** — when a registered external server is connected, the platform can discover its tool list via the MCP protocol. Discovered tools become visible alongside built-in tools in the per-job configuration surface.

The configuration surfaces for `allowed_tools` and `mcp_servers` present selectable options drawn from these inventories. Users select from what exists; they do not enter arbitrary text that may not correspond to real tools.

### Tool Surface Composition

During dispatch, the agent's available tools are the union of three independently filtered sources:

1. **CLI tools** selected via `allowed_tools` (or the full default set if none are selected)
2. **Internal MCP tools** selected via `allowed_internal_tools` (or all internal tools if none are selected)
3. **External MCP tools** from servers enabled for the job via `mcp_servers`

Each dimension is independently configurable, and the default for each is "everything available." The user can see this composed surface when configuring a job — what the agent will actually have access to.

The `allowed_internal_tools` property enables fine-grained control over internal capabilities. A read-only job can be restricted to `list_files`, `read_file`, `git_log`, `git_diff` — excluding `git_commit`, branch operations, and dispatch tools. An orchestrator job might get `dispatch_task` and `get_queue_status` while a leaf job does not.

## External MCP Servers

External MCP servers extend the tool surface beyond built-in CLI and internal platform tools. The platform manages their full lifecycle.

### Internal vs External

- **Internal**: hosted by the platform process, context-aware, policy-governed, audit-logged. Provides git operations and project context tools. Always connected — not subject to per-job configuration.
- **External**: registered globally in the MCP Servers view (`mcp_servers` table), opaque to the platform — it connects agents to them but does not mediate their tool calls.

### Lifecycle

**Registration** — external servers are registered at the platform level via the dedicated MCP Servers view. Each registration specifies: server name (primary key), command to launch, command arguments (structured list), and environment variables (key-value pairs). A registered server can be enabled or disabled globally — disabled servers are unavailable to any job regardless of per-job configuration.

**Registration Validation** — the platform validates server configuration at registration time. The command must be a resolvable executable — it must exist on `PATH` (resolved via `shutil.which()`) or be a valid absolute path. Arguments must parse as a structured list. Environment variables must be well-formed key-value pairs. Invalid registrations are rejected with specific error messages. This is an eager validation gate: invalid configuration never reaches the database.

**Environment Variables** — external MCP servers frequently require environment variables (API keys, configuration paths, service URLs). The registration stores env vars as a JSON object alongside the server's command and args. At launch time (both probing and dispatch), the platform merges server-specific env vars with the process environment and passes the combined set to the server subprocess. This keeps server configuration self-contained within the platform — the operator does not need to set env vars outside the platform for servers to function.

**Connection and Discovery** — the platform discovers external server capabilities through ephemeral probes. A probe spawns the server process, performs the MCP initialize/tools/list handshake over stdio, extracts the tool names, and terminates the process. This is a short-lived, stateless interaction — no persistent connection is maintained outside of dispatch.

**Module**: `backend/mcp_probe.py`

Probing serves two purposes:
- **Inventory enrichment** — the tool inventory endpoint probes all enabled servers in parallel and includes their discovered tools alongside CLI native and internal MCP tools. This gives the configuration surface a complete picture of what tools exist across all sources.
- **Health checking** — each probe returns a status (`ok`, `error`, or `disabled`) and, on failure, an error message. The MCP Servers view surfaces server health so the user knows which servers are reachable before assigning them to jobs.

Probes are on-demand — triggered by the inventory endpoint or by a dedicated per-server probe endpoint (`GET /api/mcp/servers/{name}/tools`). There is no background polling or persistent health monitoring. The probe timeout (10 seconds) bounds how long a misbehaving server can block the response.

If a server cannot be reached or fails the handshake, the platform reports the failure state. Discovered tools from healthy servers become visible in the per-job configuration surface alongside built-in tools.

**Per-Job Assignment** — a job's `mcp_servers` property controls which registered external servers connect during dispatch. The configuration surface presents registered servers as selectable options (not free-text). Only servers that are both registered and globally enabled appear as options. The per-job configuration surface shows each server's current health status so the user sees problems before dispatching.

**Pre-Dispatch Health Check** — before a task begins execution, the worker probes all external MCP servers assigned to the task's job. If any server is unreachable (command not found, timeout, handshake failure) or disabled, the task fails immediately with a specific error naming the server and the failure reason. This is a dispatch-time gate in the worker's execution flow — it runs after session creation and before CLI invocation. A task never runs with a silently missing tool surface.

**Error Attribution** — when an MCP server failure occurs (either at the pre-dispatch gate or during execution), the error identifies the server by name and describes the failure mode. Pre-dispatch failures produce a structured error on the task record naming the failing server. Runtime MCP errors from the CLI stream are enriched with server-specific context before being surfaced to the operator in the task detail view.

**Stale Reference Integrity** — when a server is deleted, the platform removes it from every job's `mcp_servers` property list. This is an atomic cascade: the deletion and reference cleanup happen in a single transaction. No job silently loses tools because it references a server that no longer exists. When a server is disabled, jobs referencing it see a visible warning on their configuration surface. Disabled servers are excluded from dispatch configuration unconditionally — the `enabled` flag is authoritative.

**Invocation** — at dispatch time (after the pre-dispatch health check passes), the MCP config builder (`mcp_config.py`) assembles a config file containing the internal server (always) plus any external servers enabled for the job. Each server entry includes its command, parsed args, and parsed env vars. This file is passed to the CLI via `--mcp-config`. Both internal and external servers are connected to the CLI at subprocess invocation time.

## Relationship to Other Systems

- [CLI Bridge](cli-bridge.md) connects the agent to the internal MCP server at subprocess invocation
- [Dispatch Engine](dispatch-engine.md) configures the server instance per-task based on job properties
- [Streaming and Sessions](streaming-and-sessions.md) stores tool invocation events as part of the audit trail
- [Job Configuration](job-configuration.md) provides the properties that shape each job's tool surface
- [Frontend](frontend.md) provides the MCP Servers view for registration and health management
- [Storage](storage.md) holds `mcp_servers` table for external server registrations
