"""Internal MCP stdio server for mAistro task dispatches.

Provides structured git operations, project context tools, and inter-agent
coordination to dispatched agents. Invoked as a subprocess by the Claude CLI
via --mcp-config.

Each task gets an instance configured via environment variables:
    MAISTRO_JOB_ID                   job slug (e.g. "engineer")
    MAISTRO_JOB_SLUG                 job slug for git authorship
    MAISTRO_JOB_NAME                 display name (e.g. "Engineer")
    MAISTRO_PROJECT_DIR             absolute path to project directory (read source for project-wide context)
    MAISTRO_WORKSPACE_DIR           cwd for the agent's edits and commits (Phase 3a: equals project_dir; Phase 3b: per-task worktree)
    MAISTRO_SESSION_ID              chat session ID for audit logging
    MAISTRO_BACKEND_PORT            backend HTTP port (default 8420)
    MAISTRO_TRIGGER_ID                 current task ID (for provenance)
    MAISTRO_ALLOWED_INTERNAL_TOOLS  JSON list of allowed tool names (empty = all)
    MAISTRO_ALLOWED_DISPATCH_TARGETS JSON list of job IDs this agent can dispatch
    MAISTRO_ALLOW_LEARNING_WRITES   "1" if the dispatching job has the
                                     allow_learning_self_modification permission
                                     enabled — gates add/update/delete_learning

Communication follows the MCP stdio transport (JSON-RPC 2.0, one message per line).
"""

import glob as globmod
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

# ── Context from environment ─────────────────────────────────

JOB_ID = os.environ.get("MAISTRO_JOB_ID", "0")
JOB_SLUG = os.environ.get("MAISTRO_JOB_SLUG", "unknown")
JOB_NAME = os.environ.get("MAISTRO_JOB_NAME", "Unknown")
PROJECT_DIR = os.environ.get("MAISTRO_PROJECT_DIR", ".")
# Where the agent's edits and commits land. Defaults to PROJECT_DIR for
# Phase 3a (no behavior change). Phase 3b populates this with a per-task
# worktree so write-side tools no longer share a tree with the operator
# or with other tasks. Tools that read project-wide context (list_files,
# read_file of project files, list_jobs) keep using PROJECT_DIR; tools
# that write or inspect agent work (git_*, mcp file edits) use WORKSPACE_DIR.
WORKSPACE_DIR = os.environ.get("MAISTRO_WORKSPACE_DIR", PROJECT_DIR)
SESSION_ID = os.environ.get("MAISTRO_SESSION_ID", "")
BACKEND_PORT = int(os.environ.get("MAISTRO_BACKEND_PORT", "8420"))
TASK_ID = os.environ.get("MAISTRO_TRIGGER_ID", "")

# Tool filtering: empty list = all tools available
_allowed_raw = os.environ.get("MAISTRO_ALLOWED_INTERNAL_TOOLS", "[]")
ALLOWED_INTERNAL_TOOLS = set(json.loads(_allowed_raw)) if _allowed_raw else set()

_dispatch_raw = os.environ.get("MAISTRO_ALLOWED_DISPATCH_TARGETS", "[]")
ALLOWED_DISPATCH_TARGETS = set(int(x) for x in json.loads(_dispatch_raw)) if _dispatch_raw else set()

# Per-job permission gate for learning self-modification. Read-only
# list_learnings is always available; the three write tools are filtered
# out of tools/list and refused at tools/call when this flag is false.
ALLOW_LEARNING_WRITES = os.environ.get("MAISTRO_ALLOW_LEARNING_WRITES", "0") == "1"



# ── Git helpers ──────────────────────────────────────────────

def _run_git(*args) -> tuple[bool, str]:
    # All git operations target the workspace — that's where the agent's
    # commits go on the task branch. PROJECT_DIR is the operator's main
    # checkout, which should never be mutated by the agent.
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, encoding="utf-8", cwd=WORKSPACE_DIR,
    )
    ok = result.returncode == 0
    return ok, (result.stdout if ok else result.stderr).strip()


# ── Tool implementations ─────────────────────────────────────

def tool_git_status(args: dict) -> str:
    ok, out = _run_git("status", "--porcelain")
    if not ok:
        return f"Error: {out}"
    return out or "(clean working tree)"


def tool_git_log(args: dict) -> str:
    limit = min(int(args.get("limit", 20)), 100)
    path = args.get("path")
    git_args = ["log", f"--max-count={limit}", "--oneline", "--no-decorate"]
    if path:
        git_args.extend(["--", path])
    ok, out = _run_git(*git_args)
    return out if ok else f"Error: {out}"


def tool_git_diff(args: dict) -> str:
    paths = args.get("paths") or []
    commit = args.get("commit")
    if commit:
        git_args = ["diff", f"{commit}~1", commit]
    else:
        git_args = ["diff", "HEAD"]
    if paths:
        git_args.extend(["--", *paths])
    ok, out = _run_git(*git_args)
    return out if ok else f"Error: {out}"


def tool_git_commit(args: dict) -> str:
    message = (args.get("message") or "").strip()
    paths = args.get("paths") or ["."]

    if not message:
        return "Error: message is required"

    # Enforce [JobName] message prefix
    prefix = f"[{JOB_NAME}]"
    if not message.startswith(prefix):
        message = f"{prefix} {message}"

    # Stage specified paths
    stage = subprocess.run(
        ["git", "add", "--", *paths],
        capture_output=True, text=True, encoding="utf-8", cwd=WORKSPACE_DIR,
    )
    if stage.returncode != 0:
        return f"Error staging files: {stage.stderr.strip()}"

    # Commit with enforced authorship convention
    author = f"{JOB_NAME} <{JOB_SLUG}@maistro.local>"
    ok, out = _run_git("commit", "-m", message, f"--author={author}")
    if not ok:
        return f"Error: {out}"
    return out


# Branch tools disabled while task workspace isolation is fresh.
# - git_branch_switch is unsafe inside a worktree: switching the worktree
#   off `<job-slug>/task-<id>` orphans the agent's commits from the
#   worker's later integration step (which merges that specific branch).
# - git_branch_create / git_branch_merge are not currently used by agents
#   and clutter main's branch namespace from inside the worktree.
# Re-enable selectively if a real workflow needs them; revisit the
# integration story for git_branch_switch first.
#
# def tool_git_branch_create(args: dict) -> str:
#     """Create a new branch from a specified base."""
#     name = (args.get("name") or "").strip()
#     base = (args.get("base") or "HEAD").strip()
#     if not name:
#         return "Error: branch name is required"
#     if not name.startswith(f"{JOB_SLUG}/"):
#         name = f"{JOB_SLUG}/{name}"
#     ok, out = _run_git("branch", name, base)
#     if not ok:
#         return f"Error: {out}"
#     return f"Created branch '{name}' from '{base}'"
#
# def tool_git_branch_switch(args: dict) -> str:
#     """Switch the working directory to a named branch."""
#     name = (args.get("name") or "").strip()
#     if not name:
#         return "Error: branch name is required"
#     ok, out = _run_git("checkout", name)
#     if not ok:
#         return f"Error: {out}"
#     return f"Switched to branch '{name}'"
#
# def tool_git_branch_merge(args: dict) -> str:
#     """Merge a source branch into the current branch."""
#     source = (args.get("source") or "").strip()
#     if not source:
#         return "Error: source branch is required"
#     ok, out = _run_git("merge", source)
#     if not ok:
#         return f"Merge conflict or error:\n{out}"
#     return out or f"Merged '{source}' into current branch"


def tool_list_files(args: dict) -> str:
    pattern = args.get("pattern", "**/*")
    max_files = min(int(args.get("max", 200)), 500)

    # WORKSPACE_DIR so the agent sees their own creates/edits, not the
    # operator's stale view. At task start the worktree mirrors PROJECT_DIR;
    # divergence is the agent's own work.
    matches = []
    full_pattern = os.path.join(WORKSPACE_DIR, pattern)
    for fpath in globmod.glob(full_pattern, recursive=True):
        if not os.path.isfile(fpath):
            continue
        rel = os.path.relpath(fpath, WORKSPACE_DIR).replace("\\", "/")
        size = os.path.getsize(fpath)
        matches.append(f"{rel} ({size}B)")
        if len(matches) >= max_files:
            matches.append(f"... (truncated at {max_files} files)")
            break

    return "\n".join(matches) if matches else "(no files matched)"


def tool_read_file(args: dict) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        return "Error: path is required"

    # Prevent path traversal outside the workspace
    abs_path = os.path.normpath(os.path.join(WORKSPACE_DIR, path))
    if not abs_path.startswith(os.path.normpath(WORKSPACE_DIR)):
        return "Error: path escapes workspace directory"

    if not os.path.isfile(abs_path):
        return f"Error: file not found: {path}"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        return f"Error reading file: {e}"


def tool_list_jobs(args: dict) -> str:
    try:
        url = f"http://localhost:{BACKEND_PORT}/api/jobs/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            jobs = json.loads(resp.read())
        lines = []
        for j in jobs:
            props = j.get("properties", {})
            summary = props.get("summary") or "(no summary)"
            subs = ", ".join(props.get("subscriptions") or []) or "(none)"
            running = " [RUNNING]" if props.get("running") else ""
            lines.append(f"- **{j['name']}**{running}: {summary}")
            lines.append(f"  Subscriptions: {subs}")
        return "\n".join(lines) if lines else "(no jobs configured)"
    except Exception as e:
        return f"Error fetching jobs: {e}"


def tool_dispatch_task(args: dict) -> str:
    """Enqueue a task for another job via agent dispatch."""
    target_job_id = args.get("target_job_id")
    if isinstance(target_job_id, str):
        target_job_id = int(target_job_id) if target_job_id.isdigit() else 0
    message = (args.get("message") or "").strip()

    if not target_job_id:
        return "Error: target_job_id is required"
    if not message:
        return "Error: message is required"
    job_id_int = int(JOB_ID)
    if target_job_id == job_id_int:
        return "Error: self-dispatch is prohibited"
    if not ALLOWED_DISPATCH_TARGETS:
        return "Error: this job has no allowed dispatch targets"
    if target_job_id not in ALLOWED_DISPATCH_TARGETS:
        return f"Error: not allowed to dispatch '{target_job_id}'. Allowed targets: {sorted(ALLOWED_DISPATCH_TARGETS)}"

    try:
        payload = json.dumps({
            "target_job_id": target_job_id,
            "message": message,
            "source_job_id": job_id_int,
            "source_task_id": int(TASK_ID) if TASK_ID else 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://localhost:{BACKEND_PORT}/api/triggers/agent-dispatch",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        return f"Dispatched task #{result['task_id']} for job '{target_job_id}'"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"Error dispatching task: {e.code} — {body}"
    except Exception as e:
        return f"Error dispatching task: {e}"


def tool_get_queue_status(args: dict) -> str:
    """Get read-only view of current queue state."""
    try:
        url = f"http://localhost:{BACKEND_PORT}/api/triggers/queue"
        with urllib.request.urlopen(url, timeout=5) as resp:
            tasks = json.loads(resp.read())

        pending = []
        queued = []
        running = []
        recent_completed = []

        for t in tasks:
            task_line = f"#{t['id']} {t.get('job_name', t['job_id'])} [{t['trigger']}]"
            s = t.get("status", "pending")
            if s == "active":
                running.append(task_line)
            elif s in ("completed", "exhausted", "failed", "cancelled", "interrupted", "timed_out", "rejected"):
                status = "ok" if s == "completed" else "error"
                recent_completed.append(f"{task_line} ({status})")
            elif s == "queued":
                queued.append(task_line)
            else:
                pending.append(task_line)

        parts = []
        if running:
            parts.append(f"Running ({len(running)}):\n" + "\n".join(f"  {l}" for l in running))
        if queued:
            parts.append(f"Queued ({len(queued)}):\n" + "\n".join(f"  {l}" for l in queued))
        if pending:
            parts.append(f"Pending ({len(pending)}):\n" + "\n".join(f"  {l}" for l in pending))
        if recent_completed:
            parts.append(f"Recent completed ({len(recent_completed)}):\n" + "\n".join(f"  {l}" for l in recent_completed[:10]))

        return "\n\n".join(parts) if parts else "(queue is empty)"
    except Exception as e:
        return f"Error fetching queue status: {e}"


# ── Learning tools ──────────────────────────────────────────
# The agent introspects and (when permitted) manages its own job's
# learning list. Backed by the agent-side HTTP routes in
# learnings_routes.py, which enforce source='agent' on writes.

def _job_id_int() -> int:
    try:
        return int(JOB_ID)
    except (ValueError, TypeError):
        return 0


def tool_list_learnings(args: dict) -> str:
    """Breadth view: one-line summaries of enabled learnings for this job.

    Returns id + summary so the agent can scan cheaply, then deep-dive
    selected rows via `read_learnings`. Disabled learnings are excluded —
    the operator muted them for a reason.
    """
    job_id = _job_id_int()
    if not job_id:
        return "Error: job context unavailable"
    try:
        url = f"http://localhost:{BACKEND_PORT}/api/jobs/{job_id}/learnings/summaries"
        with urllib.request.urlopen(url, timeout=5) as resp:
            rows = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"Error fetching learnings: {e.code} — {body}"
    except Exception as e:
        return f"Error fetching learnings: {e}"

    if not rows:
        return "(no learnings)"
    return "\n".join(f"#{r['id']} ({r['source']}): {r['summary']}" for r in rows)


def tool_read_learnings(args: dict) -> str:
    """Depth view: full bodies for the requested learning ids."""
    job_id = _job_id_int()
    if not job_id:
        return "Error: job context unavailable"
    raw_ids = args.get("ids") or []
    ids: list[int] = []
    for x in raw_ids:
        if isinstance(x, int):
            ids.append(x)
        elif isinstance(x, str) and x.isdigit():
            ids.append(int(x))
    if not ids:
        return "Error: ids is required (non-empty list of learning ids from list_learnings)"

    ok, out = _http_request("POST", "/api/learnings/read",
                            {"job_id": job_id, "ids": ids})
    if not ok:
        return f"Error reading learnings: {out}"
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return out
    if not rows:
        return "(no matching learnings)"
    blocks = []
    for r in rows:
        blocks.append(
            f"#{r['id']} ({r['source']}) — {r['summary']}\n{r['body']}"
        )
    return "\n\n".join(blocks)


def _http_request(method: str, path: str, payload: dict | None = None) -> tuple[bool, str]:
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://localhost:{BACKEND_PORT}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"{e.code} — {body}"
    except Exception as e:
        return False, str(e)


def tool_add_learning(args: dict) -> str:
    if not ALLOW_LEARNING_WRITES:
        return "Error: learning self-modification is not enabled for this job"
    job_id = _job_id_int()
    if not job_id:
        return "Error: job context unavailable"
    summary = (args.get("summary") or "").strip()
    body = (args.get("body") or "").strip()
    if not summary:
        return "Error: summary is required (one-sentence index entry)"
    if not body:
        return "Error: body is required"
    ok, out = _http_request("POST", "/api/learnings/agent-add",
                            {"job_id": job_id, "summary": summary, "body": body})
    if not ok:
        return f"Error adding learning: {out}"
    try:
        learning = json.loads(out)
        return f"Added learning #{learning['id']} (position {learning['position']})"
    except (json.JSONDecodeError, KeyError):
        return out


def tool_update_learning(args: dict) -> str:
    if not ALLOW_LEARNING_WRITES:
        return "Error: learning self-modification is not enabled for this job"
    learning_id = args.get("id")
    if isinstance(learning_id, str):
        learning_id = int(learning_id) if learning_id.isdigit() else 0
    if not learning_id:
        return "Error: id is required"

    payload: dict = {}
    summary = args.get("summary")
    body = args.get("body")
    if summary is not None:
        summary = summary.strip()
        if not summary:
            return "Error: summary cannot be empty"
        payload["summary"] = summary
    if body is not None:
        body = body.strip()
        if not body:
            return "Error: body cannot be empty"
        payload["body"] = body
    if not payload:
        return "Error: provide at least one of summary or body"

    ok, out = _http_request("PATCH",
                            f"/api/learnings/{learning_id}/agent-update",
                            payload)
    if not ok:
        return f"Error updating learning: {out}"
    return f"Updated learning #{learning_id}"


def tool_delete_learning(args: dict) -> str:
    if not ALLOW_LEARNING_WRITES:
        return "Error: learning self-modification is not enabled for this job"
    learning_id = args.get("id")
    if isinstance(learning_id, str):
        learning_id = int(learning_id) if learning_id.isdigit() else 0
    if not learning_id:
        return "Error: id is required"
    ok, out = _http_request("DELETE",
                            f"/api/learnings/{learning_id}/agent-delete")
    if not ok:
        return f"Error deleting learning: {out}"
    return f"Deleted learning #{learning_id}"


# ── Tool registry ────────────────────────────────────────────

TOOLS = [
    {
        "name": "git_status",
        "description": "Working-tree status (porcelain).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "git_log",
        "description": "Get recent commit history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max commits to show (default 20, max 100)",
                },
                "path": {
                    "type": "string",
                    "description": "Limit to commits touching this path",
                },
            },
        },
    },
    {
        "name": "git_diff",
        "description": "Working-tree diff, or a commit's diff.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit": {
                    "type": "string",
                    "description": "Commit hash to diff against its parent",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Limit diff to these paths",
                },
            },
        },
    },
    {
        "name": "git_commit",
        "description": f"Commit changes — authorship and [{JOB_NAME}] prefix enforced.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message (the [JobName] prefix is added automatically if missing)",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to stage before committing (default: all changes via '.')",
                },
            },
            "required": ["message"],
        },
    },
    # git_branch_create / git_branch_switch / git_branch_merge are
    # disabled — see the comment block above the tool functions.
    {
        "name": "list_files",
        "description": "List project files matching a glob.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to project root (default: **/*)",
                },
                "max": {
                    "type": "integer",
                    "description": "Max files to return (default 200)",
                },
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a project file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_jobs",
        "description": "List jobs in this project with descriptions and status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "dispatch_task",
        "description": "Enqueue a task on another job (no self-dispatch). Your message becomes the target's context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_job_id": {
                    "type": "integer",
                    "description": "Job ID (integer) to dispatch",
                },
                "message": {
                    "type": "string",
                    "description": "Explanation of why the target job should run",
                },
            },
            "required": ["target_job_id", "message"],
        },
    },
    {
        "name": "get_queue_status",
        "description": "Current queue snapshot: pending, queued, running, recent terminals.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_learnings",
        "description": "Index of this job's enabled learnings (id, summary, source). Scan first; fetch bodies via read_learnings.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "read_learnings",
        "description": "Full body of selected learnings by id (scoped to this job).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Learning ids returned by list_learnings.",
                },
            },
            "required": ["ids"],
        },
    },
    {
        "name": "add_learning",
        "description": "Add a learning (summary + body). Refused at capacity — consolidate or delete an existing one first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One-sentence index entry (max 120 chars).",
                },
                "body": {
                    "type": "string",
                    "description": "Full learning body — the rule, example, or constraint.",
                },
            },
            "required": ["summary", "body"],
        },
    },
    {
        "name": "update_learning",
        "description": "Edit an agent-authored learning (summary and/or body). Operator-authored rows are read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Learning id (from list_learnings).",
                },
                "summary": {
                    "type": "string",
                    "description": "Replacement summary (optional).",
                },
                "body": {
                    "type": "string",
                    "description": "Replacement body (optional).",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "delete_learning",
        "description": "Delete an agent-authored learning. Operator-authored rows are protected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Learning id (from list_learnings).",
                },
            },
            "required": ["id"],
        },
    },
]

TOOL_HANDLERS = {
    "git_status": tool_git_status,
    "git_log": tool_git_log,
    "git_diff": tool_git_diff,
    "git_commit": tool_git_commit,
    # Branch tools disabled — see comment block above tool functions.
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "list_jobs": tool_list_jobs,
    "dispatch_task": tool_dispatch_task,
    "get_queue_status": tool_get_queue_status,
    "list_learnings": tool_list_learnings,
    "read_learnings": tool_read_learnings,
    "add_learning": tool_add_learning,
    "update_learning": tool_update_learning,
    "delete_learning": tool_delete_learning,
}

# Write tools that are gated by the per-job allow_learning_self_modification
# permission. Filtered out of tools/list when the gate is closed (so the
# agent doesn't even see them). Defense in depth at tools/call too.
LEARNING_WRITE_TOOLS = {"add_learning", "update_learning", "delete_learning"}


# ── Audit logging ────────────────────────────────────────────

def _log_tool_call(tool_name: str, input_args: dict, result: str):
    """Log a tool invocation to the backend audit trail (best-effort)."""
    if not SESSION_ID:
        return
    try:
        payload = json.dumps({
            "tool": tool_name,
            "input": input_args,
            "result": result[:2000],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://localhost:{BACKEND_PORT}/api/triggers/mcp-event",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Session-Id": SESSION_ID,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


# ── MCP JSON-RPC protocol ────────────────────────────────────

def _send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _respond(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def handle_initialize(req_id, params):
    _respond(req_id, {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "maistro-internal", "version": "1.0.0"},
    })


def handle_tools_list(req_id, params):
    """Return tools filtered by allowed_internal_tools and the learning-writes gate.

    When ALLOWED_INTERNAL_TOOLS is empty, all tools are available.
    When set, only listed tools are presented.

    Learning write tools are additionally hidden when ALLOW_LEARNING_WRITES
    is false — gating is intersected with allowed_internal_tools so a job
    only sees a write tool if both axes permit it.
    """
    if ALLOWED_INTERNAL_TOOLS:
        filtered = [t for t in TOOLS if t["name"] in ALLOWED_INTERNAL_TOOLS]
    else:
        filtered = list(TOOLS)
    if not ALLOW_LEARNING_WRITES:
        filtered = [t for t in filtered if t["name"] not in LEARNING_WRITE_TOOLS]
    _respond(req_id, {"tools": filtered})


def handle_tools_call(req_id, params):
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    # Enforce tool filtering at call time too (defense in depth)
    if ALLOWED_INTERNAL_TOOLS and tool_name not in ALLOWED_INTERNAL_TOOLS:
        _error(req_id, -32601, f"Tool not available: {tool_name}")
        return
    if tool_name in LEARNING_WRITE_TOOLS and not ALLOW_LEARNING_WRITES:
        _error(req_id, -32601, f"Tool not available: {tool_name}")
        return

    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        _error(req_id, -32601, f"Unknown tool: {tool_name}")
        return

    try:
        result = handler(arguments)
    except Exception as e:
        result = f"Error: {e}"

    _log_tool_call(tool_name, arguments, result)
    _respond(req_id, {"content": [{"type": "text", "text": result}]})


HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def main():
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if req_id is None:
            continue

        handler = HANDLERS.get(method)
        if handler:
            handler(req_id, params)
        else:
            _error(req_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    main()
