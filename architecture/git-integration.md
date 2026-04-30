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

### Lifecycle

| Stage | What happens |
|---|---|
| **Creation** (`queued → active`) | Worker computes `start_commit = head_hash(PROJECT_DIR)`. Defensive cleanup: if the path or branch already exists (stale from a prior crash), force-remove first. `worktree_add` creates the worktree on `<job-slug>/task-<id>` from `start_commit`. Success persists `worktree_path` and `task_branch` on the task's `task_executions` row via the activation `transition_task` call. Failure transitions the task to `failed`. |
| **Execution** | CLI runs with `cwd=<worktree>`. MCP server reads `MAISTRO_WORKSPACE_DIR` from env and roots its write/read FS tools there. Agent commits go to the task branch. The post-commit hook fires from the worktree's cwd; the hook handler filters via `is_ancestor(commit, HEAD)` so only commits reachable from main trigger watch logic. |
| **Successful integration** (`completed`) | `_integrate_or_fail` in the worker tries `merge --ff-only` against PROJECT_DIR's HEAD; falls back to `merge --no-ff` with a generated message if main moved. On success, `result_commit` is the new main HEAD, then `worktree_remove --force` + `branch_delete --force` + clear the `worktree_path`/`task_branch` columns. **Integration is the gate to `completed`** — if the merge fails (conflict), the worker calls `merge_abort` and transitions to `failed` with the conflict captured in `error`, preserving the workspace. There is no intermediate "completed-but-not-integrated" state.<br><br>**Operator-dirty main during integration**: per the workspace-isolation proposal, the integration step is supposed to transparently stash the operator's dirty working-tree state before the merge and re-apply it after, so concurrent operator editing doesn't block integration. Conflict policy is **operator wins**: a failed merge or a failed post-merge stash re-apply rolls main back to its pre-integration HEAD, restores the stash, and transitions the task to `failed`. The agent's commits remain on the preserved task branch for manual integration. **Not yet implemented** — current code lets `git merge` fail loudly with "your local changes would be overwritten" if PROJECT_DIR is dirty at integration time. The task ends up `failed` with a slightly less actionable error message; the operator's edits are preserved (they're just dirty in the tree) and the agent's branch is preserved (per the existing non-success preservation policy), so the loose end here is UX, not data loss. |
| **No commits made** | If the agent finished without committing (`worktree_head == start_commit`), the worker transitions to `failed` with `"agent finished without committing"`. The workspace is preserved so the operator can inspect any dirty state. |
| **Non-success terminals** (`exhausted`, `failed`, `timed_out`, `cancelled`, `interrupted`) | `result_commit` reflects the worktree branch tip (the existing diff endpoint surfaces the agent's work via `start..result`). Worktree and branch are preserved. The detail-drawer `WorkspaceBanner` exposes the path, branch, a copyable manual-merge command, and a confirm-gated discard button (`POST /api/tasks/{id}/workspace/discard`). |
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
2. **Crash between `worktree_add` success and the activation `transition_task`.** Pre-normalization this caused task #422 to wedge: the partial transaction left the events log diverged from the column. **Now mitigated** by the explicit rollback in `transition_task` (see [Task Lifecycle — Transition Function](task-lifecycle.md#transition-function)). The worktree directory itself can still leak in this case — the defensive cleanup at activation only fires if the same task ID is re-processed, which doesn't happen for tasks reconciled to `interrupted`. **Open follow-up**: extend `_sweep_stale` to enumerate `.maistro/worktrees/` and remove directories whose task IDs are unknown to the DB or whose tasks don't claim the worktree.
3. **Crash AFTER merge succeeded, BEFORE worktree removal.** Task is `completed` in the DB but the worktree directory still exists and `worktree_path` is still populated. Cosmetic — the operator sees a banner on a completed task; the discard button works correctly.
4. **Crash AFTER worktree removal, BEFORE clearing DB columns.** Worktree gone, DB still references it. Operator sees a banner pointing at a non-existent path; clicking discard succeeds silently (helpers tolerate missing paths/branches).
5. **Operator manually deletes the worktree directory via OS file manager.** `git worktree list` retains a stale registry entry; `worktree_prune` cleans this on next backend startup. Not a leak.
6. **Backend killed mid-task** (sigkill, BSOD). On restart, sweep transitions the active task to `interrupted` and prunes. Worktree+branch preserved (correct). Same accumulation pressure as case 1.

The two real follow-ups worth implementing: an **orphan-directory sweep** for case 2, and a **retention policy** for case 1. Both are small, isolated additions to `_sweep_stale`.

### CWD Override

The git helpers accept `cwd` (or `project_dir` for write helpers) so per-task git operations target the worktree path rather than the global `state.PROJECT_DIR`. The MCP server's `WORKSPACE_DIR` constant (from `MAISTRO_WORKSPACE_DIR`) defaults to `PROJECT_DIR` for backward compatibility but the worker always populates it with the per-task worktree path.

## Relationship to Other Systems

- [Trigger System](trigger-system.md) depends on the post-commit hook for watch triggers and on `changed_files_in_commit()` for pattern matching
- [Dispatch Engine](dispatch-engine.md) uses `head_hash()` for commit tracking on task start and completion
- [Prompt Assembly](prompt-assembly.md) uses `resolve_glob_files()` to build the subscribed files list
- The feed view in the [Frontend](frontend.md) consumes structured log entries enriched with task metadata
