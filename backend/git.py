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


def ensure_gitignore(project_dir: str, entries: list[str] | None = None):
    """Add entries to .gitignore if not already present."""
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


def stash_apply(cwd: str, sha: str) -> bool:
    """Re-apply a stash to the working tree. Leaves the stash entry intact."""
    result = run_git("stash", "apply", sha, cwd=cwd)
    return result.returncode == 0


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
