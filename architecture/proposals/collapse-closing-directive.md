# Proposal: Collapse Closing Directive into Trigger Context

## Problem

The user prompt currently communicates trigger-type awareness through three redundant layers, each restating the same information with decreasing specificity:

| Layer | Example (commit trigger) | Source |
|---|---|---|
| **Dispatch mode** (identity section) | `**Dispatch:** auto-dispatched (commit)` | `task_meta["trigger"]` in `build_user_prompt` |
| **Invocation context** (section 3) | `**Commit** abc123de: Add login feature` | `context` column, built at enqueue site |
| **Closing directive** (section 6) | "Changes in your subscribed files triggered this dispatch. Review the triggering commits above and respond accordingly." | `_build_closing_directive(trigger)` in dispatch.py |

The invocation context is the only layer that carries real data — commit hashes, error messages, upstream job names, cron expressions, user notes. The dispatch mode label and closing directive restate the trigger type in generic prose without adding information.

## Analysis

### What each trigger's closing directive actually says

- **commit**: "Review the triggering commits above" — the invocation section already names the commit
- **dependency**: "Use the commit range to inspect what changed" — the invocation section already provides the range and outcome summary
- **schedule**: "Check your subscribed files and project state" — this is what every trigger should do
- **retry**: "Review context above, adjust your approach" — the invocation section already carries the error and prior commit range
- **resume**: "Pick up where you left off" — the CLI session resume mechanism handles continuity; this is a no-op instruction
- **manual/default**: "Review instructions, subscriptions, and context; identify what needs doing" — the only genuinely useful directive, and it applies universally

### The closing directive is business logic in the wrong place

`_build_closing_directive` is a switch statement on trigger type inside the prompt assembly layer. Prompt assembly should be a compositor — it arranges sections that other systems provide. It should not contain conditional logic that interprets trigger semantics.

The trigger system already owns the "why" — each enqueue site builds a context string with full causal detail. The closing directive duplicates this ownership by re-deriving a weaker version of the same "why" from the trigger type alone.

### The dispatch mode label is also redundant

The `**Dispatch:** auto-dispatched (commit)` line in the identity section restates the trigger type a third time. The invocation section already opens with `**Commit**`, `**Schedule**`, `**Dependency**`, etc. The agent doesn't need a separate label to know its dispatch mode.

## Proposed Change

1. **Remove `_build_closing_directive`**. Replace it with a single static directive that works for all trigger types:

   ```
   ## Your Turn
   Review the project state — your instructions, subscriptions, and context above.
   Identify what needs to be done and do it. If nothing needs updating, say so briefly.
   ```

   This is the current default/manual directive, and it's the only one that gives a universal, non-redundant instruction.

2. **Remove the dispatch mode line** from the identity section. The `**Dispatch:** auto-dispatched (commit)` adds no information beyond what the invocation section provides.

3. **Update prompt-assembly.md** to reflect the simplified structure: the "Action Directive" section becomes a static closer, not a trigger-conditional one.

## What stays the same

- The invocation context section is unchanged — it remains the sole carrier of trigger-specific information
- Trigger context strings are still built at enqueue sites with full causal detail
- The `_build_queue_context` function continues to render pre-built context strings with deduplication and coalesced framing

## Impact

- `dispatch.py`: delete `_build_closing_directive` (~30 lines), simplify `build_user_prompt` (remove `task_meta` parameter and dispatch mode line)
- `worker.py`: simplify `run_task` (stop building and passing `task_meta`)
- `prompt-assembly.md`: simplify section 6 documentation
- No data model changes, no schema changes, no API changes
