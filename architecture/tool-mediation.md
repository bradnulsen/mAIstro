# Tool Mediation

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call flows through platform code, making actions observable, auditable, and policy-governed.

## Purpose

Without mediation, agents interact with the project through unstructured shell commands (via the CLI's Bash tool). The internal MCP server provides structured alternatives with enforced conventions, audit trails, and per-job access control. Agents retain access to native CLI tools (subject to tool scoping via `--allowedTools` and `--disallowedTools` flags) alongside MCP tools — mediated tools are preferred alternatives, not an exclusive replacement.

## Architecture

The internal server runs as part of the backend process and is connected to the Claude CLI at invocation time. Each task gets a server instance configured for the dispatching job — the tool surface is determined by job configuration, not by agent choice.

### Context-Aware Tool Surfaces

The server reads the dispatching job's configuration (properties, subscriptions) and presents only relevant tools. Different jobs get different tool surfaces based on:

- **Job properties**: `allowed_tools`, `disallowed_tools`, and `mcp_servers` shape the available tool set
- **Subscriptions**: glob patterns scope which files and paths are relevant to the job

This means two jobs dispatched in sequence may see entirely different tool inventories from the same internal server.

### Dual-Flag Tool Scoping

The Claude CLI provides two complementary flags for tool access control:

- **`--allowedTools`** — tools the agent can use without permission prompting. This is a *permission* flag, not a visibility flag.
- **`--disallowedTools`** — tools that are **hidden** from the agent entirely. The agent cannot see, invoke, or reason about disallowed tools. This is the enforcement mechanism for invisible restrictions.

These flags are inverses: `allowed + disallowed = all tools`. When the platform sets `allowed_tools` on a job, it computes the complement and passes `--disallowedTools` to the CLI to hide excluded tools.

This distinction matters because the platform runs with `--dangerously-skip-permissions`. In this mode, `--allowedTools` alone does not restrict tool access — all permission checks are bypassed. **`--disallowedTools` is the only mechanism that actually removes tools from the agent's view**, regardless of permission mode.

The user-facing model remains whitelist-only: the user defines what a job *can* do via `allowed_tools` (and `disallowed_tools` as its explicit complement). The agent sees only tools in its allowed set — restrictions are invisible. No prompt instructions mention excluded tools, and the agent has no awareness they exist.

## Tool Categories

### Git Operations

Structured tools for git interaction with enforced conventions:

- **`git_commit`** — commits with enforced authorship (`<JobName> <<job-id>@maistro.local>`) and message format (`[JobName] description`). Path restrictions can limit which files a job is allowed to commit.
- **`git_diff`** — returns structured diff output for specified paths or the working tree
- **`git_log`** — returns commit history with configurable depth and format
- **`git_status`** — returns working tree status

These replace unmediated shell-based git access. The key difference is enforcement: an agent using `git_commit` through the MCP server cannot bypass authorship conventions or commit message formatting that the platform requires.

### Read-Only Project Context

Tools for querying project state, scoped by the job's subscriptions and configuration:

- **File listing** — enumerate files in the project, potentially filtered by subscription globs
- **File reading** — read file contents, with awareness of which files the job subscribes to
- **Job information** — retrieve information about other jobs in the project (names, descriptions, states)

These tools provide structured access to the same information agents could get through shell commands, but with consistent formatting and subscription-aware scoping.

## Tool Invocation Logging

Every MCP tool call is recorded as a structured event in the task's chat session. This creates an audit trail that captures:

- Which tool was called
- What input was provided
- What the tool returned
- When the call occurred

This is richer than parsing tool use from the NDJSON stream — the platform records the actual operation it performed, not just the agent's request. The audit trail integrates with the existing `chat_events` storage (see [Streaming and Sessions](streaming-and-sessions.md)).

## Relationship to External MCP Servers

The internal MCP server is distinct from external MCP servers:

- **Internal**: hosted by the platform, context-aware, policy-governed, audit-logged. Provides git operations and project context tools. Always connected — not subject to per-job configuration.
- **External**: registered globally in Settings (`mcp_servers` table), then selectively enabled per-job via the job's `mcp_servers` property. These are opaque to the platform — it connects the agent to them but does not mediate their tool calls. A server must be registered and enabled at the platform level before any job can use it.

Both are connected to the CLI at invocation time.

## Relationship to Other Systems

- [CLI Bridge](cli-bridge.md) connects the agent to the internal MCP server at subprocess invocation
- [Dispatch Engine](dispatch-engine.md) configures the server instance per-task based on job properties
- [Streaming and Sessions](streaming-and-sessions.md) stores tool invocation events as part of the audit trail
- [Job Configuration](job-configuration.md) provides the properties that shape each job's tool surface
- [Storage](storage.md) holds `mcp_servers` table for external server registrations
