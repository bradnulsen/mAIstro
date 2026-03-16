"""Internal MCP stdio server for mAistro task dispatches.

Provides structured git operations and project context tools to dispatched
agents. Invoked as a subprocess by the Claude CLI via --mcp-config.

Each task gets an instance configured via environment variables:
    MAISTRO_JOB_ID          job slug (e.g. "engineer")
    MAISTRO_JOB_NAME        display name (e.g. "Engineer")
    MAISTRO_PROJECT_DIR     absolute path to project directory
    MAISTRO_SESSION_ID      chat session ID for audit logging
    MAISTRO_BACKEND_PORT    backend HTTP port (default 8420)

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

JOB_ID = os.environ.get("MAISTRO_JOB_ID", "unknown")
JOB_NAME = os.environ.get("MAISTRO_JOB_NAME", "Unknown")
PROJECT_DIR = os.environ.get("MAISTRO_PROJECT_DIR", ".")
SESSION_ID = os.environ.get("MAISTRO_SESSION_ID", "")
BACKEND_PORT = int(os.environ.get("MAISTRO_BACKEND_PORT", "8420"))


# ── Git helpers ──────────────────────────────────────────────

def _run_git(*args) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=PROJECT_DIR,
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
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    if stage.returncode != 0:
        return f"Error staging files: {stage.stderr.strip()}"

    # Commit with enforced authorship convention
    author = f"{JOB_NAME} <{JOB_ID}@maistro.local>"
    ok, out = _run_git("commit", "-m", message, f"--author={author}")
    if not ok:
        return f"Error: {out}"
    return out


def tool_list_files(args: dict) -> str:
    pattern = args.get("pattern", "**/*")
    max_files = min(int(args.get("max", 200)), 500)

    matches = []
    full_pattern = os.path.join(PROJECT_DIR, pattern)
    for fpath in globmod.glob(full_pattern, recursive=True):
        if not os.path.isfile(fpath):
            continue
        rel = os.path.relpath(fpath, PROJECT_DIR).replace("\\", "/")
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

    # Prevent path traversal outside project directory
    abs_path = os.path.normpath(os.path.join(PROJECT_DIR, path))
    if not abs_path.startswith(os.path.normpath(PROJECT_DIR)):
        return "Error: path escapes project directory"

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
            desc = props.get("description") or "(no description)"
            subs = ", ".join(props.get("subscriptions") or []) or "(none)"
            running = " [RUNNING]" if props.get("running") else ""
            lines.append(f"- **{j['name']}**{running}: {desc}")
            lines.append(f"  Subscriptions: {subs}")
        return "\n".join(lines) if lines else "(no jobs configured)"
    except Exception as e:
        return f"Error fetching jobs: {e}"


# ── Tool registry ────────────────────────────────────────────

TOOLS = [
    {
        "name": "git_status",
        "description": "Get the working tree status (porcelain format).",
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
        "description": "Get diff for working tree changes or a specific commit.",
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
        "description": (
            f"Commit staged/unstaged changes with enforced authorship "
            f"({JOB_NAME} <{JOB_ID}@maistro.local>) and message prefix ([{JOB_NAME}])."
        ),
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
    {
        "name": "list_files",
        "description": "List files in the project matching a glob pattern.",
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
        "description": "Read a file's contents from the project directory.",
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
        "description": "List all configured jobs in this mAistro project with their descriptions and current status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

TOOL_HANDLERS = {
    "git_status": tool_git_status,
    "git_log": tool_git_log,
    "git_diff": tool_git_diff,
    "git_commit": tool_git_commit,
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "list_jobs": tool_list_jobs,
}


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
            f"http://localhost:{BACKEND_PORT}/api/tasks/mcp-event",
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
    _respond(req_id, {"tools": TOOLS})


def handle_tools_call(req_id, params):
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

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
