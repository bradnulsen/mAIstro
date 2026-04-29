---
name: task-workspace-isolation
description: Each task executes in a dedicated git worktree so non-success terminal states cannot orphan changes in the project's working tree
type: proposal
status: accepted
raised-by: Engineering
reviewed-by: Architecture; behavioral surface incorporated into DESIGN.md
---

# Proposal: Task Workspace Isolation via Per-Task Git Worktrees

## Problem

Tasks run directly in the project's main working tree. The agent reads, edits, and commits files in the same checkout the user is editing in their IDE. This works only because completed tasks reliably commit before exiting — when they don't, the platform has no recovery path and the failure is silent.

Three concrete failure modes follow from the lack of isolation:

1. **Orphaned changes on non-success terminal states.** When a task ends in `exhausted`, `failed`, `timed_out`, `cancelled`, or `interrupted`, any uncommitted edits the agent made remain in the project's working tree. The next task inherits them as part of its dispatch context. The user sees them in `git status` with no obvious provenance. Task #400 is the witnessing case: the agent ran 50 turns updating four architecture docs, hit `error_max_turns`, exited mid-thought before committing, and left four files dirty that the platform never acknowledged or reverted.

2. **Concurrent human edits collide with agent edits.** A human editing a file while a task is running shares the same working tree with the agent. There is no structural barrier between them. Most of the time this is benign (different files), but the platform offers no guarantee — and any policy that "reverts orphan changes on failure" risks reverting the user's concurrent work.

3. **Parallel dispatch is structurally blocked.** `STRATEGY.md` defers parallel dispatch citing "merge conflicts, concurrent MCP sessions" as the risk. Both reduce to "two agents writing into one working tree." Per-task worktrees are the prerequisite that removes the risk.

The CLI's silent-classification bug (max-turns errors mapped to `completed`) is upstream of the orphan-changes problem but does not cause it. Even with the bug fixed, every non-success terminal state still leaves agent work stranded in the working tree. The orphan problem is structural; the CLI bug is tactical.

## Proposed Plan

Three phases, ordered by leverage and confidence. Each phase is independently shippable — the first two close immediate gaps; the third is the durable solution.

### Phase 1 — CLI subtype mapping (immediate, hours)

Fix the silent-classification bug in `backend/cli.py:382-398`. When `is_error: true`, the `result` event currently emits only an `error` event and discards `subtype`, `num_turns`, and `total_cost_usd`. Emit `result_meta` alongside the error, deriving `stop_reason` from `subtype`:

```python
if is_error:
    if not content:
        content = data.get("subtype", "unknown error")
    subtype = data.get("subtype", "")
    stop_reason = "max_turns" if subtype == "error_max_turns" else (subtype or "error")
    return [
        events.error(content),
        events.result_meta(
            cli_session_id=sid or None,
            stop_reason=stop_reason,
            num_turns=data.get("num_turns"),
            cost_usd=data.get("total_cost_usd"),
        ),
    ]
```

Effect: the worker's existing exhaustion path (`worker.py:300`) correctly transitions max-turns failures to `exhausted`, suppresses the dependency cascade, skips the Governor counter increment, and surfaces turns/cost on the task record. Other error subtypes (e.g., `error_during_execution`) gain a non-null `stop_reason` for diagnostics.

This phase does not solve the orphan problem. It stops the platform from misclassifying max-turns failures as success and propagating them through the dependency graph.

**Scope**: `backend/cli.py` (one branch). No schema change. No frontend change.

### Phase 2 — Stash-on-orphan safety net (interim, days)

On any non-success terminal state where `start_commit == result_commit` and the working tree is dirty, stash the agent's changes with a labeled message and record the stash ref on the task. Surface it on the detail drawer with a "view stash" / "restore" / "discard" affordance.

```
git stash push -u -m "task-<id> orphan @ <terminal_state>"
```

Effect: agent work is no longer silently absorbed into the next task's context. The user sees explicit acknowledgment in the UI ("Reverted N files — view stash"). Stashes are recoverable; nothing is destroyed.

Limitations this does **not** address:
- Concurrent human edits in the same working tree. Stashing on terminal sweeps everything dirty, including the user's in-progress edits. A pre-task snapshot of the user's dirty state could narrow the blast radius but adds nested-stash fragility.
- A long-running task continues to share the working tree with the user during execution. The orphan problem is bounded only at the terminal transition.

This phase is a stop-gap. It buys safety for the common single-operator case while Phase 3 is designed and built.

**Scope**: `backend/worker.py` (terminal-state branches), `backend/git.py` (stash helpers), `backend/db_tasks.py` (new `orphan_stash_ref` column on `tasks`), `frontend/src/components/Queue.jsx` (detail drawer surface). Schema change is one column.

### Phase 3 — Per-task git worktree (durable, multi-week)

Each task executes in its own worktree at `<project>/.maistro/worktrees/task-<id>/`, checked out on a task branch (`<job-slug>/task-<id>`). The agent's working directory for the entire dispatch is this worktree. The project's main checkout is structurally untouchable by agents.

#### Lifecycle

| Phase | Action |
|-------|--------|
| Activation (`queued → active`) | Create worktree from current main HEAD onto `<job-slug>/task-<id>`. CLI subprocess runs with `cwd=<worktree_path>`. Internal MCP server's git/file tools resolve paths against the worktree, not `state.PROJECT_DIR`. |
| Agent execution | All reads, edits, commits target the worktree. The user's main checkout is never touched. |
| Terminal — `completed` | Fast-forward (or merge) the task branch into main. `result_commit` is the post-merge HEAD on main. Worktree is removed; task branch is retained or deleted per config. |
| Terminal — `exhausted`, `failed`, `timed_out`, `cancelled`, `interrupted` | Worktree and branch are preserved by default. Detail drawer shows commit count and file change summary, with discard / merge-manually affordances. The project's main branch is untouched. |
| Terminal — `rejected` | Never executed. No worktree exists. |
| Startup recovery | Sweep worktrees whose tasks are in non-success terminal states and were not cleanly handled before shutdown. Record for review; do not blindly delete. |

#### Code surface

- **`backend/git.py`** — new helpers: `worktree_add(branch, base_commit) → path`, `worktree_remove(path)`, `worktree_list()`, `branch_fast_forward(branch, target)`, `branch_merge_into_main(branch)`. Existing `head_hash`, `diff_range`, etc. accept a `cwd` override (currently always `state.PROJECT_DIR`).
- **`backend/dispatch.py` / `backend/worker.py`** — `_process_task` creates the worktree on activation, passes the worktree path through to `cli.invoke` (new `cwd` param) and to the MCP server's environment (`MAISTRO_WORKTREE_PATH`). Terminal handlers reconcile.
- **`backend/mcp_server.py`** — every tool resolves paths against `MAISTRO_WORKTREE_PATH`, not the global project directory. Read-only project-context tools (`list_files`, `read_file` of project-wide content) read from the main checkout; write-side and per-task git tools (`git_commit`, `git_status`, `git_diff`, file edits) target the worktree.
- **`backend/cli.py`** — `invoke(...)` accepts `cwd`. `Popen` uses it.
- **Schema** — `tasks` gains `worktree_path TEXT`, `task_branch TEXT`. Both nullable for legacy rows.
- **Frontend** — detail drawer for non-success tasks shows the task branch, commit list, and discard / merge-manually controls. Diff view continues to work — `git diff main..<task-branch>` is one command.
- **Hook and watch triggers** — unchanged. The post-commit hook fires on commits to the project's main branch (whether from the user, an `Active` task's merge, or external pushes). Task-branch commits do not trigger watch — that is intentional, watch should fire on main.

#### Compatibility with `R10` (project context)

`R10` in `REMEDIATION.md` proposed replacing the global `state.PROJECT_DIR` with a `ProjectContext` object passed through the request/task lifecycle, citing "only justified if multi-project becomes a requirement." Phase 3 creates a *new* requirement: per-task workspace path flowing through worker → cli → mcp_server. The plumbing is the same plumbing R10 needs. Phase 3 effectively pre-pays R10 for the in-process case (per-task context), without committing to multi-project (per-process context).

Concretely: introduce a `TaskContext` (or expand the worker's existing per-task locals) carrying both `project_dir` and `worktree_path`. The global `state.PROJECT_DIR` remains the user's main-checkout view; the per-task context is what the agent's tools resolve against.

#### What stays the same

- Single SQLite database per project (in `<project>/.maistro/`, on the user's main checkout).
- Single post-commit hook on the user's main checkout.
- Single project-switch coordination model (`db_read_guard`, `_switching` flag).
- Sequential execution by default. Worktrees enable parallel dispatch but do not require it.
- The job's commit authorship convention (`<slug>@maistro.local`) and branch naming convention (`<slug>/<description>`).

#### What this enables

- **Orphan changes vanish with the worktree.** Reverting a non-success task is `worktree_remove` — no risk to user state.
- **Concurrent human edits are structurally untouchable.** The user's main checkout is a different filesystem location from any task's workspace.
- **Parallel dispatch becomes a config change, not a structural one.** Two tasks running concurrently each have their own worktree, their own MCP session, their own branch. The merge phase serializes back to main.
- **Diff view simplifies.** `start_commit..result_commit` becomes `main..<task-branch>` (when the task is non-success and unmerged) or remains a commit-range diff (when the task merged into main).

## Sequencing

1. **Phase 1 (CLI subtype mapping) first** — small, isolated, fixes silent classification bug. Ship within a working day. Independently valuable.
2. **Phase 2 (stash-on-orphan) second** — close the orphan gap for the common case while Phase 3 is designed. Ship within a working week. Removes the silent-orphan trap.
3. **Phase 3 (worktree) when designed and budgeted** — multi-week structural change. Sets up the durable solution and unblocks parallel dispatch.

Phase 3 supersedes Phase 2: once worktrees are in place, the stash mechanism becomes dead code and is removed. Phase 2 is intentionally short-lived.

## Resolutions

- **Merge strategy on `completed`** — resolved. Fast-forward when possible, fall back to merge with a generated message that captures the task's identity. When neither is possible (conflicts, divergence the platform cannot resolve automatically), the task does not reach `completed` — it transitions to `failed` with the conflict surfaced as the error context and the workspace preserved for inspection. Completion is contingent on the work being in main, not just on the agent finishing. DESIGN.md ratifies this in the Workspace Isolation and Failed-terminal-state sections.

## Open Questions

- **Default behavior on non-success terminal states**: preserve the workspace (current proposal) or auto-delete after N days? Auto-delete simplifies disk hygiene; preserve gives the operator more recovery options. Default *preserve* and let an operator-configurable retention policy come later.
- **Worktrees inside `.maistro/`**: gitignored from the user's main checkout, but on Windows worktrees cannot share a parent path with the main repo if filesystem links matter. Concrete path: `<project>/.maistro/worktrees/task-<id>/`. Verify on Windows that `git worktree add` accepts a child of `.maistro/` even though it's gitignored — should work because gitignore only governs the user's main checkout, not git's internal mechanisms.
- **Read-only session interrogation (P3)**: when interrogating a resolved task's session, does the agent run in the original task's worktree (preserved or recreated from the task branch) or in the main checkout? Worktree-on-the-task-branch is the consistent answer — the agent reasons about exactly the state it produced. Adds a workspace lifecycle note to that feature.

## Migration Note

No migration script is required. New tasks dispatched after Phase 3 ships will use worktrees; tasks that completed before Phase 3 retain their `start_commit`/`result_commit` columns as the authoritative record of their work. The new `worktree_path` and `task_branch` columns are nullable; legacy rows have NULL.

## Architect Review

Accepted. The three-phase sequencing is sound: each phase is independently shippable and the leverage curve is correct (Phase 1 stops the silent-success classification immediately, Phase 2 bounds the orphan window in the common case, Phase 3 is the durable structural fix that supersedes Phase 2). The R10 alignment is the right read — per-task workspace context is the same plumbing R10 demanded for in-process per-task project context, so Phase 3 absorbs that scope without committing to multi-project per-process.

The behavioral surface (workspace creation at activation, reconciliation per terminal state, integration-failure → `failed`) lives in DESIGN.md. This document is the implementation tier: paths, helper signatures, schema additions, lifecycle plumbing.

Two notes carried forward to implementation:

- **Integration is the gate to `completed`, not a follow-up to it.** The terminal transition that records `completed` must include integration into main as part of the same atomic step (write the event only after the merge succeeds). If integration fails, the transition target is `failed` with the conflict captured in the error column. This avoids an intermediate "completed-but-not-integrated" state that is observable but inconsistent. Worker terminal handlers and `transition_task` callers need to enforce this ordering.
- **Path-resolution boundary in the MCP server.** `mcp_server.py` distinguishes per-task git/file-write tools (resolve against the worktree) from project-context read tools (`list_files`, `read_file` of project-wide content — read from the main checkout). The boundary is "what the agent is producing" vs. "what the agent is reading about the project." Both surfaces need the per-task path env var, but they use it differently. The implementation should make this distinction explicit rather than blanket-rerooting every tool to the worktree.
