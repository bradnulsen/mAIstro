"""Tool inventory and external MCP server CRUD routes.

Exposes the full tool inventory (CLI native + internal MCP + external MCP),
manages registered external MCP server records, and probes them on demand.
Also owns the .mcpb bundle import flow: preview → install with optional
user_config form, plus cancel/reap.
"""

import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend import database as db
from backend import mcp_server, mcpb_import, state
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


# ── .mcpb bundle import ──────────────────────────────────────
#
# Two-phase flow: preview stages and validates; install finalizes by
# stagingId after the operator (optionally) fills in user_config. The
# staging registry is in-memory only — abandoned imports get reaped by
# TTL on the next request.

# stagingId → {staging_dir, manifest, created_at}
_STAGING: dict[str, dict] = {}
_STAGING_LOCK = Lock()
_STAGING_TTL_S = 30 * 60  # 30 minutes

# Cap upload size to avoid unbounded tmp writes from a malicious or
# accidental request body. .mcpb bundles are tiny in practice (a few MB).
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def _reap_staging() -> None:
    cutoff = time.time() - _STAGING_TTL_S
    expired = []
    with _STAGING_LOCK:
        for sid, entry in list(_STAGING.items()):
            if entry["created_at"] < cutoff:
                expired.append((sid, entry))
                _STAGING.pop(sid, None)
    for _, entry in expired:
        mcpb_import.cleanup_staging(entry["staging_dir"])


class BundleInstallRequest(BaseModel):
    staging_id: str
    name: str
    user_config: dict | None = None
    overwrite: bool | None = False


@router.post("/api/mcp/bundles/preview")
async def preview_bundle(req: Request):
    """Stream a .mcpb body to disk, extract+validate, register a staging entry.

    The browser sends the file as the raw request body (Content-Type:
    application/octet-stream). Avoids dragging in a multipart parser for
    a single-file upload.
    """
    require_project()
    _reap_staging()

    project_dir = state.PROJECT_DIR
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mcpb", prefix="maistro-bundle-")
    total = 0
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            async for chunk in req.stream():
                total += len(chunk)
                if total > _MAX_BUNDLE_BYTES:
                    raise HTTPException(413, f"Bundle exceeds size limit ({_MAX_BUNDLE_BYTES} bytes)")
                f.write(chunk)

        if total == 0:
            raise HTTPException(400, "Empty request body")

        try:
            staging_dir, manifest = mcpb_import.extract_to_staging(tmp_path, project_dir)
        except ValueError as e:
            raise HTTPException(400, str(e))

        staging_id = secrets.token_hex(8)
        with _STAGING_LOCK:
            _STAGING[staging_id] = {
                "staging_dir": staging_dir,
                "manifest": manifest,
                "created_at": time.time(),
            }

        log.info("[mcpb] Staged bundle '%s' v%s (id=%s)", manifest.get("name"), manifest.get("version"), staging_id)
        return {
            "staging_id": staging_id,
            "manifest": {
                "name": manifest.get("name"),
                "display_name": manifest.get("display_name"),
                "version": manifest.get("version"),
                "description": manifest.get("description"),
            },
            "user_config_schema": mcpb_import.manifest_user_config_schema(manifest),
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.post("/api/mcp/bundles/install")
async def install_bundle(req: BundleInstallRequest):
    """Finalize a staged bundle into a registered MCP server."""
    require_project()
    _reap_staging()

    if not req.staging_id or not req.name:
        raise HTTPException(400, "staging_id and name are required")

    with _STAGING_LOCK:
        entry = _STAGING.get(req.staging_id)
    if not entry:
        raise HTTPException(404, "Staging entry not found or expired")

    # Conflict check before finalize. The frontend re-POSTs with overwrite=true
    # after the operator confirms.
    existing = await db.list_mcp_servers()
    existing_by_name = {s["name"]: s for s in existing}
    if req.name in existing_by_name and not req.overwrite:
        raise HTTPException(
            409,
            f"Server '{req.name}' already exists",
        )

    # Overwrite path: delete the prior server (cascading any prior bundle dir)
    # before claiming the name.
    if req.name in existing_by_name and req.overwrite:
        await db.delete_mcp_server_cascade(req.name)

    try:
        bundle_dir, server_def = mcpb_import.finalize_bundle(
            entry["staging_dir"],
            entry["manifest"],
            req.user_config or {},
            req.name,
            state.PROJECT_DIR,
        )
    except Exception as e:
        # Staging dir may be partly gone after a failed rename; cleanup is idempotent.
        mcpb_import.cleanup_staging(entry["staging_dir"])
        with _STAGING_LOCK:
            _STAGING.pop(req.staging_id, None)
        raise HTTPException(400, f"Failed to finalize bundle: {e}")

    try:
        await db.create_mcp_server(
            name=req.name,
            command=server_def["command"],
            args=server_def["args"],
            env=server_def["env"],
            bundle_dir=bundle_dir,
        )
    except Exception as e:
        # DB write failed after we already finalized the dir — roll it back so
        # we don't leak an installed bundle without a registry row.
        mcpb_import.remove_bundle(bundle_dir)
        with _STAGING_LOCK:
            _STAGING.pop(req.staging_id, None)
        raise HTTPException(500, f"Failed to register server: {e}")

    with _STAGING_LOCK:
        _STAGING.pop(req.staging_id, None)

    log.info("[mcpb] Installed bundle as server '%s' at %s", req.name, bundle_dir)
    return {
        "name": req.name,
        "command": server_def["command"],
        "bundle_dir": bundle_dir,
    }


@router.delete("/api/mcp/bundles/staging/{staging_id}")
async def cancel_bundle_staging(staging_id: str):
    """Drop a staging entry and clean up its on-disk directory."""
    require_project()
    with _STAGING_LOCK:
        entry = _STAGING.pop(staging_id, None)
    if entry:
        mcpb_import.cleanup_staging(entry["staging_dir"])
    return {"status": "cancelled"}
