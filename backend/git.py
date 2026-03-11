"""Git operations — single abstraction over git subprocess calls."""

import os
import subprocess
from datetime import datetime, timezone


def run_git(*args, cwd: str) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=cwd,
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


def ensure_gitignore(project_dir: str, entry: str = ".maistro/"):
    """Add an entry to .gitignore if not already present."""
    gitignore = os.path.join(project_dir, ".gitignore")
    if os.path.exists(gitignore):
        with open(gitignore, "r") as f:
            if entry in f.read():
                return
    with open(gitignore, "a") as f:
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
    """Parse git log --name-only output (format lines interleaved with file lists)."""
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
            current = {**parsed, "files": []}
        elif current:
            current["files"].append(line.strip())
    if current:
        entries.append(current)
    return entries


# ── Query operations ────────────────────────────────────────

def log(cwd: str, limit: int = 50, skip: int = 0, path: str | None = None,
        name_only: bool = False) -> list[dict]:
    """Get git log entries as structured dicts."""
    args = ["log", f"--max-count={limit}", f"--skip={skip}", f"--format={LOG_FORMAT}"]
    if name_only:
        args.append("--name-only")
    if path:
        args.extend(["--", path])

    result = run_git(*args, cwd=cwd)
    if result.returncode != 0:
        return []

    if name_only:
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


def changed_files_in_commit(cwd: str, commit_hash: str) -> list[str]:
    """Get list of files changed in a commit."""
    result = run_git("diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash, cwd=cwd)
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


def authored_files(cwd: str, email: str) -> list[str]:
    """Get unique file paths committed by a given author email."""
    result = run_git(
        "log", "--author", email, "--name-only", "--format=", "--diff-filter=ACMR",
        cwd=cwd,
    )
    if result.returncode != 0:
        return []
    seen = set()
    files = []
    for line in result.stdout.strip().split("\n"):
        f = line.strip()
        if f and f not in seen:
            seen.add(f)
            # Only include files that still exist
            if os.path.isfile(os.path.join(cwd, f)):
                files.append(f)
    return files


def head_hash(cwd: str) -> str | None:
    """Get current HEAD commit hash."""
    result = run_git("rev-parse", "HEAD", cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


# ── Write operations ────────────────────────────────────────

def has_changes(cwd: str) -> bool:
    """Check if there are uncommitted changes."""
    return bool(status(cwd).strip())


def commit_all(cwd: str, message: str, author: str | None = None) -> str | None:
    """Stage all changes and commit. Returns commit hash or None."""
    if not has_changes(cwd):
        return None

    run_git("add", "-A", cwd=cwd)

    args = ["commit", "-m", message]
    if author:
        args.extend(["--author", author])

    result = run_git(*args, cwd=cwd)
    if result.returncode != 0:
        return None

    return head_hash(cwd)


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
