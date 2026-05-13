"""Shared read-only git tool handlers for the stdio MCP servers.

The internal MCP server (``backend/mcp_server.py``, task agents, cwd=worktree)
and the Governor MCP server (``backend/governor_mcp.py``, cwd=project root)
both expose ``git_status``, ``git_log``, ``git_diff``, ``git_show``. The
handlers live here so they're defined once. cwd is passed explicitly because
the two servers operate in different working directories.

``git_commit`` stays in ``mcp_server.py`` — it enforces a job-specific
authorship/prefix convention and only makes sense in a task context.
"""

import subprocess


def _run_git(*args: str, cwd: str, cap: int | None = None, timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, encoding="utf-8",
            cwd=cwd, timeout=timeout,
        )
        if result.returncode != 0:
            return f"Error: {(result.stderr or result.stdout).strip()}"
        out = result.stdout
        if cap is not None and len(out) > cap:
            out = out[:cap] + f"\n... (truncated at {cap} chars)"
        return out
    except Exception as e:
        return f"Error: {e}"


def git_status_handler(args: dict, *, cwd: str) -> str:
    out = _run_git("status", "--porcelain", cwd=cwd)
    return out if out else "(clean working tree)"


def git_log_handler(args: dict, *, cwd: str) -> str:
    limit = min(int(args.get("limit", 20)), 100)
    path = args.get("path")
    git_args = ["log", f"--max-count={limit}", "--oneline", "--no-decorate"]
    if path:
        git_args.extend(["--", path])
    return _run_git(*git_args, cwd=cwd)


def git_diff_handler(args: dict, *, cwd: str, cap: int | None = None) -> str:
    """commit=<sha>: that commit vs its parent.
    base + head: range diff base..head (use task.start_commit..result_commit).
    neither: working-tree diff vs HEAD.
    name_only=true: file list only. paths=[...]: pathspec filter.
    """
    commit = (args.get("commit") or "").strip()
    base = (args.get("base") or "").strip()
    head = (args.get("head") or "").strip()
    paths = args.get("paths") or []

    git_args = ["diff"]
    if args.get("name_only"):
        git_args.append("--name-only")
    if base and head:
        git_args.append(f"{base}..{head}")
    elif commit:
        git_args.extend([f"{commit}~1", commit])
    else:
        git_args.append("HEAD")
    if paths:
        git_args.append("--")
        git_args.extend(paths)
    return _run_git(*git_args, cwd=cwd, cap=cap)


def git_show_handler(args: dict, *, cwd: str, cap: int | None = None) -> str:
    commit = (args.get("commit") or "").strip()
    if not commit:
        return "Error: commit required"
    git_args = ["show"]
    if args.get("name_only"):
        git_args.extend(["--name-only", "--format=%H %s%n"])
    git_args.append(commit)
    return _run_git(*git_args, cwd=cwd, cap=cap)
