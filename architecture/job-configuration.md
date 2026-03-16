# Job Configuration

A job is a named, persistent configuration of autonomous work. The job configuration system manages job identity, properties, ordering, and inter-job dependency validation.

## Job Identity

Each job has a human-facing `name` and a system-facing `id` derived via `slugify()` — lowercase, non-alphanumeric characters replaced with hyphens. The slug is the primary key in the database and the identifier used across all APIs, task records, and git authorship (`<job-id>@maistro.local`).

Job identity is immutable after creation — the name can be updated, but the id (derived from the original name) does not change. This means renaming a job in the UI updates the display name only.

## Property System

Job behavior is configured entirely through the EAV property system (see [Storage](storage.md)). The property registry defines these core properties:

| Property | Type | Default | Purpose |
|----------|------|---------|---------|
| `description` | string | `""` | Short reference label shown in UI and job manifests |
| `instructions` | string | `""` | Detailed prompt body — the job's behavioral specification |
| `model` | string | `"sonnet"` | Claude model identifier for CLI invocation |
| `subscriptions` | json | `[]` | Glob patterns for watch triggers and context injection |
| `depends_on` | json | `[]` | List of upstream job IDs for dependency triggers |
| `schedule` | string | `""` | Cron expression for scheduled dispatch |
| `timeout` | integer | `900` | Maximum execution time in seconds |
| `require_approval` | boolean | `false` | Whether automated triggers require human approval |
| `coalesce_dispatches` | boolean | `false` | Global coalescing — never more than one pending task |
| `base_tools` | json | `[]` | Allowed tools — CLI tools the agent can use without permission prompting |
| `disallowed_tools` | json | `[]` | Disallowed tools — CLI tools hidden from the agent entirely (inverse of allowed) |
| `mcp_servers` | json | `[]` | External MCP servers to enable |
| `sort_order` | integer | `0` | Explicit ordering in the job list |

Properties are read with defaults applied — a job with no overrides gets all default values. The update path (`update_task`) accepts a partial set of properties and writes only the provided keys as overrides.

## Ordering

Jobs have an explicit `sort_order` property controlled by drag-to-reorder in the UI. The `reorder` endpoint accepts an ordered list of job IDs and sets `sort_order = index` for each. `list_tasks()` sorts by this value.

## Dependency Validation

Jobs declare upstream dependencies via `depends_on`. The system prevents:

- **Self-dependency**: rejected at the API layer before any graph analysis

Circular dependency chains are permitted. When job A depends on job B and vice versa, each task completion triggers the other — but coalescing absorbs redundant triggers. A job that already has a pending task absorbs the new dependency trigger rather than creating unbounded queue growth. The combination of coalescing and sequential execution makes cycles safe without requiring graph analysis at configuration time.

## CRUD Operations

- **Create**: slugifies name → inserts into `tasks` table → writes any initial properties → returns full job
- **Read**: loads job row + all property defs (with defaults) + job-specific overrides + derived running status
- **Update**: updates name if provided, then upserts property overrides
- **Delete**: cascades — explicitly deletes chat sessions and task records, then deletes the job (which cascades to `task_properties`)
- **List**: fetches all jobs, batch-queries running status to avoid N+1, sorts by `sort_order`

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) reads job properties at dispatch time to determine model, timeout, tools, and approval requirements
- [Trigger System](trigger-system.md) reads `subscriptions`, `schedule`, `depends_on`, and `coalesce_dispatches` to determine when and how to enqueue tasks
- [Prompt Assembly](prompt-assembly.md) reads `instructions`, `description`, and `subscriptions` to build the agent's prompt
- The job registry (manifest) is built from all jobs' names, descriptions, and subscriptions — injected into every task prompt so agents know their neighbors
