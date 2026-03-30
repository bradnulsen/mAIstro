# Prompt Assembly

The prompt assembly system builds two prompts per task — a system prompt establishing execution behavior and a user prompt layering job-specific instructions with runtime context.

**Module**: `backend/dispatch.py` (prompt functions)

## System Prompt

The system prompt is a static template (`DISPATCH_SYSTEM_PROMPT`) interpolated with two values: the job name (for commit tag prefixes) and the project directory.

It establishes three behavioral contracts:

1. **Execution mode**: headless, autonomous, no questions or clarification requests. The agent makes decisions based on available context.
2. **Documentation principle**: files are first-principle representations of current state, not task lists. Reasoning belongs in commit messages.
3. **Git workflow**: commit with descriptive messages, use `[GoalName]` prefix, stage related changes together.

The system prompt is delivered via stdin wrapped in `<system-instructions>` tags. It is omitted on resume sessions (the CLI retains the original).

## User Prompt

The user prompt is assembled from multiple sections, concatenated with double newlines:

### 1. Job Identity
- Job name as a heading (`# Job: {name}`)
- Description (if present)

No dispatch mode label. The trigger type is communicated solely through the invocation context section, which carries the actual causal data. A generic label like "auto-dispatched (commit)" restates what the invocation context already says without adding information.

### 2. Instructions
The job's `instructions` property — the detailed behavioral specification written by the user. This is the core payload that defines what the agent does.

### 3. Invocation Context
Built from the task's context and any subordinate tasks' context (for coalesced tasks). Pre-formatted context strings (built at the enqueue site) are rendered as the invocation section. Duplicate lines are collapsed with a count suffix (e.g. `×3`).

This section answers "why am I running?" — commit hashes, schedule expressions, dependency completions, retry history, user notes. Each enqueue site formats its own context string with full causal detail:

- **Commit**: `` **Commit** `abc123de`: Add login feature ``
- **Schedule**: `` **Schedule** (`*/60 * * * *`) at abc123de ``
- **Dependency**: `` **Dependency** — triggered by completion of Engineer (task #42), commits abc123de..def456ab ``
- **Retry**: `` **Retry** of task #42: previous run failed: <error> — commits abc123de..def456ab ``
- **Resume**: `` **Resume** — continuing from task #42 ``
- **Manual**: `` **Manual** at abc123de `` (with optional user notes)

The invocation context is the **single source of trigger-type awareness** in the prompt. No other section restates or re-derives trigger semantics.

When multiple triggers have been coalesced into a single task, the section header includes a framing line: "Multiple triggers have been coalesced into this task (N items). Address them together." This tells the agent to treat the listed reasons as a unified scope rather than picking one.

### 4. Job Registry (Manifest)
A listing of all jobs in the project with their names, descriptions, and subscription patterns. This gives the agent awareness of its neighbors — useful for jobs that need to coordinate or understand the broader system.

Built by `build_goal_manifest()` which queries all jobs at dispatch time.

### 5. Subscribed Files
If the job has subscription glob patterns, they're resolved against the working tree. Matching files are listed with paths and sizes. The agent reads their contents via its tools as needed — the list is a pointer, not inline content.

### 6. Action Directive
A static closing section ("Your Turn") that applies universally to all trigger types:

```
## Your Turn
Review the project state — your instructions, subscriptions, and context above.
Identify what needs to be done and do it. If nothing needs updating, say so briefly.
```

This is intentionally trigger-agnostic. The invocation context (section 3) already carries all trigger-specific information — the closing directive should not re-derive it. A single static closer keeps business logic out of the prompt assembly layer and avoids the trap of restating what the context already says in weaker, generic prose.

## Context Immutability

Trigger context strings are built at the enqueue site (when the trigger fires), not at execution time. This preserves the causal record — the task's invocation section reflects the state that caused it to be created, even if time passes between enqueue and execution.

The `_build_queue_context()` function simply renders these pre-built strings. It does not query for new information.

## Relationship to Other Systems

- [CLI Bridge](cli-bridge.md) receives the assembled prompts via stdin
- [Job Configuration](job-configuration.md) provides instructions, description, and subscriptions
- [Trigger System](trigger-system.md) builds and stores the context strings consumed here
- [Git Integration](git-integration.md) resolves subscription globs to file lists
