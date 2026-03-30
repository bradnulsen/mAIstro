"""MCP config generation — produce the --mcp-config JSON file for Claude CLI.

Connects dispatched agents to:
  1. The platform's internal MCP server (git operations + project context)
  2. Any external MCP servers enabled for the dispatching job
"""

import json
import os
import sys
import tempfile


def build_mcp_config(
    job: dict,
    project_dir: str,
    session_id: str,
    external_servers: list[dict],
    backend_port: int = 8420,
    task_id: int | None = None,
) -> dict:
    """Build the mcpServers config dict for a task.

    The internal server is always included. External servers are included
    only if listed in the job's mcp_servers property and marked enabled.
    """
    server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")

    job_mcp_names = set(job["properties"].get("mcp_servers") or [])
    props = job["properties"]

    # Serialize list properties for the MCP server environment
    allowed_internal = json.dumps(props.get("allowed_internal_tools") or [])
    allowed_dispatch = json.dumps(props.get("allowed_dispatch_targets") or [])

    servers = {
        "maistro": {
            "type": "stdio",
            "command": sys.executable,
            "args": [server_script],
            "env": {
                "MAISTRO_JOB_ID": str(job["id"]),
                "MAISTRO_JOB_SLUG": job["slug"],
                "MAISTRO_JOB_NAME": job["name"],
                "MAISTRO_PROJECT_DIR": project_dir,
                "MAISTRO_SESSION_ID": session_id,
                "MAISTRO_BACKEND_PORT": str(backend_port),
                "MAISTRO_TASK_ID": str(task_id or ""),
                "MAISTRO_ALLOWED_INTERNAL_TOOLS": allowed_internal,
                "MAISTRO_ALLOWED_DISPATCH_TARGETS": allowed_dispatch,
            },
        }
    }

    for server in external_servers:
        name = server["name"]
        if name in job_mcp_names and server.get("enabled", True):
            servers[name] = {
                "type": "stdio",
                "command": server["command"],
                "args": json.loads(server.get("args") or "[]"),
                "env": json.loads(server.get("env") or "{}"),
            }

    return {"mcpServers": servers}


def write_mcp_config(
    job: dict,
    project_dir: str,
    session_id: str,
    external_servers: list[dict],
    backend_port: int = 8420,
    task_id: int | None = None,
) -> str:
    """Write an MCP config to a temp file and return its path.

    Caller is responsible for deleting the file after the task completes.
    """
    config = build_mcp_config(job, project_dir, session_id, external_servers,
                              backend_port, task_id=task_id)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="maistro-mcp-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path
