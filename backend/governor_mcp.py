"""Governor MCP stdio server — tools for meta-analysis and configuration.

Provides the Governor agent with read access to jobs, tasks, git history,
and health aggregates. Write tools (job config, queue settings) are available
only in execution mode (MAISTRO_GOVERNOR_MODE=write).

Environment variables:
    MAISTRO_PROJECT_DIR     absolute path to project directory
    MAISTRO_BACKEND_PORT    backend HTTP port (default 8420)
    MAISTRO_GOVERNOR_MODE   "read" (analysis) or "write" (suggestion execution)
"""

import json
import os
import subprocess
import sys
import urllib.request

PROJECT_DIR = os.environ.get("MAISTRO_PROJECT_DIR", ".")
BACKEND_PORT = int(os.environ.get("MAISTRO_BACKEND_PORT", "8420"))
GOVERNOR_MODE = os.environ.get("MAISTRO_GOVERNOR_MODE", "read")


# ── HTTP helpers ────────────────────────────────────────────

def _api_get(path: str) -> str:
    try:
        req = urllib.request.Request(f"http://localhost:{BACKEND_PORT}{path}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"error": str(e)})


def _api_post(path: str, data: dict | None = None) -> str:
    try:
        body = json.dumps(data or {}).encode("utf-8")
        req = urllib.request.Request(
            f"http://localhost:{BACKEND_PORT}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"error": str(e)})


def _api_patch(path: str, data: dict) -> str:
    try:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            f"http://localhost:{BACKEND_PORT}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"error": str(e)})


def _api_delete(path: str) -> str:
    try:
        req = urllib.request.Request(
            f"http://localhost:{BACKEND_PORT}{path}", method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool implementations ────────────────────────────────────

def tool_list_jobs(args: dict) -> str:
    return _api_get("/api/jobs/")


def tool_get_recent_tasks(args: dict) -> str:
    limit = args.get("limit", 50)
    return _api_get(f"/api/governor/recent-tasks?limit={limit}")


def tool_get_git_log(args: dict) -> str:
    n = args.get("count", 30)
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{n}"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=10,
        )
        return result.stdout or "(no commits)"
    except Exception as e:
        return f"Error: {e}"


def tool_get_job_health(args: dict) -> str:
    window = args.get("window_days", 7)
    return _api_get(f"/api/dashboard?window={window}")


def tool_get_prior_findings(args: dict) -> str:
    limit = args.get("limit", 30)
    return _api_get(f"/api/governor/findings?limit={limit}")


# Write tools — only available in execution mode

def tool_update_job_properties(args: dict) -> str:
    job_id = args.get("job_id")
    if not job_id:
        return "Error: job_id required"
    properties = args.get("properties", {})
    return _api_patch(f"/api/jobs/{job_id}", {"properties": properties})


def tool_create_job(args: dict) -> str:
    name = args.get("name")
    if not name:
        return "Error: name required"
    payload = {"name": name}
    if args.get("properties"):
        payload["properties"] = args["properties"]
    return _api_post("/api/jobs/", payload)


def tool_delete_job(args: dict) -> str:
    job_id = args.get("job_id")
    if not job_id:
        return "Error: job_id required"
    return _api_delete(f"/api/jobs/{job_id}")


def tool_update_queue_settings(args: dict) -> str:
    return _api_post("/api/queue/settings", args)


# ── Tool definitions ────────────────────────────────────────

READ_TOOLS = [
    {
        "name": "list_jobs",
        "description": "List all jobs with their full configuration and properties.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_tasks",
        "description": "Get recent terminal tasks with execution metadata (status, turns, cost, errors).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max tasks to return (default 50)"},
            },
        },
    },
    {
        "name": "get_git_log",
        "description": "Get recent git commit history with authorship.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of commits (default 30)"},
            },
        },
    },
    {
        "name": "get_job_health",
        "description": "Get per-job health aggregates: success rate, failure count, exhaustion count, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "description": "Time window in days (default 7)"},
            },
        },
    },
    {
        "name": "get_prior_findings",
        "description": "Get your own previous findings with their status (approved/declined/pending for suggestions, read/unread for observations).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max findings to return (default 30)"},
            },
        },
    },
]

WRITE_TOOLS = [
    {
        "name": "update_job_properties",
        "description": "Update properties on an existing job. Can change description, subscriptions, model, max_turns, timeout, schedule, coalesce_tasks, require_approval, allowed_tools, allowed_internal_tools, allowed_dispatch_targets, mcp_servers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "ID of the job to update"},
                "properties": {
                    "type": "object",
                    "description": "Key-value pairs of properties to set",
                },
            },
            "required": ["job_id", "properties"],
        },
    },
    {
        "name": "create_job",
        "description": "Create a new job with a name and optional properties.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name"},
                "properties": {"type": "object", "description": "Initial properties"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_job",
        "description": "Delete a job and all its associated data (tasks, properties, sessions).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "ID of the job to delete"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "update_queue_settings",
        "description": "Update global queue settings (e.g., auto_dispatch).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "auto_dispatch": {"type": "boolean", "description": "Enable auto-queueing of new tasks"},
            },
        },
    },
]

READ_TOOL_NAMES = {t["name"] for t in READ_TOOLS}

TOOL_HANDLERS = {
    "list_jobs": tool_list_jobs,
    "get_recent_tasks": tool_get_recent_tasks,
    "get_git_log": tool_get_git_log,
    "get_job_health": tool_get_job_health,
    "get_prior_findings": tool_get_prior_findings,
    "update_job_properties": tool_update_job_properties,
    "create_job": tool_create_job,
    "delete_job": tool_delete_job,
    "update_queue_settings": tool_update_queue_settings,
}


# ── MCP JSON-RPC protocol ──────────────────────────────────

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
        "serverInfo": {"name": "maistro-governor", "version": "1.0.0"},
    })


def handle_tools_list(req_id, params):
    if GOVERNOR_MODE == "write":
        tools = READ_TOOLS + WRITE_TOOLS
    else:
        tools = READ_TOOLS
    _respond(req_id, {"tools": tools})


def handle_tools_call(req_id, params):
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    # Enforce mode filtering
    if GOVERNOR_MODE != "write" and tool_name not in READ_TOOL_NAMES:
        _error(req_id, -32601, f"Tool not available in read mode: {tool_name}")
        return

    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        _error(req_id, -32601, f"Unknown tool: {tool_name}")
        return

    try:
        result = handler(arguments)
    except Exception as e:
        result = f"Error: {e}"

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
