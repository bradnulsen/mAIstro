# Git Integration

Git is the content source of truth. The platform reads git state extensively but writes to it only for two operational purposes: hook installation and gitignore management. All project content changes come from agents committing through their own tools — the platform never auto-commits on behalf of agents (see [Dispatch Engine — Commit Tracking](dispatch-engine.md#commit-tracking-no-auto-commit) for rationale).

**Module**: `backend/git.py`

## Subprocess Abstraction

All git operations use a single `run_git()` function wrapping `subprocess.run` with `capture_output=True, text=True`. The `cwd` parameter is always the project directory — git commands execute in the project's context.

No git library is used. The abstraction is thin and deliberate — subprocess calls are predictable, debuggable, and have no dependency beyond git itself.

**Encoding**: `run_git()` must pass `encoding="utf-8"` explicitly. On Windows, `text=True` without an explicit encoding uses the system codepage (cp1252), which fails on non-ASCII content in commit messages, filenames, or diff output. Git itself outputs UTF-8; the subprocess call must match.

## Project Initialization

Three operations run when a project is opened:

### Repository Verification
`ensure_repo()` checks for `.git/` and runs `git init` if absent. This means mAistro can open a non-git directory and initialize it.

### Post-Commit Hook
`install_post_commit_hook()` writes a bash script to `.git/hooks/post-commit` that curls `POST /api/hooks/post-commit` with the commit hash. The curl runs in the background (`&`) to avoid blocking the commit.

The hook is reinstalled every time a project is opened, ensuring it stays current (e.g., if the port changes). It's a fire-and-forget notification — if the backend isn't running, the curl fails silently.

### Gitignore Management
`ensure_gitignore()` appends `.maistro/` and `.claude/` to `.gitignore` if not already present. This keeps operational state out of version control.

## Read Operations

### Log Parsing

Git log output uses a structured format (`%H|%an|%ae|%s|%ai`) parsed into dicts with hash, author, email, message, and date fields. Two parsing modes:

- **Standard**: one entry per line, pipe-delimited
- **With numstat**: interleaved format lines and file stats, producing entries with file lists and insertion/deletion counts

### Diff Operations

- `diff(commit)`: diff of a single commit against its parent
- `diff_range(from, to)`: diff between two commits — returns structured file stats (via `--numstat`) plus the raw diff text. Used for task before/after comparison.

### Other Queries

- `head_hash()`: current HEAD commit — used for task start/result commit tracking
- `changed_files_in_commit()`: file list from `git diff-tree` — used by watch trigger matching
- `commit_oneline()`: subject + stat summary — used for trigger context strings
- `status()`: porcelain format working tree status
- `show()`: full commit details with optional stats

## Write Operations

- `commit_file()`: stages a specific file and commits. Used by the file write API endpoint (not by agents — they commit through their own tools)
- `write_file()` / `read_file()`: direct filesystem operations on the working tree

## Glob Resolution

`resolve_glob_files()` resolves subscription patterns to file metadata. Uses Python's `glob.glob` with `recursive=True` for `**` support. Returns relative paths, sizes, and modification times — this data feeds into the task prompt's subscribed files section.

## Worktree Management

Tasks execute in their own per-task git worktree at `<project>/.maistro/worktrees/task-<id>/`, on a branch named `<job-slug>/task-<id>`. The agent's CLI subprocess and the MCP server's write/read FS tools all resolve against this path. The operator's main checkout is structurally untouchable by agents.

### Helpers

| Helper | Purpose |
|---|---|
| `worktree_add(project_dir, path, branch, base_commit) → (ok, err)` | Create a worktree on a fresh branch from the given base commit. Returns `(False, stderr)` on failure (most often: branch already exists from a stale prior attempt). |
| `worktree_remove(project_dir, path, force=False)` | Tear down a worktree. `force=True` discards uncommitted changes — used in cleanup paths where the work is being deliberately discarded. |
| `worktree_list(project_dir) → list[dict]` | Parse `git worktree list --porcelain` into structured entries. Used by startup recovery and operator UI. |
| `worktree_prune(project_dir)` | Clean up registry entries for worktrees whose directories were removed externally. Run on every backend startup. |
| `branch_delete(project_dir, branch, force=False)` | Delete a branch by name. The integration path uses `force=True` after merging into main. |
| `is_ancestor(cwd, commit, ref) → bool` | Check whether `commit` is reachable from `ref`. Used by the post-commit hook to filter out task-branch commits. |
| `merge_branch(project_dir, source_branch, ff_only=, no_ff=, message=) → (ok, output)` | Merge a task branch into the current HEAD. Worker tries `ff_only=True` first, falls back to `no_ff=True` if main moved. |
| `merge_abort(project_dir)` | Abort an in-progress merge and reset the working tree. Called when integration conflicts. |
| `current_branch_name(cwd) → str \| None` | Return the branch HEAD points at (None for detached HEAD). Used by `integrate_branch` to label generated merge commits with the operator's actual base branch (e.g. `main`, `master`, `develop`) rather than assuming. |
| `reset_hard(cwd, target) → (ok, err)` | Run `git reset --hard <target>`. Used inside `integrate_branch`'s rollback path to restore main to its pre-merge HEAD when the post-merge stash apply fails. |
| `integrate_branch(project_dir, branch, stash_label, merge_message) → (ok, error)` | Encapsulates the full stash → merge → unstash flow with operator-wins rollback. Handles dirty-main stashing, FF-then-merge fallback, post-merge stash re-apply, and rollback to the captured pre-merge HEAD on conflict. Used by both the worker's automatic completion path and the manual workspace-integrate route. Caller decides what to do with the `(ok, error)` result. |

### Lifecycle

| Stage | What happens |
|---|---|
| **Creation** (`queued → active`) | Worker computes `start_commit = head_hash(PROJECT_DIR)`. Defensive cleanup: if the path or branch already exists (stale from a prior crash), force-remove first. `worktree_add` creates the worktree on `<job-slug>/task-<id>` from `start_commit`. Success persists `worktree_path` and `task_branch` on the task's `task_executions` row via the activation `transition_task` call. Failure transitions the task to `failed`. |
| **Execution** | CLI runs with `cwd=<worktree>`. MCP server reads `MAISTRO_WORKSPACE_DIR` from env and roots its write/read FS tools there. Agent commits go to the task branch. The post-commit hook fires from the worktree's cwd; the hook handler filters via `is_ancestor(commit, HEAD)` so only commits reachable from main trigger watch logic. |
| **Successful integration** (`completed`) | The worker delegates to `git.integrate_branch(...)`, which captures the pre-merge HEAD, stashes the operator's dirty working tree if any with the marker `maistro pre-integrate task-<id>`, attempts `merge --ff-only` then falls back to `merge --no-ff` with a generated message labelled with `current_branch_name`, and re-applies the stash on success. On overall success, `result_commit` is the new main HEAD and the worker runs `worktree_remove --force` + `branch_delete --force` + clears the `worktree_path`/`task_branch` columns. **Integration is the gate to `completed`** — there is no intermediate "completed-but-not-integrated" state.<br><br>**Conflict policy is operator-wins.** Two failure modes are handled identically: a failed merge (agent's branch vs main) or a failed post-merge stash apply (operator's edits overlap agent's commits). In either case `integrate_branch` calls `merge_abort`, `reset_hard` to the captured pre-merge HEAD, and `stash_apply`+`stash_drop` to restore the operator's exact pre-integration state. The worker then transitions the task to `failed` and preserves the workspace. The two modes produce distinguishable error messages on `task_executions.error` ("agent's branch '<branch>' could not merge into <main>" vs. "operator's in-progress working-tree edits overlap with agent's commits") so reply invocations can reason about which side to adjust. |
| **No commits made** | If the agent finished without committing (`worktree_head == start_commit`), the worker transitions to `failed` with `"agent finished without committing"`. The workspace is preserved so the operator can inspect any dirty state. |
| **Non-success terminals** (`exhausted`, `failed`, `timed_out`, `cancelled`, `interrupted`) | `result_commit` reflects the worktree branch tip (the existing diff endpoint surfaces the agent's work via `start..result`). Worktree and branch are preserved. The detail-drawer `WorkspaceBanner` exposes the path/branch and offers two backend operations: **`POST /api/tasks/{id}/workspace/integrate`** re-runs `integrate_branch` on demand (useful when the original auto-integration failed on a transient state the operator has since resolved); **`POST /api/tasks/{id}/workspace/discard`** force-removes the worktree and branch. Both routes use a shared `_resolve_workspace_for_action` helper that gates on the **owning** task's status — under inverted coalescing a resume task `Y` may be actively using a worktree the database row of original task `X` still references, so the gate reads the owner's status (via `effective_root_id`) rather than the queried task's own status. Both require the owner to be in `NON_SUCCESS_TERMINAL_STATUSES` with a populated `worktree_path`. |
| **Startup sweep** | `_sweep_stale` marks any tasks left in `active` (from a crashed prior backend run) as `interrupted` and runs `git worktree prune` to clear registry entries for directories removed while the backend was down. Worktrees from interrupted tasks are preserved. |

### Post-Commit Hook Filtering

Worktrees share the same `.git` directory as the main checkout, so a commit made in a worktree fires the post-commit hook from the worktree's cwd. The hook handler now skips any commit not reachable from PROJECT_DIR's HEAD (`is_ancestor(commit_hash, HEAD)`). Effects:

- Task-branch commits don't trigger watch logic during execution. Watch only fires when work lands on main.
- The integration step (FF or merge commit) IS reachable from PROJECT_DIR's HEAD afterward, so the hook fires once per integration with the new main HEAD as the trigger commit.
- This filter also means commits the operator makes on a non-main branch in PROJECT_DIR (e.g. working on `develop`) don't trigger watch either — watch fires on integration into wherever HEAD points.

### Disk Pressure and Dead Worktrees

By design, every non-success terminal preserves a worktree until the operator discards it. This creates accumulation pressure proportional to failure rate:

- Each worktree is essentially a full project checkout. A 100MB project with 50 stuck tasks is 5GB of worktree storage.
- There is no auto-prune or retention policy yet — the only cleanup paths are operator action via the detail drawer and successful integration.

**Concrete leak modes**, ranked roughly by likelihood:

1. **Operator never discards non-success workspaces.** Most common case. UX-driven, not a correctness bug. Mitigation: a retention policy ("auto-discard after N days for terminal failures") is a deferred follow-up.
2. **Crash between `worktree_add` success and the activation `transition_task`.** Pre-normalization this caused task #422 to wedge: the partial transaction left the events log diverged from the column. **Now mitigated** by the explicit rollback in `transition_task` (see [Task Lifecycle — Transition Function](task-lifecycle.md#transition-function)). The worktree directory itself is reconciled by `_sweep_orphan_worktrees`, called from `_sweep_stale` on startup: it enumerates `.maistro/worktrees/` and removes any `task-N/` directory whose task is missing or in a state that shouldn't have a worktree (`pending`, `queued`, `completed`, `rejected`). Directories belonging to active or non-success-terminal tasks are preserved.
3. **Crash AFTER merge succeeded, BEFORE worktree removal.** Task is `completed` in the DB but the worktree directory still exists and `worktree_path` is still populated. Cosmetic — the operator sees a banner on a completed task; the discard button works correctly.
4. **Crash AFTER worktree removal, BEFORE clearing DB columns.** Worktree gone, DB still references it. Operator sees a banner pointing at a non-existent path; clicking discard succeeds silently (helpers tolerate missing paths/branches).
5. **Operator manually deletes the worktree directory via OS file manager.** `git worktree list` retains a stale registry entry; `worktree_prune` cleans this on next backend startup. Not a leak.
6. **Backend killed mid-task** (sigkill, BSOD). On restart, sweep transitions the active task to `interrupted` and prunes. Worktree+branch preserved (correct). Same accumulation pressure as case 1.

Case 1 (operator never discards) remains the dominant pressure source. A **retention policy** ("auto-discard after N days for terminal failures") is the obvious next step; it's a small, isolated addition to `_sweep_stale`.

### CWD Override

The git helpers accept `cwd` (or `project_dir` for write helpers) so per-task git operations target the worktree path rather than the global `state.PROJECT_DIR`. The MCP server's `WORKSPACE_DIR` constant (from `MAISTRO_WORKSPACE_DIR`) defaults to `PROJECT_DIR` for backward compatibility but the worker always populates it with the per-task worktree path.

## Relationship to Other Systems

- [Trigger System](trigger-system.md) depends on the post-commit hook for watch triggers and on `changed_files_in_commit()` for pattern matching
- [Dispatch Engine](dispatch-engine.md) uses `head_hash()` for commit tracking on task start and completion
- [Prompt Assembly](prompt-assembly.md) uses `resolve_glob_files()` to build the subscribed files list
- The feed view in the [Frontend](frontend.md) consumes structured log entries enriched with task metadata
