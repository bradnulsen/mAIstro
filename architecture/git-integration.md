# Git Integration

Git is the content source of truth. The platform reads git state extensively but writes to it only for two operational purposes: hook installation and gitignore management. All project content changes come from agents committing through their own tools — the platform never auto-commits on behalf of agents (see [Dispatch Engine — Commit Tracking](dispatch-engine.md#commit-tracking-no-auto-commit) for rationale).

**Module**: `backend/git.py`

## Subprocess Abstraction

All git operations use a single `run_git()` function wrapping `subprocess.run` with `capture_output=True, text=True`. The `cwd` parameter is always the project directory — git commands execute in the project's context.

No git library is used. The abstraction is thin and deliberate — subprocess calls are predictable, debuggable, and have no dependency beyond git itself.

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

## Relationship to Other Systems

- [Trigger System](trigger-system.md) depends on the post-commit hook for watch triggers and on `changed_files_in_commit()` for pattern matching
- [Dispatch Engine](dispatch-engine.md) uses `head_hash()` for commit tracking on task start and completion
- [Prompt Assembly](prompt-assembly.md) uses `resolve_glob_files()` to build the subscribed files list
- The feed view in the [Frontend](frontend.md) consumes structured log entries enriched with task metadata
