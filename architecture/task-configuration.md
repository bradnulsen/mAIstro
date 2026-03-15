# Task Configuration

A task is a named, configurable unit of autonomous work. The task configuration system manages task identity, properties, ordering, and inter-task dependency validation.

## Task Identity

Each task has a human-facing `name` and a system-facing `id` derived via `slugify()` — lowercase, non-alphanumeric characters replaced with hyphens. The slug is the primary key in the database and the identifier used across all APIs, dispatch records, and git authorship (`<task-id>@maistro.local`).

Task identity is immutable after creation — the name can be updated, but the id (derived from the original name) does not change. This means renaming a task in the UI updates the display name only.

## Property System

Task behavior is configured entirely through the EAV property system (see [Storage](storage.md)). The property registry defines these core properties:

| Property | Type | Default | Purpose |
|----------|------|---------|---------|
| `description` | string | `""` | Short reference label shown in UI and task manifests |
| `instructions` | string | `""` | Detailed prompt body — the task's behavioral specification |
| `model` | string | `"sonnet"` | Claude model identifier for CLI invocation |
| `subscriptions` | json | `[]` | Glob patterns for watch triggers and context injection |
| `depends_on` | json | `[]` | List of upstream task IDs for dependency triggers |
| `schedule` | string | `""` | Cron expression for scheduled dispatch |
| `timeout` | integer | `900` | Maximum execution time in seconds |
| `require_approval` | boolean | `false` | Whether automated triggers require human approval |
| `coalesce_dispatches` | boolean | `false` | Global coalescing — never more than one pending dispatch |
| `base_tools` | json | `[]` | Allowed tools whitelist for CLI |
| `disallowed_tools` | json | `[]` | Disallowed tools blacklist for CLI |
| `mcp_servers` | json | `[]` | External MCP servers to enable |
| `sort_order` | integer | `0` | Explicit ordering in the task list |

Properties are read with defaults applied — a task with no overrides gets all default values. The update path (`update_task`) accepts a partial set of properties and writes only the provided keys as overrides.

## Ordering

Tasks have an explicit `sort_order` property. The `reorder` endpoint accepts an ordered list of task IDs and sets `sort_order = index` for each. `list_tasks()` sorts by this value.

## Dependency Validation

Tasks declare upstream dependencies via `depends_on`. The system prevents:

- **Self-dependency**: rejected at the API layer before any graph analysis

Circular dependency chains are permitted. When task A depends on task B and vice versa, each completion triggers the other — but coalescing absorbs redundant triggers. A task that is already pending absorbs the new dependency trigger rather than creating unbounded queue growth. The combination of coalescing and sequential execution makes cycles safe without requiring graph analysis at configuration time.

## CRUD Operations

- **Create**: slugifies name → inserts into `tasks` → writes any initial properties → returns full task
- **Read**: loads task row + all property defs (with defaults) + task-specific overrides + derived running status
- **Update**: updates name if provided, then upserts property overrides
- **Delete**: cascades — explicitly deletes chat sessions and dispatch records, then deletes the task (which cascades to `task_properties`)
- **List**: fetches all tasks, batch-queries running status to avoid N+1, sorts by `sort_order`

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) reads task properties at dispatch time to determine model, timeout, tools, and approval requirements
- [Trigger System](trigger-system.md) reads `subscriptions`, `schedule`, `depends_on`, and `coalesce_dispatches` to determine when and how to enqueue
- [Prompt Assembly](prompt-assembly.md) reads `instructions`, `description`, and `subscriptions` to build the agent's prompt
- The task registry (manifest) is built from all tasks' names, descriptions, and subscriptions — injected into every dispatch so agents know their neighbors
