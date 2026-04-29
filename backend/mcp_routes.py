"""Tool inventory and external MCP server CRUD routes.

Exposes the full tool inventory (CLI native + internal MCP + external MCP),
manages registered external MCP server records, and probes them on demand.
"""

import json
import logging
import os
import shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import database as db
from backend import mcp_server, state
from backend.cli import CLI_NATIVE_TOOLS
from backend.mcp_probe import probe_server
from backend.state import require_project

log = logging.getLogger("maistro.mcp_routes")

router = APIRouter(tags=["mcp"])


class CreateMcpServerRequest(BaseModel):
    name: str
    command: str
    args: list | None = None
    env: dict | None = None


class UpdateMcpServerRequest(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    args: list | None = None
    env: dict | None = None


# Derived from the MCP server's tool registry so the inventory endpoint
# never reports a stale list when tools are added/removed there.
INTERNAL_MCP_TOOLS = [t["name"] for t in mcp_server.TOOLS]


@router.get("/api/tools/inventory")
async def get_tool_inventory(probe: bool = False):
    """Return the full tool inventory: CLI native, internal MCP, and external server tools.

    External server probing is opt-in via ?probe=true to avoid blocking the
    response on subprocess handshakes. Without probing, external servers are
    listed with status "unknown" — the frontend can probe individual servers
    on demand via GET /api/mcp/servers/{name}/tools.
    """
    result = {
        "cli_native": sorted(CLI_NATIVE_TOOLS),
        "internal_mcp": INTERNAL_MCP_TOOLS,
        "external_servers": {},
    }

    if state.PROJECT_DIR:
        servers = await db.list_mcp_servers()
        enabled = [s for s in servers if s.get("enabled", True)]
        disabled = [s for s in servers if not s.get("enabled", True)]

        for s in disabled:
            result["external_servers"][s["name"]] = {"status": "disabled", "tools": []}

        if probe and enabled:
            import asyncio
            probes = await asyncio.gather(*(
                probe_server(
                    s["command"],
                    json.loads(s.get("args") or "[]"),
                    json.loads(s.get("env") or "{}"),
                ) for s in enabled
            ))
            for s, p in zip(enabled, probes):
                result["external_servers"][s["name"]] = p
        else:
            for s in enabled:
                result["external_servers"][s["name"]] = {"status": "unknown", "tools": []}

    return result


def _validate_mcp_server_config(command: str, args: list | None = None, env: dict | None = None):
    """Validate MCP server configuration eagerly at registration/update time."""
    if not command or not command.strip():
        raise HTTPException(422, "Command is required")
    cmd = command.strip()
    if not os.path.isabs(cmd) and not shutil.which(cmd):
        raise HTTPException(422, f"Command not found: '{cmd}' is not on PATH and is not an absolute path")
    if args is not None and not isinstance(args, list):
        raise HTTPException(422, "Arguments must be a list")
    if env is not None:
        if not isinstance(env, dict):
            raise HTTPException(422, "Environment variables must be a key-value object")
        for k, v in env.items():
            if not isinstance(k, str) or not k.strip():
                raise HTTPException(422, "Environment variable keys must be non-empty strings")
            if not isinstance(v, str):
                raise HTTPException(422, f"Environment variable value for '{k}' must be a string")


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    require_project()
    return await db.list_mcp_servers()


@router.post("/api/mcp/servers")
async def create_mcp_server(req: CreateMcpServerRequest):
    require_project()
    _validate_mcp_server_config(req.command, req.args, req.env)
    await db.create_mcp_server(
        name=req.name,
        command=req.command,
        args=req.args,
        env=req.env,
    )
    log.info("[mcp] Registered server '%s' (command=%s)", req.name, req.command)
    return {"status": "created"}


@router.patch("/api/mcp/servers/{name}")
async def update_mcp_server(name: str, req: UpdateMcpServerRequest):
    require_project()
    if req.enabled is not None:
        await db.update_mcp_server_enabled(name, req.enabled)
    if req.command is not None or req.args is not None or req.env is not None:
        if req.command is not None:
            _validate_mcp_server_config(req.command, req.args, req.env)
        await db.update_mcp_server_fields(name, req.command, req.args, req.env)
    return {"status": "updated"}


@router.get("/api/mcp/servers/{name}/tools")
async def probe_mcp_server(name: str):
    """Probe an external MCP server and return its discovered tools and health."""
    require_project()
    servers = await db.list_mcp_servers()
    server = next((s for s in servers if s["name"] == name), None)
    if not server:
        raise HTTPException(404, "Server not found")
    if not server.get("enabled", True):
        return {"status": "disabled", "tools": []}
    return await probe_server(
        server["command"],
        json.loads(server.get("args") or "[]"),
        json.loads(server.get("env") or "{}"),
    )


@router.get("/api/mcp/servers/{name}/jobs")
async def get_mcp_server_jobs(name: str):
    """Return jobs that reference this MCP server in their mcp_servers property."""
    require_project()
    jobs = await db.get_jobs_referencing_mcp_server(name)
    return [{"id": j["id"], "name": j["name"]} for j in jobs]


@router.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    require_project()
    await db.delete_mcp_server_cascade(name)
    log.info("[mcp] Deleted server '%s' (cascade)", name)
    return {"status": "deleted"}
