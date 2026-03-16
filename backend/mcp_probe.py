"""MCP client probe — discover tools from external MCP servers.

Connects to an external MCP server via stdio transport, performs the
initialize/tools/list handshake, and returns the discovered tool names.
Used by the tool inventory endpoint to surface external server capabilities.
"""

import asyncio
import json
import logging

log = logging.getLogger("maistro.mcp_probe")

# Timeout for the full probe handshake (seconds)
PROBE_TIMEOUT = 10


async def probe_server(command: str, args: list[str], env: dict[str, str] | None = None) -> dict:
    """Probe an MCP server and return its tool inventory.

    Returns dict with:
        status: "ok" | "error"
        tools: list of tool name strings (empty on error)
        error: error message (only present on failure)
    """
    import os
    proc_env = {**os.environ, **(env or {})}

    try:
        proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
    except FileNotFoundError:
        return {"status": "error", "tools": [], "error": f"Command not found: {command}"}
    except Exception as e:
        return {"status": "error", "tools": [], "error": str(e)}

    try:
        return await asyncio.wait_for(_handshake(proc), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return {"status": "error", "tools": [], "error": "Probe timed out"}
    finally:
        # Ensure the subprocess is cleaned up
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass


async def _handshake(proc) -> dict:
    """Run the MCP initialize + tools/list handshake."""
    # Send initialize request
    _write(proc.stdin, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "maistro-probe", "version": "1.0.0"},
        },
    })

    # Read initialize response
    init_resp = await _read_response(proc.stdout, expected_id=1)
    if not init_resp:
        return {"status": "error", "tools": [], "error": "No initialize response"}

    # Send initialized notification (no id — it's a notification)
    _write(proc.stdin, {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })

    # Send tools/list request
    _write(proc.stdin, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })

    # Read tools/list response
    tools_resp = await _read_response(proc.stdout, expected_id=2)
    if not tools_resp:
        return {"status": "error", "tools": [], "error": "No tools/list response"}

    if "error" in tools_resp:
        return {"status": "error", "tools": [], "error": tools_resp["error"].get("message", "Unknown error")}

    result = tools_resp.get("result", {})
    tool_names = [t["name"] for t in result.get("tools", []) if "name" in t]
    return {"status": "ok", "tools": tool_names}


def _write(stdin, msg: dict):
    """Write a JSON-RPC message to the process stdin."""
    data = json.dumps(msg) + "\n"
    stdin.write(data.encode("utf-8"))


async def _read_response(stdout, expected_id: int) -> dict | None:
    """Read lines from stdout until we get a JSON-RPC response with the expected id."""
    for _ in range(50):  # safety limit on lines to read
        line = await stdout.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        # Skip notifications (no id)
        if msg.get("id") == expected_id:
            return msg
    return None
