"""MCP config generation — produce the --mcp-config JSON file for Claude CLI.

Connects dispatched agents to:
  1. The platform's internal MCP server (git operations + project context)
  2. Any external MCP servers enabled for the dispatching task
"""

import json
import os
import sys
import tempfile


def build_mcp_config(
    task: dict,
    project_dir: str,
    session_id: str,
    external_servers: list[dict],
    backend_port: int = 8420,
) -> dict:
    """Build the mcpServers config dict for a dispatch.

    The internal server is always included. External servers are included
    only if listed in the task's mcp_servers property and marked enabled.
    """
    server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")

    task_mcp_names = set(task["properties"].get("mcp_servers") or [])

    servers = {
        "maistro": {
            "type": "stdio",
            "command": sys.executable,
            "args": [server_script],
            "env": {
                "MAISTRO_TASK_ID": task["id"],
                "MAISTRO_TASK_NAME": task["name"],
                "MAISTRO_PROJECT_DIR": project_dir,
                "MAISTRO_SESSION_ID": session_id,
                "MAISTRO_BACKEND_PORT": str(backend_port),
            },
        }
    }

    for server in external_servers:
        name = server["name"]
        if name in task_mcp_names and server.get("enabled", True):
            servers[name] = {
                "type": "stdio",
                "command": server["command"],
                "args": json.loads(server.get("args") or "[]"),
                "env": json.loads(server.get("env") or "{}"),
            }

    return {"mcpServers": servers}


def write_mcp_config(
    task: dict,
    project_dir: str,
    session_id: str,
    external_servers: list[dict],
    backend_port: int = 8420,
) -> str:
    """Write an MCP config to a temp file and return its path.

    Caller is responsible for deleting the file after the dispatch completes.
    """
    config = build_mcp_config(task, project_dir, session_id, external_servers, backend_port)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="maistro-mcp-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path
