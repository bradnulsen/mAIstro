"""System-environment routes — no project required.

Exposes information about the host environment the platform depends on
but doesn't manage itself. Today: presence of the Claude Code CLI on
PATH, which is required for any task dispatch.
"""

import logging
import shutil

from fastapi import APIRouter

log = logging.getLogger("maistro.system_routes")

router = APIRouter(tags=["system"])


@router.get("/api/system/claude-status")
async def claude_status():
    """Detect whether the `claude` CLI is reachable on PATH.

    Returns ``{installed: bool, path: str | null}``. The launcher already
    emits a stderr warning when missing; this endpoint lets the UI surface
    the same state as a status pill alongside MCP server health.
    """
    path = shutil.which("claude")
    return {"installed": path is not None, "path": path}
