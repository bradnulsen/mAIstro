# Prompt Assembly

The prompt assembly system builds two prompts per dispatch — a system prompt establishing execution behavior and a user prompt layering task-specific instructions with runtime context.

**Module**: `backend/dispatch.py` (prompt functions)

## System Prompt

The system prompt is a static template (`DISPATCH_SYSTEM_PROMPT`) interpolated with two values: the task name (for commit tag prefixes) and the project directory.

It establishes three behavioral contracts:

1. **Execution mode**: headless, autonomous, no questions or clarification requests. The agent makes decisions based on available context.
2. **Documentation principle**: files are first-principle representations of current state, not task lists. Reasoning belongs in commit messages.
3. **Git workflow**: commit with descriptive messages, use `[TaskName]` prefix, stage related changes together.

The system prompt is delivered via stdin wrapped in `<system-instructions>` tags. It is omitted on resume sessions (the CLI retains the original).

## User Prompt

The user prompt is assembled from multiple sections, concatenated with double newlines:

### 1. Task Identity
- Task name as a heading
- Description (if present)
- Dispatch mode: "manually dispatched" or "auto-dispatched ({trigger})"

### 2. Instructions
The task's `instructions` property — the detailed behavioral specification written by the user. This is the core payload that defines what the agent does.

### 3. Invocation Context
Built from the dispatch's `triggers` array. Each trigger entry's `context` field (pre-formatted at the enqueue site) is rendered as a bullet point. Duplicate lines are collapsed with a count suffix.

This section answers "why am I running?" — commit hashes, schedule expressions, dependency completions, retry history, user notes.

### 4. Task Registry (Manifest)
A listing of all tasks in the project with their names, descriptions, and subscription patterns. This gives the agent awareness of its neighbors — useful for tasks that need to coordinate or understand the broader system.

Built by `build_task_manifest()` which queries all tasks at dispatch time.

### 5. Subscribed Files
If the task has subscription glob patterns, they're resolved against the working tree. Matching files are listed with paths and sizes. The agent reads their contents via its tools as needed — the list is a pointer, not inline content.

### 6. Action Directive
A closing section tailored to the dispatch trigger. For commit-triggered dispatches: "Changes in your subscribed files triggered this dispatch. Review the triggering commits above and respond accordingly." For dependency-triggered dispatches: "An upstream task has completed. Review what changed and respond accordingly." For schedule and manual dispatches: "Review the project state — your instructions, subscriptions, and context above. Identify what needs to be done and do it. If nothing needs updating, say so briefly."

## Context Immutability

Trigger context strings are built at the enqueue site (when the trigger fires), not at execution time. This preserves the causal record — the dispatch's invocation section reflects the state that caused it to be created, even if time passes between enqueue and execution.

The `_build_queue_context()` function simply renders these pre-built strings. It does not query for new information.

## Relationship to Other Systems

- [CLI Bridge](cli-bridge.md) receives the assembled prompts via stdin
- [Task Configuration](task-configuration.md) provides instructions, description, and subscriptions
- [Trigger System](trigger-system.md) builds and stores the context strings consumed here
- [Git Integration](git-integration.md) resolves subscription globs to file lists
