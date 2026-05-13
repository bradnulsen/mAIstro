"""MCP config generation — produce the --mcp-config JSON file for Claude CLI.

Connects dispatched agents to:
  1. The platform's internal MCP server (git operations + project context)
  2. Any external MCP servers enabled for the dispatching job
"""

import json
import logging
import os
import sys
import tempfile

log = logging.getLogger("maistro.mcp_config")


def build_mcp_config(
    job: dict,
    project_dir: str,
    session_id: str,
    external_servers: list[dict],
    backend_port: int = 8420,
    task_id: int | None = None,
    workspace_dir: str | None = None,
) -> dict:
    """Build the mcpServers config dict for a task.

    The internal server is always included. External servers are included
    only if listed in the job's mcp_servers property and marked enabled.

    ``workspace_dir`` is where the agent's edits and commits should land.
    For Phase 3a it equals project_dir; Phase 3b will pass a per-task
    worktree path. The MCP server uses it for git/file write operations
    while keeping project_dir as the read source for project-wide context.
    """
    # In a PyInstaller bundle, sys.executable is the bundled exe — invoking
    # it with a script path would just re-run uvicorn. The launcher's
    # --mcp-server flag routes the bundled exe into mcp_server.main()
    # instead. In dev, we still pass the script path directly.
    if getattr(sys, "frozen", False):
        cmd_args = [sys.executable, "--mcp-server"]
    else:
        server_script = os.path.join(os.path.dirname(__file__), "mcp_server.py")
        cmd_args = [sys.executable, server_script]

    job_mcp_names = set(job["properties"].get("mcp_servers") or [])
    props = job["properties"]

    # Serialize list properties for the MCP server environment
    allowed_internal = json.dumps(props.get("allowed_internal_tools") or [])
    allowed_dispatch = json.dumps(props.get("allowed_dispatch_targets") or [])
    allow_learning_writes = "1" if props.get("allow_learning_self_modification") else "0"
    allow_self_requeue = "1" if props.get("allow_self_requeue") else "0"

    servers = {
        "maistro": {
            "type": "stdio",
            "command": cmd_args[0],
            "args": cmd_args[1:],
            "env": {
                "MAISTRO_JOB_ID": str(job["id"]),
                "MAISTRO_JOB_SLUG": job["slug"],
                "MAISTRO_JOB_NAME": job["name"],
                "MAISTRO_PROJECT_DIR": project_dir,
                "MAISTRO_WORKSPACE_DIR": workspace_dir or project_dir,
                "MAISTRO_SESSION_ID": session_id,
                "MAISTRO_BACKEND_PORT": str(backend_port),
                "MAISTRO_TRIGGER_ID": str(task_id or ""),
                "MAISTRO_ALLOWED_INTERNAL_TOOLS": allowed_internal,
                "MAISTRO_ALLOWED_DISPATCH_TARGETS": allowed_dispatch,
                "MAISTRO_ALLOW_LEARNING_WRITES": allow_learning_writes,
                "MAISTRO_ALLOW_SELF_REQUEUE": allow_self_requeue,
            },
        }
    }

    for server in external_servers:
        name = server["name"]
        if name in job_mcp_names and server.get("enabled", True):
            try:
                args = json.loads(server.get("args") or "[]")
                env = json.loads(server.get("env") or "{}")
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError(
                    f"MCP server '{name}' has malformed config: {e}"
                ) from e
            if not isinstance(args, list):
                raise ValueError(f"MCP server '{name}': args must be a list, got {type(args).__name__}")
            if not isinstance(env, dict):
                raise ValueError(f"MCP server '{name}': env must be an object, got {type(env).__name__}")
            servers[name] = {
                "type": "stdio",
                "command": server["command"],
                "args": args,
                "env": env,
            }

    return {"mcpServers": servers}


def write_mcp_config(
    job: dict,
    project_dir: str,
    session_id: str,
    external_servers: list[dict],
    backend_port: int = 8420,
    task_id: int | None = None,
    workspace_dir: str | None = None,
) -> str:
    """Write an MCP config to a temp file and return its path.

    Caller is responsible for deleting the file after the task completes.
    """
    config = build_mcp_config(job, project_dir, session_id, external_servers,
                              backend_port, task_id=task_id,
                              workspace_dir=workspace_dir)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="maistro-mcp-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path
