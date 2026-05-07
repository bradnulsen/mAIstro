# Job Configuration

A job is a named, persistent configuration of autonomous work. The job configuration system manages job identity, properties, ordering, and inter-job dependency validation.

## Job Identity

Each job has three identity fields:

- **`id`** — `INTEGER PRIMARY KEY AUTOINCREMENT`. The stable identifier used across all foreign keys (`tasks.job_id`, `job_properties.job_id`, `chat_sessions.job_id`), API routes, and internal references.
- **`slug`** — derived from the original name via `slugify()` (lowercase, non-alphanumeric characters replaced with hyphens). `UNIQUE` column. Used for git authorship (`<slug>@maistro.local`) and branch naming (`<slug>/<description>`). Immutable after creation.
- **`name`** — human-facing display name. Can be updated; renaming a job in the UI changes the display name only, not the slug or integer ID.

## Property System

Job behavior is configured entirely through the EAV property system (see [Storage](storage.md)). The property registry defines these core properties:

| Property | Type | Default | Purpose |
|----------|------|---------|---------|
| `summary` | string | `""` | Short reference blurb — feeds the job manifest line and the job list UI |
| `description` | string | `""` | Long-form prose body — mission, scope, voice. The "north star" of what good output looks like. Concatenated into every dispatch prompt |
| `model` | string | `"sonnet"` | Claude model identifier for CLI invocation |
| `subscriptions` | json | `[]` | Glob patterns for watch triggers and context injection |
| `cascades_from` | json | `[]` | List of upstream job IDs for dependency triggers (DESIGN refers to this property as `depends_on` — the on-disk key is `cascades_from`) |
| `schedule` | string | `""` | Cron expression for scheduled dispatch |
| `timeout` | integer | `900` | Maximum execution time in seconds |
| `max_turns` | integer | `100` | Maximum agent turns per task — safety bound, not a target |
| `require_approval` | boolean | `false` | Whether automated triggers require human approval |
| `coalesce_tasks` | boolean | `false` | Global coalescing — never more than one pending task |
| `allowed_tools` | json | `[]` | CLI tools the agent can use, selected from the platform's discovered tool inventory. When set, the platform computes the complement and hides all other tools from the agent |
| `allowed_internal_tools` | json | `[]` | Internal MCP tools the agent can access. When set, only listed tools are presented by the internal server. When empty, all internal tools are available. Enables read-only jobs or selective capability grants |
| `allowed_dispatch_targets` | json | `[]` | Job IDs this agent can dispatch via `dispatch_task`. When empty, the agent cannot dispatch other jobs. Self-dispatch is always prohibited |
| `allow_learning_self_modification` | boolean | `false` | Whether the agent's `add_learning` / `update_learning` / `delete_learning` MCP tools are exposed for this job. Read-only `list_learnings` is always available. Agents can only edit or delete rows they themselves authored (`source='agent'`); operator-authored rows are never agent-writable |
| `mcp_servers` | json | `[]` | External MCP servers to enable, selected from registered servers |
| `sort_order` | integer | `0` | Explicit ordering in the job list |

Properties are read with defaults applied — a job with no overrides gets all default values. The update path (`update_job`) accepts a partial set of properties and writes only the provided keys as overrides.

### Pending Properties (DESIGN reset)

DESIGN's reset around closed-loop learning still has one structural surface not yet in the property registry. It will flow through the same EAV mechanism when it ships.

- **`color`** (string) — visual identifier assigned at creation from a curated palette. No behavioral semantics; renders the job's identity color across every surface (command bar indicator, queue cards, status pills). Operator-changeable via a hue picker. The 10 default palette tokens (`--job-color-0` … `--job-color-9`) already exist in [App.css](frontend/src/App.css) — adding the property is what wires the value into job records.

## Ordering

Jobs have an explicit `sort_order` property controlled by drag-to-reorder in the UI. The `reorder` endpoint accepts an ordered list of job IDs and sets `sort_order = index` for each. `list_jobs()` sorts by this value.

## Dependency Validation

Jobs declare upstream dependencies via `depends_on`. The system prevents:

- **Self-dependency**: rejected at the API layer before any graph analysis

Circular dependency chains are permitted. When job A depends on job B and vice versa, each task completion triggers the other — but coalescing absorbs redundant triggers. A job that already has a pending task absorbs the new dependency trigger rather than creating unbounded queue growth. The combination of coalescing and sequential execution makes cycles safe without requiring graph analysis at configuration time.

## CRUD Operations

- **Create**: slugifies name → inserts into `jobs` table → writes any initial properties → returns full job
- **Read**: loads job row + all property defs (with defaults) + job-specific overrides + derived running status
- **Update**: updates name if provided, then upserts property overrides
- **Delete**: cascades — explicitly deletes chat sessions and task records, then deletes the job (which cascades to `job_properties`)
- **List**: fetches all jobs, batch-queries running status to avoid N+1, sorts by `sort_order`

## Relationship to Other Systems

- [Dispatch Engine](dispatch-engine.md) reads job properties at dispatch time to determine model, timeout, tools, and approval requirements
- [Trigger System](trigger-system.md) reads `subscriptions`, `schedule`, `cascades_from`, and `coalesce_tasks` to determine when and how to enqueue tasks
- [Prompt Assembly](prompt-assembly.md) reads `summary`, `description`, `subscriptions`, and enabled `job_learnings` rows to build the agent's prompt
- [Tool Mediation](tool-mediation.md) uses `allowed_tools`, `allowed_internal_tools`, and `mcp_servers` to compose each job's tool surface at dispatch time. `allow_learning_self_modification` extends this composition to gate learning write tools
- The job registry (manifest) is built from all jobs' names, summaries, and subscriptions — injected into every task prompt so agents know their neighbors
