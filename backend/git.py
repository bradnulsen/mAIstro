"""Git operations — single abstraction over git subprocess calls."""

import glob as globmod
import os
import subprocess
from datetime import datetime, timezone


def run_git(*args, cwd: str) -> subprocess.CompletedProcess:
    """Run a git command and return the result.

    Explicit encoding="utf-8" is required — on Windows, text=True without it
    uses the system codepage (cp1252), which fails on non-ASCII content in
    commit messages, filenames, or diff output. Git itself outputs UTF-8.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, encoding="utf-8", cwd=cwd,
    )


# ── Repo setup ──────────────────────────────────────────────

def ensure_repo(project_dir: str):
    """Initialize a git repo if one doesn't exist."""
    git_dir = os.path.join(project_dir, ".git")
    if not os.path.isdir(git_dir):
        run_git("init", cwd=project_dir)


def ensure_initial_commit(project_dir: str) -> str | None:
    """Bootstrap an initial commit on a fresh repo so HEAD is non-null.

    The worktree-based dispatch path branches every task off the project's
    main HEAD; a repo from a bare `git init` has no commits, so worktree
    creation fails before the agent ever runs. Bootstrap one empty commit
    (or one that captures `.gitignore` if it was just written) so the
    platform's "every task branches from a real ref" invariant always
    holds. Author is mAistro to keep provenance clear.

    Idempotent: returns None if HEAD already exists.
    """
    if head_hash(project_dir) is not None:
        return None
    gitignore = os.path.join(project_dir, ".gitignore")
    base = ["-c", "user.name=mAistro", "-c", "user.email=maistro@local"]
    if os.path.isfile(gitignore):
        run_git("add", ".gitignore", cwd=project_dir)
        result = run_git(*base, "commit", "-m", "Initialize mAistro project", cwd=project_dir)
    else:
        result = run_git(*base, "commit", "--allow-empty", "-m", "Initialize mAistro project", cwd=project_dir)
    if result.returncode != 0:
        return None
    return head_hash(project_dir)


def commit_gitignore_additions_if_safe(project_dir: str, entries: list[str]) -> bool:
    """Commit `.gitignore` if (and only if) we just appended `entries` to it
    and that's the *only* change in the working tree.

    Project-open's `ensure_gitignore` may have appended platform entries to
    an existing tracked `.gitignore` in a repo that already has commits.
    That dirties the tree and trips the integration path's stash flow
    later. Resolve by committing the change atomically with project-open
    when it's clearly ours, or leaving it alone when the operator has
    other in-flight changes (we don't know what they want to do with the
    tree, so we don't touch it).

    Returns True if a commit was made.
    """
    head = head_hash(project_dir)
    if head is None:
        return False  # ensure_initial_commit handles the no-HEAD case
    # Inspect the working tree. We only auto-commit when .gitignore is the
    # one and only dirty path — anything else is operator-owned.
    full_status = run_git("status", "--porcelain", cwd=project_dir).stdout
    dirty_lines = [l for l in full_status.splitlines() if l.strip()]
    if len(dirty_lines) != 1:
        if dirty_lines:
            # Operator has other in-flight work; leave .gitignore alone too
            return False
        return False
    line = dirty_lines[0]
    # Porcelain format: "XY path"; we want the path portion
    if len(line) < 4:
        return False
    path = line[3:].strip()
    if os.path.normpath(path) != ".gitignore":
        return False
    # Verify the only diff is appending the entries we just added —
    # if the operator was simultaneously editing .gitignore for their
    # own reasons, refuse to commit on their behalf.
    diff = run_git("diff", "--", ".gitignore", cwd=project_dir).stdout
    added_lines = [
        l[1:].strip() for l in diff.splitlines()
        if l.startswith("+") and not l.startswith("+++")
    ]
    if not added_lines:
        return False
    expected = {e.strip() for e in entries if e.strip()}
    actual_added = {l for l in added_lines if l}
    if not actual_added.issubset(expected):
        return False  # Operator is also editing .gitignore — don't touch it
    base = ["-c", "user.name=mAistro", "-c", "user.email=maistro@local"]
    run_git("add", ".gitignore", cwd=project_dir)
    result = run_git(*base, "commit", "-m",
                     "Add mAistro entries to .gitignore", cwd=project_dir)
    return result.returncode == 0


def install_post_commit_hook(project_dir: str, port: int = 8420):
    """Install the maistro post-commit hook."""
    hooks_dir = os.path.join(project_dir, ".git", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "post-commit")

    script = f"""#!/bin/bash
curl -s -X POST http://localhost:{port}/api/hooks/post-commit \\
  -H "Content-Type: application/json" \\
  -d '{{"commit_hash": "'$(git rev-parse HEAD)'"}}' > /dev/null 2>&1 &
"""
    with open(hook_path, "w", newline="\n") as f:
        f.write(script)
    try:
        os.chmod(hook_path, 0o755)
    except OSError:
        pass


def ensure_gitignore(project_dir: str, entries: list[str] | None = None) -> list[str]:
    """Add entries to .gitignore if not already present.

    Returns the list of entries that were appended (empty if the file
    already had everything). Caller decides whether to commit the change
    via `commit_gitignore_additions_if_safe`.
    """
    if entries is None:
        entries = [".maistro/", ".claude/"]
    gitignore = os.path.join(project_dir, ".gitignore")
    existing = ""
    if os.path.exists(gitignore):
        with open(gitignore, "r") as f:
            existing = f.read()
    to_add = [e for e in entries if e not in existing]
    if to_add:
        with open(gitignore, "a") as f:
            for entry in to_add:
                f.write(f"\n{entry}\n")
    return to_add


# ── Log parsing ─────────────────────────────────────────────

# Canonical format string for structured git log output
LOG_FORMAT = "%H|%an|%ae|%s|%ai"


def parse_log_line(line: str) -> dict | None:
    """Parse a single git log line in LOG_FORMAT into a dict."""
    if not line or "|" not in line or line.count("|") < 4:
        return None
    parts = line.split("|", 4)
    return {
        "hash": parts[0],
        "author": parts[1],
        "email": parts[2],
        "message": parts[3],
        "date": parts[4],
    }


def parse_log_output(output: str) -> list[dict]:
    """Parse multi-line git log output into a list of dicts."""
    entries = []
    for line in output.strip().split("\n"):
        entry = parse_log_line(line.strip())
        if entry:
            entries.append(entry)
    return entries


def parse_log_with_files(output: str) -> list[dict]:
    """Parse git log --numstat output (format lines interleaved with file stats)."""
    entries = []
    current = None
    for line in output.strip().split("\n"):
        if not line:
            if current:
                entries.append(current)
                current = None
            continue
        parsed = parse_log_line(line)
        if parsed:
            if current:
                entries.append(current)
            current = {**parsed, "files": [], "insertions": 0, "deletions": 0}
        elif current:
            parts = line.split("\t")
            if len(parts) == 3:
                add, delete, filename = parts
                current["files"].append(filename.strip())
                if add != "-":
                    current["insertions"] += int(add)
                if delete != "-":
                    current["deletions"] += int(delete)
            else:
                current["files"].append(line.strip())
    if current:
        entries.append(current)
    return entries


# ── Query operations ────────────────────────────────────────

def log(cwd: str, limit: int = 50, skip: int = 0, path: str | None = None,
        with_stats: bool = False) -> list[dict]:
    """Get git log entries as structured dicts."""
    args = ["log", f"--max-count={limit}", f"--skip={skip}", f"--format={LOG_FORMAT}"]
    if with_stats:
        args.append("--numstat")
    if path:
        args.extend(["--", path])

    result = run_git(*args, cwd=cwd)
    if result.returncode != 0:
        return []

    if with_stats:
        return parse_log_with_files(result.stdout)
    return parse_log_output(result.stdout)


def log_oneline(cwd: str, limit: int = 20) -> str:
    """Get recent git log as a compact string (for agent context)."""
    result = run_git("log", f"--max-count={limit}", "--oneline", "--no-decorate", cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else ""


def diff(cwd: str, commit_hash: str) -> str:
    """Get diff for a specific commit."""
    result = run_git("diff", f"{commit_hash}~1", commit_hash, cwd=cwd)
    return result.stdout if result.returncode == 0 else ""


def diff_range(cwd: str, from_hash: str, to_hash: str) -> dict:
    """Get diff between two commits with file-level stats and raw diff."""
    # File stats via --numstat
    stat_result = run_git("diff", "--numstat", f"{from_hash}..{to_hash}", cwd=cwd)
    files = []
    total_add = 0
    total_del = 0
    if stat_result.returncode == 0:
        for line in stat_result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 3:
                add, delete, filename = parts
                ins = int(add) if add != "-" else 0
                dels = int(delete) if delete != "-" else 0
                files.append({"path": filename, "insertions": ins, "deletions": dels})
                total_add += ins
                total_del += dels

    # Raw diff
    diff_result = run_git("diff", f"{from_hash}..{to_hash}", cwd=cwd)
    raw = diff_result.stdout if diff_result.returncode == 0 else ""

    return {
        "files": files,
        "insertions": total_add,
        "deletions": total_del,
        "diff": raw,
    }


def show(cwd: str, commit_hash: str, stat: bool = False) -> str:
    """Show a commit. If stat=True, includes --stat."""
    args = ["show", commit_hash, f"--format={LOG_FORMAT}"]
    if stat:
        args.append("--stat")
    result = run_git(*args, cwd=cwd)
    return result.stdout if result.returncode == 0 else ""


def status(cwd: str) -> str:
    """Get working tree status (porcelain format)."""
    result = run_git("status", "--porcelain", cwd=cwd)
    return result.stdout if result.returncode == 0 else ""


def is_dirty(cwd: str) -> bool:
    """Return True if the working tree has uncommitted or untracked changes."""
    return bool(status(cwd).strip())


# ── Worktree operations (per-task workspace isolation) ────

def worktree_add(project_dir: str, path: str, branch: str,
                 base_commit: str) -> tuple[bool, str]:
    """Create a new worktree on a fresh branch from base_commit.

    Returns (ok, error_message). The branch is created (-b) so this fails
    if it already exists — task IDs are unique so this is correct: a stale
    branch from a previous attempt indicates a state that needs operator
    attention, not silent reuse.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    result = run_git("worktree", "add", "-b", branch, path, base_commit, cwd=project_dir)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, ""


def worktree_remove(project_dir: str, path: str, force: bool = False) -> bool:
    """Remove a worktree (does not delete the underlying branch).

    `force=True` allows removing a worktree that has uncommitted changes
    (used in cleanup paths where the work is being deliberately discarded).
    """
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(path)
    result = run_git(*args, cwd=project_dir)
    return result.returncode == 0


def worktree_list(project_dir: str) -> list[dict]:
    """List all worktrees registered with this repo."""
    result = run_git("worktree", "list", "--porcelain", cwd=project_dir)
    if result.returncode != 0:
        return []
    entries: list[dict] = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):].strip()
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip()
        elif line == "detached":
            current["detached"] = True
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
    if current:
        entries.append(current)
    return entries


def worktree_prune(project_dir: str):
    """Clean up registry entries for worktrees whose directories were removed."""
    run_git("worktree", "prune", cwd=project_dir)


def branch_delete(project_dir: str, branch: str, force: bool = False) -> bool:
    """Delete a branch by name."""
    flag = "-D" if force else "-d"
    result = run_git("branch", flag, branch, cwd=project_dir)
    return result.returncode == 0


def branch_exists(project_dir: str, branch: str) -> bool:
    """Return True if a local branch by this name exists."""
    result = run_git("show-ref", "--verify", "--quiet",
                     f"refs/heads/{branch}", cwd=project_dir)
    return result.returncode == 0


def is_ancestor(cwd: str, commit: str, ref: str) -> bool | None:
    """Return True if `commit` is an ancestor of (or equal to) `ref`.

    Returns False if `commit` is definitively not an ancestor (git exit 1),
    None if git couldn't answer (exit 128 — detached HEAD with missing ref,
    unknown commit, broken repo). Callers that lump None into False risk
    silently dropping commits the hook should have processed; check
    explicitly when the distinction matters.
    """
    result = run_git("merge-base", "--is-ancestor", commit, ref, cwd=cwd)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def merge_branch(project_dir: str, source_branch: str,
                 ff_only: bool = False, no_ff: bool = False,
                 message: str | None = None,
                 strategy_option: str | None = None) -> tuple[bool, str]:
    """Merge source_branch into the current branch of project_dir.

    Returns (ok, output_or_error). The caller is expected to have ensured
    project_dir's HEAD is on the integration target (typically main/master).

    `strategy_option` maps to git's -X flag: `theirs` resolves any conflict
    in source_branch's favor; `ours` resolves in the integration target's
    favor. Useful for the "Take task's version" / "Keep mine" buttons in
    the workspace banner. Has no effect on FF merges (no conflict possible).
    """
    args = ["merge"]
    if ff_only:
        args.append("--ff-only")
    if no_ff:
        args.append("--no-ff")
    if strategy_option:
        args.extend(["-X", strategy_option])
    if message:
        args.extend(["-m", message])
    args.append(source_branch)
    result = run_git(*args, cwd=project_dir)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, result.stdout.strip()


def merge_abort(project_dir: str):
    """Abort an in-progress merge and reset the working tree."""
    run_git("merge", "--abort", cwd=project_dir)


def reset_hard(cwd: str, target: str) -> bool:
    """Reset the index and working tree to `target`, discarding all local changes.

    Destructive — used by the integration path to roll back a merge or
    clear a partial stash apply. Caller is responsible for ensuring any
    state that needs to survive the reset has been stashed first.
    """
    result = run_git("reset", "--hard", target, cwd=cwd)
    return result.returncode == 0


def current_branch_name(cwd: str) -> str:
    """Return the current branch name, or 'main' if HEAD is detached/unknown.

    Used to label merge commits and to identify the operator's
    integration branch when integrating a task branch.
    """
    result = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    if result.returncode == 0:
        name = result.stdout.strip()
        if name and name != "HEAD":
            return name
    return "main"


def _format_git_conflict_paths(detail: str) -> tuple[list[str], list[str]]:
    """Extract conflicting paths and any non-boilerplate residual lines from git stderr.

    Recognises the two repetitive shapes git produces during failed merges
    and stash applies — `<path>: already exists, no checkout` (untracked
    overwrite) and `CONFLICT (...): ... <path>` (content overlap) — so the
    operator-facing error can show one line per path instead of git's
    raw output. Returns (sorted_unique_paths, other_lines). Boilerplate
    trailers like "Automatic merge failed; ..." are dropped.
    """
    if not detail:
        return [], []

    paths: list[str] = []
    other: list[str] = []
    boilerplate = (
        "error: could not restore untracked files from stash",
        "Automatic merge failed; fix conflicts and then commit the result.",
    )

    for raw in detail.splitlines():
        line = raw.strip()
        if not line or line in boilerplate:
            continue
        if line.endswith("already exists, no checkout"):
            paths.append(line[: -len("already exists, no checkout")].rstrip(" :"))
            continue
        if line.startswith("CONFLICT") and " in " in line:
            # CONFLICT (content): Merge conflict in <path>
            # CONFLICT (modify/delete): <path> deleted in <ref>
            paths.append(line.rsplit(" in ", 1)[1].strip())
            continue
        other.append(line)

    return sorted(set(paths)), other


def _format_path_list(paths: list[str], limit: int = 12) -> str:
    if not paths:
        return ""
    if len(paths) <= limit:
        return "\n".join(f"  {p}" for p in paths)
    shown = "\n".join(f"  {p}" for p in paths[:limit])
    return f"{shown}\n  …and {len(paths) - limit} more"


def integrate_branch(project_dir: str, branch: str, stash_label: str,
                     merge_message: str | None = None,
                     strategy: str = "default",
                     dedup_label: str | None = None) -> tuple[bool, str]:
    """Merge `branch` into project_dir's HEAD, transparently handling operator-dirty state.

    Stashes any operator-dirty working tree before the merge and re-applies
    after. Operator-wins conflict policy: any failure (merge conflict OR
    post-merge stash conflict) rolls main back to its pre-merge HEAD,
    restores the stash, and returns (False, error). On success returns
    (True, "").

    Used by both the worker's automatic integration after a successful
    task and the manual `POST /workspace/integrate` route. `stash_label`
    is the message attached to the temporary stash so the operator can
    find it if rollback restoration ever fails. `merge_message` is the
    commit message for the no-FF fallback merge; if None, a default is
    generated.

    `strategy` selects conflict resolution behaviour:
      - "default": no -X flag; conflicts fail the integration (operator-wins)
      - "theirs":  -X theirs; conflicts resolve in the task branch's favour
                   (operator says "use the agent's version")
      - "ours":    -X ours;   conflicts resolve in main's favour
                   (operator says "keep mine, but take any new files")

    `dedup_label` is a substring matched against `git stash list` messages.
    Stashes whose message contains it are dropped both before stashing
    (cleaning up debris from prior failed retries on the same task) and
    after a successful integration (the operator state is now part of
    main's history and the stash is redundant). When None, no dedup runs.
    """
    # Dedup retries: drop any prior pre-integrate stashes for this task
    # before creating a new one. Keeps `git stash list` from accumulating
    # N copies of identical operator state across retry attempts.
    if dedup_label:
        stash_drop_matching(project_dir, dedup_label)

    pre_merge_head = head_hash(project_dir)

    pre_stash_ref = None
    if is_dirty(project_dir):
        pre_stash_ref = stash_push_orphan(project_dir, stash_label)
        if not pre_stash_ref:
            return False, ("could not stash operator's dirty working tree "
                           "before merge")

    def _rollback_and_restore() -> None:
        if pre_merge_head:
            current = head_hash(project_dir)
            if current and current != pre_merge_head:
                reset_hard(project_dir, pre_merge_head)
        if pre_stash_ref:
            ok_apply, _ = stash_apply(project_dir, pre_stash_ref)
            if ok_apply:
                stash_drop(project_dir, pre_stash_ref)
            # On apply failure, the stash entry stays in `git stash list`
            # for manual recovery. Caller can surface a warning.

    strategy_opt = None
    if strategy == "theirs":
        strategy_opt = "theirs"
    elif strategy == "ours":
        strategy_opt = "ours"
    elif strategy != "default":
        return False, f"unknown integration strategy {strategy!r}"

    main_branch = current_branch_name(project_dir)
    # FF merge ignores -X (no conflict possible); attempt it first only
    # when strategy is default. With theirs/ours the operator is asking
    # for a specific conflict-resolution behaviour, so always go through
    # the no-FF path even if FF would have worked — the resulting merge
    # commit makes the operator's choice explicit in history.
    if strategy_opt is None:
        ok, out = merge_branch(project_dir, branch, ff_only=True)
    else:
        ok = False
        out = ""
    if not ok:
        msg = merge_message or f"Merge {branch} into {main_branch}"
        ok, out = merge_branch(project_dir, branch, no_ff=True, message=msg,
                               strategy_option=strategy_opt)
        if not ok:
            merge_abort(project_dir)
            _rollback_and_restore()
            paths, other = _format_git_conflict_paths(out)
            sections: list[str] = [
                f"Merge of '{branch}' into {main_branch} hit conflicts. "
                f"Working tree restored; the agent's commits are preserved "
                f"on '{branch}' for manual resolution."
            ]
            if paths:
                sections.append(
                    "Conflicting paths:\n" + _format_path_list(paths)
                )
            if other:
                sections.append("\n".join(other))
            return False, "\n\n".join(sections)

    # Merge succeeded. Restore the operator's stash on top of integrated main.
    if pre_stash_ref:
        ok_apply, apply_err = stash_apply(project_dir, pre_stash_ref)
        if ok_apply:
            stash_drop(project_dir, pre_stash_ref)
        else:
            # Stash re-apply failed — could be content overlap, untracked-
            # overwrite, missing stash, anything. Clear the partial apply,
            # roll the merge back, restore the stash on the original main.
            integrated_head = head_hash(project_dir)
            if integrated_head:
                reset_hard(project_dir, integrated_head)
            _rollback_and_restore()
            paths, other = _format_git_conflict_paths(apply_err)
            sections: list[str] = [
                f"Your dirty working-tree changes overlap with files the "
                f"agent committed on '{branch}', so they couldn't be "
                f"reapplied after the merge. The merge has been rolled "
                f"back and your changes restored; the agent's commits are "
                f"preserved on '{branch}' for manual resolution."
            ]
            if paths:
                sections.append(
                    "Overlapping paths:\n" + _format_path_list(paths)
                )
            if other:
                sections.append("\n".join(other))
            elif not paths:
                sections.append(apply_err or "(no detail from git)")
            return False, "\n\n".join(sections)

    # Success cleanup: any remaining stashes for this task are now
    # subsumed by main's history.
    if dedup_label:
        stash_drop_matching(project_dir, dedup_label)

    return True, ""


# ── Stash operations (orphan-changes safety net) ───────────

def stash_push_orphan(cwd: str, message: str) -> str | None:
    """Stash all working-tree changes (including untracked) and return the stash commit SHA.

    The SHA is captured immediately after the push so subsequent stashes
    don't shift our reference (`stash@{N}` indices change as new entries
    are pushed; the commit SHA is stable). Returns None on failure or if
    nothing was stashed.
    """
    push = run_git("stash", "push", "-u", "-m", message, cwd=cwd)
    if push.returncode != 0:
        return None
    if "No local changes to save" in push.stdout:
        return None
    sha = run_git("rev-parse", "stash@{0}", cwd=cwd)
    if sha.returncode != 0:
        return None
    return sha.stdout.strip() or None


def _find_stash_index(cwd: str, sha: str) -> str | None:
    """Locate a stash entry's current `stash@{N}` ref by its commit SHA."""
    listing = run_git("stash", "list", "--format=%H %gd", cwd=cwd)
    if listing.returncode != 0:
        return None
    for line in listing.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] == sha:
            return parts[1]
    return None


def stash_show(cwd: str, sha: str) -> str:
    """Return the patch for a stashed commit (works whether or not it's still in `stash list`)."""
    result = run_git("show", sha, cwd=cwd)
    return result.stdout if result.returncode == 0 else ""


def stash_apply(cwd: str, sha: str) -> tuple[bool, str]:
    """Re-apply a stash to the working tree. Leaves the stash entry intact.

    Returns (ok, error). On failure `error` carries git's stderr (or stdout
    if stderr is empty) so callers can distinguish content conflicts from
    untracked-overwrite, missing-stash, or lock errors.
    """
    result = run_git("stash", "apply", sha, cwd=cwd)
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


def stash_drop(cwd: str, sha: str) -> bool:
    """Remove a stash entry from `stash list` by its commit SHA.

    Returns True if dropped, False if it could not be located (already
    pruned, expired from reflog, etc.). The underlying commit object may
    still be recoverable via `git fsck --lost-found` until gc runs.
    """
    ref = _find_stash_index(cwd, sha)
    if not ref:
        return False
    result = run_git("stash", "drop", ref, cwd=cwd)
    return result.returncode == 0


def stash_drop_matching(cwd: str, label_substring: str) -> int:
    """Drop every stash entry whose message contains `label_substring`.

    Returns the count dropped. Stable across drops by collecting commit
    SHAs first and dropping by SHA — `stash@{N}` indices shift as entries
    are removed, but commit SHAs don't.

    Used by the integration path to clean up the stash debris that piles
    up when the same task is integrated multiple times — each retry was
    creating a new pre-integrate stash without dropping the prior ones,
    yielding N copies of the same operator state in `git stash list`.
    """
    listing = run_git("stash", "list", "--format=%H %gs", cwd=cwd)
    if listing.returncode != 0:
        return 0
    targets: list[str] = []
    for line in listing.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and label_substring in parts[1]:
            targets.append(parts[0])
    dropped = 0
    for sha in targets:
        if stash_drop(cwd, sha):
            dropped += 1
    return dropped


def changed_files_in_commit(cwd: str, commit_hash: str) -> list[str]:
    """Get list of files changed in a commit."""
    result = run_git("diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash, cwd=cwd)
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


def commit_oneline(cwd: str, commit_hash: str) -> str | None:
    """Get a one-line summary: subject + file count."""
    result = run_git("show", commit_hash, "--format=%s", "--stat", "--stat-width=1", cwd=cwd)
    if result.returncode != 0:
        return None
    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    subject = lines[0] if lines else commit_hash[:8]
    # Last line of --stat is like "3 files changed, 10 insertions(+), 2 deletions(-)"
    stat_line = lines[-1] if len(lines) > 1 and "changed" in lines[-1] else None
    if stat_line:
        return f"{subject} ({stat_line})"
    return subject


def outcome_summary(cwd: str, from_hash: str, to_hash: str) -> str | None:
    """Compute an outcome summary from the commit range — messages and change stats."""
    if from_hash == to_hash:
        return None
    result = run_git("log", "--format=%s", f"{from_hash}..{to_hash}", cwd=cwd)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    messages = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    # Single commit in range — already captured by commit_oneline, skip
    if len(messages) <= 1:
        return None
    # Get overall stat line
    stat_result = run_git("diff", "--shortstat", f"{from_hash}..{to_hash}", cwd=cwd)
    stat_line = stat_result.stdout.strip() if stat_result.returncode == 0 else ""
    parts = []
    for msg in messages:
        parts.append(f"- {msg}")
    if stat_line:
        parts.append(f"({stat_line})")
    return "\n".join(parts)


def build_commit_context(cwd: str, start_commit: str | None, result_commit: str | None) -> str | None:
    """Build a formatted git context string from a commit range.

    This is the singular function for describing what happened between two
    commits.  It replaces ad-hoc combinations of commit_oneline + outcome_summary
    in trigger context building.

    Single commit in range: ``<hash>: <subject> (<stats>)``
    Multiple commits: listed messages with aggregate stats.
    Returns None when there are no commits (same hash or missing).
    """
    if not result_commit or not start_commit or start_commit == result_commit:
        return None

    log_result = run_git("log", "--format=%s", f"{start_commit}..{result_commit}", cwd=cwd)
    if log_result.returncode != 0 or not log_result.stdout.strip():
        return None
    messages = [l.strip() for l in log_result.stdout.strip().split("\n") if l.strip()]

    stat_result = run_git("diff", "--shortstat", f"{start_commit}..{result_commit}", cwd=cwd)
    stat_line = stat_result.stdout.strip() if stat_result.returncode == 0 else ""

    if len(messages) == 1:
        line = f"`{result_commit[:8]}`: {messages[0]}"
        if stat_line:
            line += f" ({stat_line})"
        return line

    # Multiple commits — list messages then aggregate stats
    parts = []
    for msg in messages:
        parts.append(f"- {msg}")
    if stat_line:
        parts.append(f"({stat_line})")
    return "\n".join(parts)


def head_hash(cwd: str) -> str | None:
    """Get current HEAD commit hash."""
    result = run_git("rev-parse", "HEAD", cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


# ── Write operations ────────────────────────────────────────

def commit_file(cwd: str, path: str, message: str) -> str | None:
    """Stage a specific file and commit. Returns commit hash or None."""
    run_git("add", path, cwd=cwd)
    result = run_git("commit", "-m", message, cwd=cwd)
    if result.returncode != 0:
        return None
    return head_hash(cwd)


def read_file(cwd: str, path: str) -> str | None:
    """Read a file from the working tree."""
    filepath = os.path.join(cwd, path)
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def write_file(cwd: str, path: str, content: str):
    """Write a file to the working tree."""
    filepath = os.path.join(cwd, path)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


# ── Glob helpers ───────────────────────────────────────────

def resolve_glob_files(project_dir: str, patterns: list[str]) -> list[dict]:
    """Resolve glob patterns to file metadata dicts."""
    files = []
    seen = set()
    for pattern in patterns:
        for fpath in globmod.glob(os.path.join(project_dir, pattern), recursive=True):
            if fpath in seen or not os.path.isfile(fpath):
                continue
            seen.add(fpath)
            rel = os.path.relpath(fpath, project_dir).replace("\\", "/")
            stat = os.stat(fpath)
            files.append({
                "path": rel,
                "pattern": pattern,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return files
