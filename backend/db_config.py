"""Project-scoped key/value config and external MCP server records.

Config holds runtime toggles (queue_auto_dispatch, agent_dispatch_depth_limit,
etc.) — small set of strings keyed by name. MCP servers are external stdio
processes that jobs can opt into via the `mcp_servers` job property; this
module owns the registration table and the cascade-delete that scrubs the
server name out of any job that referenced it.
"""

import json
import logging
import shutil

from backend.db_core import get_db

log = logging.getLogger("maistro.db_config")


# ── MCP Servers ────────────────────────────────────────────

async def list_mcp_servers() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM mcp_servers")
    return [dict(r) for r in rows]


async def create_mcp_server(
    name: str,
    command: str,
    args: list | None = None,
    env: dict | None = None,
    bundle_dir: str | None = None,
):
    db = await get_db()
    await db.execute(
        "INSERT INTO mcp_servers (name, command, args, env, bundle_dir) VALUES (?, ?, ?, ?, ?)",
        (name, command, json.dumps(args or []), json.dumps(env or {}), bundle_dir),
    )
    await db.commit()


async def update_mcp_server_enabled(name: str, enabled: bool):
    db = await get_db()
    await db.execute(
        "UPDATE mcp_servers SET enabled = ? WHERE name = ?",
        (1 if enabled else 0, name),
    )
    await db.commit()


async def update_mcp_server_fields(name: str, command: str | None, args: list | None, env: dict | None):
    db = await get_db()
    sets, vals = [], []
    if command is not None:
        sets.append("command = ?")
        vals.append(command)
    if args is not None:
        sets.append("args = ?")
        vals.append(json.dumps(args))
    if env is not None:
        sets.append("env = ?")
        vals.append(json.dumps(env))
    if sets:
        vals.append(name)
        await db.execute(f"UPDATE mcp_servers SET {', '.join(sets)} WHERE name = ?", vals)
        await db.commit()


async def delete_mcp_server(name: str):
    db = await get_db()
    await db.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
    await db.commit()


async def get_jobs_referencing_mcp_server(server_name: str) -> list[dict]:
    """Return jobs whose mcp_servers property contains the given server name."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT jp.job_id, jp.value, j.name FROM job_properties jp JOIN jobs j ON j.id = jp.job_id "
        "WHERE jp.key = 'mcp_servers'",
    )
    result = []
    for row in rows:
        try:
            servers = json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            continue
        if server_name in servers:
            result.append({"id": row["job_id"], "name": row["name"]})
    return result


async def delete_mcp_server_cascade(name: str):
    """Delete an MCP server and remove it from all jobs' mcp_servers properties.

    If the server was installed from a bundle, also removes the extracted
    bundle directory. Disk cleanup failures are logged but don't fail the
    delete — the row is gone, only orphan disk state remains.
    """
    conn = await get_db()

    # Capture bundle_dir before we drop the row so we can rm it after commit.
    bundle_rows = await conn.execute_fetchall(
        "SELECT bundle_dir FROM mcp_servers WHERE name = ?", (name,),
    )
    bundle_dir = bundle_rows[0]["bundle_dir"] if bundle_rows else None

    # Find and update all jobs referencing this server
    rows = await conn.execute_fetchall(
        "SELECT job_id, value FROM job_properties WHERE key = 'mcp_servers'",
    )
    for row in rows:
        try:
            servers = json.loads(row["value"])
        except (json.JSONDecodeError, KeyError):
            continue
        if name in servers:
            servers = [s for s in servers if s != name]
            await conn.execute(
                "UPDATE job_properties SET value = ? WHERE job_id = ? AND key = 'mcp_servers'",
                (json.dumps(servers), row["job_id"]),
            )
    await conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
    await conn.commit()

    if bundle_dir:
        try:
            shutil.rmtree(bundle_dir, ignore_errors=False)
        except Exception as e:
            log.warning("[mcp] bundle dir cleanup failed for '%s' at %s: %s", name, bundle_dir, e)


# ── Config ──────────────────────────────────────────────────

async def get_config(key: str | None = None) -> dict | str | None:
    db = await get_db()
    if key:
        rows = await db.execute_fetchall("SELECT value FROM config WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None
    rows = await db.execute_fetchall("SELECT key, value FROM config")
    return {r["key"]: r["value"] for r in rows}


async def set_config(key: str, value: str):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
    )
    await db.commit()


async def get_config_prefix(prefix: str) -> dict[str, str]:
    """Return all config entries whose key starts with prefix, as a dict."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT key, value FROM config WHERE key LIKE ?", (prefix + "%",)
    )
    return {r["key"]: r["value"] for r in rows}
