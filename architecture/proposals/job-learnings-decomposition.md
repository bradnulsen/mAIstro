# Proposal: Job Learnings Decomposition

## Status

Draft.

## Summary

Decompose the monolithic `description` field on jobs into a normalized collection of **learnings** — discrete, addressable knowledge pieces that together carry what an agent is, what it knows, what it does, and its guidance. The job schema flattens to name + a short description (the mission statement / north star); the operational substance moves to a `job_learnings` table. The job configuration screen replaces the description textarea with a CRUD list: add, edit, delete, reorder, and individually enable/disable learnings. Optionally, per-job permission allows the agent itself to add or refine learnings via internal MCP tools, audited like every other internal tool call.

## Goals

- Make job guidance editable at the granularity it's actually reasoned about — one rule, one example, one constraint at a time — instead of by rewriting a wall of prose.
- Allow individual pieces of guidance to be toggled on/off without deletion. Useful for A/B-style behavior tweaks and for muting stale guidance without losing the artifact.
- Give agents a structured write surface back to their own instructions, gated by an opt-in per-job permission. Closes the only feedback loop the platform doesn't currently have: "what did the agent learn from this task that should change how it operates next time?"
- Preserve the prompt assembly contract — enabled learnings concatenate into the same slot the description occupies today. Dispatch behavior is unchanged for jobs that don't opt into self-modification.

## Non-goals (scope guard)

- **RAG / vector retrieval / semantic search over learnings.** Learnings are concatenated into the system prompt directly, like the description does today. Retrieval-augmented selection is a separate problem deferred until job prompts grow large enough to justify it.
- **Cross-job learning sharing or a global knowledge base.** Learnings are scoped to a single job. The cross-project job templates feature already covers "share configuration across projects" at a coarser grain.
- **Versioning, diff, or history beyond what git already records.** A learning has a `created_at` / `updated_at` / `source` and that's it. Audit beyond that comes from the existing MCP tool call log.
- **Auto-distillation from task outcomes.** The Governor or a future evaluator can *propose* learnings via the same write tools as the agent, but no automatic synthesis pipeline ships in v1.
- **Migration of `job_property_defs` / EAV properties** (max_turns, timeout, allowed_tools, schedule, etc.). Those stay on the job. This proposal is only about the prose-shaped guidance currently in `description`.
- **The job/task system, worker, dispatch, queue, coalescing.** The only worker-adjacent change is in prompt assembly (`build_dispatch_system_prompt`), and that change is "read learnings instead of description."

## Conceptual Model

### Learning

A single, atomic piece of guidance attached to a job. Fields:

- `id` (int, PK)
- `job_id` (int, FK → `jobs`)
- `body` (text — the guidance itself, free-form prose, typically a sentence to a paragraph)
- `enabled` (bool, default true) — disabled learnings are stored but excluded from prompt assembly
- `position` (int) — ordering within the job's learning list (drag-reorder in UI)
- `source` (text — `seed` | `human` | `agent`) — provenance, useful for filtering and for the "show only agent-added learnings" UI affordance
- `created_at`, `updated_at`

Learnings are append-only in spirit but mutable: edits update `updated_at`, deletions are hard deletes (git history is the audit trail). No soft-delete; `enabled=false` covers the "I want it gone but might want it back" case.

### Job description (post-flatten)

The `description` column stays on `jobs` but its role narrows. After this change it is a **one-paragraph mission statement** — what the job exists to do, written for humans skimming the job list. It does not carry operational guidance. Existing long-form descriptions migrate forward as a single seed learning per job (see Migration below); the description becomes a one-line summary derived from the job name + first sentence of the original prose, editable by the operator.

### Prompt assembly

`build_dispatch_system_prompt` in [dispatch.py](backend/dispatch.py) currently injects `description` into the system prompt. Post-change, it queries `job_learnings WHERE job_id = ? AND enabled = 1 ORDER BY position` and concatenates the bodies into the same slot, separated by blank lines. The slot's surrounding text ("Here is your operating guidance:") remains unchanged. Disabled learnings are not visible to the agent.

### Self-modification permission

A new EAV property: `allow_learning_self_modification` (boolean, default `false`). When `true`, the dispatch passes a flag through `MAISTRO_ALLOWED_INTERNAL_TOOLS` that exposes write tools on the internal MCP server. When `false` (default for all migrated jobs), the agent sees only read access to its own learnings — useful for introspection without write capability.

### Internal MCP tools

Following the pattern in [mcp_server.py](backend/mcp_server.py):

- `list_learnings()` — read; always available regardless of permission. Returns enabled + disabled learnings for the current job with id, body, enabled, position, source.
- `add_learning(body)` — write; gated. Appends at the end of the position list with `source='agent'`.
- `update_learning(id, body)` — write; gated. Mutates body, bumps `updated_at`. Cannot be used to flip `enabled` (intentional — toggling is an operator action, not an agent action).
- `delete_learning(id)` — write; gated. Hard delete. Restricted to learnings the agent itself created (`source='agent'`); seed/human learnings cannot be deleted by the agent.

All four tools route through the same audit logging as existing internal tools. The reasoning for the asymmetry on `delete_learning` is that the agent can manage its own knowledge but not retract guidance the operator wrote.

### Operator UI

The job configuration screen ([Tasks.jsx](frontend/src/components/Tasks.jsx)) replaces the description textarea with:

- A short single-line input for the new mission-statement description.
- A learnings list below it: each row shows the body (truncated, click to expand/edit), an enabled toggle, a source badge (`seed` / `human` / `agent`), a drag handle for reorder, and a delete button.
- An "Add learning" button that opens an inline editor.
- Filter controls: show all / show enabled / show by source.

Creation of a new job presents an empty learnings list with a single suggested seed: "Describe the job's purpose, scope, and guidance here. You can split this into multiple learnings later."

### Schema

```sql
CREATE TABLE IF NOT EXISTS job_learnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'human' CHECK (source IN ('seed','human','agent')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_learnings_job ON job_learnings(job_id, position);
```

Goes in `SCHEMA_SQL` in [db_core.py](backend/db_core.py) per the no-baked-in-migrations convention. A one-shot script under `/scripts/` (e.g. `seed_learnings_from_descriptions.py`) handles the data migration for existing project DBs offline — same pattern as `migrate_db.py`.

## Migration

For each existing job:

1. Read the current `description`.
2. If non-empty, insert a single `job_learnings` row with `body = description`, `source = 'seed'`, `position = 0`, `enabled = 1`.
3. Replace the `description` column value with a derived one-line summary (first sentence of the original, or the job name if empty).

Result: existing jobs produce byte-identical prompts post-migration, because prompt assembly concatenates a single learning containing the same text the description used to carry. Operators then incrementally split the seed into smaller learnings as they edit.

## Open Questions

- **Categories or tags on learnings.** Tempting (`role`, `examples`, `constraints`, `style`) but adds UI surface and a taxonomy to maintain. Defer; revisit if real usage shows the flat list is unmanageable past ~20 learnings per job.
- **Should the agent see learning IDs in its prompt?** Probably yes — opens the door to "the rule on line 7 contradicts the rule on line 3, here's why I followed line 7" kinds of reasoning. Cheap to include.
- **Position vs. ordering by source.** Drag-reorder is more flexible but requires UI work; ordering by `source` then `created_at` is simpler. Lean toward drag-reorder because operators will want seed/foundational guidance pinned at the top.
- **Should `update_learning` be allowed to edit human-authored learnings, or only agent-authored ones?** Symmetric with delete: agent edits its own, operator edits everything. Default to that.

## Second-Order Effects

- **Governor surface.** The Governor can read learnings via the same MCP path and propose new ones as findings ("I noticed jobs without explicit error-handling guidance fail more often — consider adding..."). The Governor Threads proposal pairs naturally: a thread can carry a proposed learning as its `action_payload`, the operator replies, the Governor adds the learning if assented.
- **Job templates.** The cross-project job templates feature (in `appstate.py`) will need to learn to copy learnings alongside the job row. Mechanical addition.
- **Prompt size.** Concatenated learnings can grow larger than a single description would have, because there's no longer pressure to keep it tight. Monitor; revisit retrieval-based selection if prompts start getting truncated.
- **Self-modification feedback loop.** Even with the permission gated off by default, the read-only `list_learnings` tool gives agents introspection into their own instructions. That alone is valuable for "why did you do X?" interrogation (Priority 3 in STRATEGY.md).

## Sequencing

Sized for a multi-day chunk, similar in scope to the original task-workspace-isolation Phase 2:

1. Schema + read path: add table, port `build_dispatch_system_prompt` to read learnings, migrate existing data via offline script. Behavior unchanged for jobs with one seed learning.
2. Operator UI: replace description textarea with learnings list + add/edit/delete/reorder/toggle.
3. MCP tools: read tool first, then write tools behind the permission gate.
4. Property: `allow_learning_self_modification` definition + plumbing through to `MAISTRO_ALLOWED_INTERNAL_TOOLS`.

Each step ships independently; (1) alone is shippable as "we changed where guidance lives, no other behavior change."
