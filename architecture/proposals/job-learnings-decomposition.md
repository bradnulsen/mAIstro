# Proposal: Job Learnings

## Status

Draft.

## Summary

Add **learnings** as a complementary structured surface alongside the job's existing prose fields. A learning is a discrete, addressable, individually-toggleable piece of guidance — one rule, one example, one constraint. Learnings live in a new `job_learnings` table and are concatenated into the prompt *after* the existing description prose. The job's `summary` and `description` properties stay as they are: `summary` remains the short manifest blurb, `description` remains the long-form prose body that carries narrative framing — mission, scope, voice. Operators add learnings as they accumulate rules worth toggling individually; agents may add learnings via internal MCP tools when a per-job permission is enabled.

This replaces an earlier framing that proposed decomposing `description` entirely into learnings. The hybrid is better because the two surfaces support different authoring modes: prose carries cohesive narrative (and fragments badly into rows), while learnings carry the kind of guidance that benefits from individual toggle, reorder, source provenance, and agent write-back.

## Goals

- Make individually-toggleable guidance editable at the granularity it's actually reasoned about — one rule, one example, one constraint at a time — without forcing operators to refactor a wall of prose every time they want to mute or revise a single rule.
- Allow individual pieces of guidance to be toggled on/off without deletion. Useful for A/B-style behavior tweaks and for muting stale guidance without losing the artifact.
- Give agents a structured write surface back to their own instructions, gated by an opt-in per-job permission. Closes the only feedback loop the platform doesn't currently have: "what did the agent learn from this task that should change how it operates next time?"
- Preserve the prompt assembly contract for jobs that don't opt in: zero learnings + an unchanged `description` produces a byte-identical prompt. No forced migration, no flattening of working jobs.

## Non-goals (scope guard)

- **Eliminating or shrinking `description`.** It stays as the prose body. The role of learnings is *additive*, not replacement.
- **RAG / vector retrieval / semantic search over learnings.** Learnings concatenate into the system prompt directly, like the description does today. Retrieval-augmented selection is a separate problem deferred until job prompts grow large enough to justify it.
- **Cross-job learning sharing or a global knowledge base.** Learnings are scoped to a single job. The cross-project job templates feature already covers "share configuration across projects" at a coarser grain.
- **Versioning, diff, or history beyond what git already records.** A learning has `created_at` / `updated_at` / `source` and that's it. Audit beyond that comes from the existing MCP tool call log.
- **Auto-distillation from task outcomes.** The Governor or a future evaluator can *propose* learnings via the same write tools as the agent, but no automatic synthesis pipeline ships in v1.
- **Migration of `job_property_defs` / EAV properties** (max_turns, timeout, allowed_tools, schedule, etc.). Those stay on the job. This proposal only adds a sibling table.
- **The job/task system, worker, dispatch, queue, coalescing.** The only worker-adjacent change is in prompt assembly (`build_user_prompt`) where enabled learnings are appended after the existing description block.

## Conceptual Model

### The two surfaces

- **Prose (`summary` + `description`, unchanged from today's design)** carries the cohesive narrative. `summary` is the short, pithy line that feeds the job manifest blurb in every dispatched prompt and the job list UI. `description` is the long-form body that establishes mission, scope, voice, framing — the things that read worse if fragmented.
- **Learnings (new)** carry discrete, individually-addressable rules, examples, constraints, accumulated knowledge. This is the surface that benefits from toggle / reorder / source-provenance / self-modification.

The authoring convention: prose for framing, learnings for anything an operator might want to disable, reorder, or have an agent contribute. The boundary is judgment, not enforcement — both surfaces are free-text strings under the hood.

### Learning

A single, atomic piece of guidance attached to a job. Fields:

- `id` (int, PK)
- `job_id` (int, FK → `jobs`)
- `body` (text — the guidance itself, free-form prose, typically a sentence to a paragraph)
- `enabled` (bool, default true) — disabled learnings are stored but excluded from prompt assembly
- `position` (int) — ordering within the job's learning list (drag-reorder in UI)
- `source` (text — `human` | `agent`) — provenance, useful for filtering and for the "show only agent-added learnings" UI affordance. No `seed` value is needed because there's no forced migration.
- `created_at`, `updated_at`

Learnings are append-only in spirit but mutable: edits update `updated_at`, deletions are hard deletes (git history is the audit trail). No soft-delete; `enabled=false` covers the "I want it gone but might want it back" case.

### Prompt assembly

`build_user_prompt` in [dispatch.py](backend/dispatch.py) currently emits a job-identity section with `# {name}` followed by the description (or summary as fallback). Post-change, after that section it appends a `## Learnings` section with the bodies of enabled learnings ordered by `position`, separated by blank lines, **only if any enabled learnings exist for the job**. Jobs with no learnings produce an unchanged prompt.

Disabled learnings are not visible to the agent. The query is `SELECT body FROM job_learnings WHERE job_id = ? AND enabled = 1 ORDER BY position`.

The section header (`## Learnings`) is intentionally generic. Whether to title it differently per-job (e.g. "## Rules" vs "## Examples") is deferred — see Open Questions.

### Self-modification permission

A new EAV property: `allow_learning_self_modification` (boolean, default `false`). When `true`, the dispatch passes a flag through `MAISTRO_ALLOWED_INTERNAL_TOOLS` that exposes write tools on the internal MCP server. When `false` (default for all jobs), the agent sees only read access to its own learnings — useful for introspection without write capability.

### Internal MCP tools

Following the pattern in [mcp_server.py](backend/mcp_server.py):

- `list_learnings()` — read; always available regardless of permission. Returns enabled + disabled learnings for the current job with id, body, enabled, position, source.
- `add_learning(body)` — write; gated. Appends at the end of the position list with `source='agent'`.
- `update_learning(id, body)` — write; gated. Mutates body, bumps `updated_at`. Cannot be used to flip `enabled` (intentional — toggling is an operator action, not an agent action). Restricted to learnings the agent itself authored (`source='agent'`).
- `delete_learning(id)` — write; gated. Hard delete. Restricted to learnings the agent itself created (`source='agent'`); human-authored learnings cannot be deleted by the agent.

All four tools route through the same audit logging as existing internal tools. The reasoning for the asymmetry on `update_learning` and `delete_learning` is that the agent can manage its own knowledge but not retract or rewrite guidance the operator wrote.

### Operator UI

The job configuration screen ([Tasks.jsx](frontend/src/components/Tasks.jsx)) keeps the existing `summary` and `description` fields exactly as they are. A new **Learnings** section renders below `description`:

- Each row shows the body (truncated, click to expand/edit), an enabled toggle, a source badge (`human` / `agent`), a drag handle for reorder, and a delete button.
- An "Add learning" button opens an inline editor.
- Filter controls: show all / show enabled / show by source.

For new jobs, the learnings list starts empty. Operators are not nudged to seed it — the description carries the prose, and learnings accumulate when the operator (or an agent) has a discrete rule worth pinning.

### Schema

```sql
CREATE TABLE IF NOT EXISTS job_learnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'human' CHECK (source IN ('human','agent')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_learnings_job ON job_learnings(job_id, position);
```

Goes in `SCHEMA_SQL` in [db_core.py](backend/db_core.py) per the no-baked-in-migrations convention.

## Migration

There is no data migration. The `job_learnings` table is empty for every existing job after the schema change ships. Existing `description` prose stays where it is and continues to drive prompt assembly unchanged. Operators (and agents, for jobs that opt into self-modification) add learnings incrementally as the need arises.

This is a structural advantage of the hybrid framing: zero data risk, no offline scripts, no flattening pass, no chance of garbling working jobs. The feature is purely additive at the data layer.

## Open Questions

- **Categories or tags on learnings.** Tempting (`role`, `examples`, `constraints`, `style`) but adds UI surface and a taxonomy to maintain. Defer; revisit if real usage shows the flat list is unmanageable past ~20 learnings per job.
- **Should the agent see learning IDs in its prompt?** Probably yes — opens the door to "the rule on line 7 contradicts the rule on line 3, here's why I followed line 7" kinds of reasoning. Cheap to include. The IDs are already in `list_learnings` output; the question is whether to also embed them in the assembled prompt section.
- **Section header phrasing.** `## Learnings` is plain but accurate. Alternatives like `## Rules`, `## Guidance`, `## Operating Notes` lean different ways. Pick one and commit; cosmetic.
- **Should an empty learnings list render the section header at all?** No — emit nothing when no learnings are enabled, so jobs that don't use the feature produce byte-identical prompts.

## Second-Order Effects

- **Authoring decision tax.** Two surfaces means operators must decide "is this prose or a rule?" The convention (prose for framing, learnings for anything toggleable) is judgment-based. This is a real cost but small — and the failure mode of putting something in the wrong surface is benign (it still ends up in the prompt).
- **Governor surface.** The Governor can read learnings via the same MCP path and propose new ones as findings ("I noticed jobs without explicit error-handling guidance fail more often — consider adding..."). The Governor Threads proposal pairs naturally: a thread can carry a proposed learning as its `action_payload`, the operator replies, the Governor adds the learning if assented.
- **Job templates.** The cross-project job templates feature (in `appstate.py`) will need to learn to copy learnings alongside the job row. Mechanical addition.
- **Prompt size.** Concatenated learnings can grow over time, on top of an unchanged description. Monitor; revisit retrieval-based selection if prompts start getting truncated. Disabled learnings cost nothing at prompt time.
- **Self-modification feedback loop.** Even with the permission gated off by default, the read-only `list_learnings` tool gives agents introspection into their own learnings (though not the description prose itself, unless a similar `get_job_description` read is added later). That alone is valuable for "why did you do X?" interrogation (Priority 3 in STRATEGY.md).

## Sequencing

Sized for a multi-day chunk, similar in scope to the original task-workspace-isolation Phase 2:

1. Schema + read path: add table, extend `build_user_prompt` to append the learnings section when enabled rows exist. Ship as a no-op for all existing jobs (empty table → unchanged prompts).
2. Operator UI: add the learnings list below the description textarea — add/edit/delete/reorder/toggle. No change to `summary` / `description` editing.
3. MCP tools: read tool first (`list_learnings`), then write tools behind the permission gate.
4. Property: `allow_learning_self_modification` definition + plumbing through to `MAISTRO_ALLOWED_INTERNAL_TOOLS`.

Each step ships independently; (1) alone is shippable as "schema landed, no observable behavior change yet."
