# Tool Mediation

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call flows through platform code, making actions observable, auditable, and policy-governed.

## Purpose

Without mediation, agents interact with the project through unstructured shell commands (via the CLI's Bash tool). The internal MCP server provides structured alternatives with enforced conventions, audit trails, and per-task access control. Agents retain access to native CLI tools alongside MCP tools — mediated tools are preferred alternatives, not a restriction.

## Architecture

The internal server runs as part of the backend process and is connected to the Claude CLI at invocation time. Each dispatch gets a server instance configured for the dispatching task — the tool surface is determined by task configuration, not by agent choice.

### Context-Aware Tool Surfaces

The server reads the dispatching task's configuration (properties, subscriptions) and presents only relevant tools. Different tasks get different tool surfaces based on:

- **Task properties**: `base_tools`, `disallowed_tools`, and `mcp_servers` shape the available tool set
- **Subscriptions**: glob patterns scope which files and paths are relevant to the task

This means two tasks dispatched in sequence may see entirely different tool inventories from the same internal server.

## Tool Categories

### Git Operations

Structured tools for git interaction with enforced conventions:

- **`git_commit`** — commits with enforced authorship (`<TaskName> <<task-id>@maistro.local>`) and message format (`[TaskName] description`). Path restrictions can limit which files a task is allowed to commit.
- **`git_diff`** — returns structured diff output for specified paths or the working tree
- **`git_log`** — returns commit history with configurable depth and format
- **`git_status`** — returns working tree status

These replace unmediated shell-based git access. The key difference is enforcement: an agent using `git_commit` through the MCP server cannot bypass authorship conventions or commit message formatting that the platform requires.

### Read-Only Project Context

Tools for querying project state, scoped by the task's subscriptions and configuration:

- **File listing** — enumerate files in the project, potentially filtered by subscription globs
- **File reading** — read file contents, with awareness of which files the task subscribes to
- **Task information** — retrieve information about other tasks in the project (names, descriptions, states)

These tools provide structured access to the same information agents could get through shell commands, but with consistent formatting and subscription-aware scoping.

## Tool Invocation Logging

Every MCP tool call is recorded as a structured event in the dispatch's chat session. This creates an audit trail that captures:

- Which tool was called
- What input was provided
- What the tool returned
- When the call occurred

This is richer than parsing tool use from the NDJSON stream — the platform records the actual operation it performed, not just the agent's request. The audit trail integrates with the existing `chat_events` storage (see [Streaming and Sessions](streaming-and-sessions.md)).

## Relationship to External MCP Servers

The internal MCP server is distinct from external MCP servers:

- **Internal**: hosted by the platform, context-aware, policy-governed, audit-logged. Provides git operations and project context tools.
- **External**: user-registered servers (`mcp_servers` table) that extend agent capabilities. These are opaque to the platform — it connects the agent to them but does not mediate their tool calls.

Both are connected to the CLI at invocation time. The task's `mcp_servers` property controls which external servers are enabled for that task.

## Relationship to Other Systems

- [CLI Bridge](cli-bridge.md) connects the agent to the internal MCP server at subprocess invocation
- [Dispatch Engine](dispatch-engine.md) configures the server instance per-dispatch based on task properties
- [Streaming and Sessions](streaming-and-sessions.md) stores tool invocation events as part of the audit trail
- [Task Configuration](task-configuration.md) provides the properties that shape each task's tool surface
- [Storage](storage.md) holds `mcp_servers` table for external server registrations
