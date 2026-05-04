"""Bundle (.mcpb) import lifecycle.

Owns the .mcpb registration path: extract a zip to project storage, validate
its manifest, expose the user_config schema, build a stdio server definition,
and tear down on cancel or removal. No HTTP routing, no DB I/O — the routes
layer in mcp_routes wires the produced def into db.create_mcp_server.

A .mcpb is a zip containing manifest.json (declares name, version, server
entry-point, mcp_config template, optional user_config schema) plus the
server's runnable files. This module mirrors mcp-commander's bundle flow
and skips its node-only restriction so any stdio command works.

Storage layout: <project>/.maistro/bundles/.staging-<id>/ during import,
                <project>/.maistro/bundles/<sanitized-name>-<suffix>/ after finalize.
The .staging- prefix makes orphans distinguishable and reapable on startup.
"""

import json
import logging
import os
import re
import secrets
import shutil
import zipfile

log = logging.getLogger("maistro.mcpb_import")

STAGING_PREFIX = ".staging-"


def storage_root(project_dir: str) -> str:
    return os.path.join(project_dir, ".maistro", "bundles")


def _sanitize_for_dir(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned or "bundle"


def _short_id() -> str:
    return secrets.token_hex(4)


def _is_unsafe_path(name: str) -> bool:
    """Reject zip-slip and absolute paths."""
    if name.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", name):
        return True
    parts = re.split(r"[\\/]", name)
    return ".." in parts


def _extract_zip(zip_path: str, dest_dir: str) -> tuple[bytes | None, set[str]]:
    """Extract zip to dest_dir. Returns (manifest_bytes, set_of_normalized_filenames).

    Rejects encrypted entries and unsafe paths. Compression methods supported
    are whatever zipfile supports (store + deflate cover the universe in practice).
    """
    manifest_buffer: bytes | None = None
    file_names: set[str] = set()

    abs_dest = os.path.abspath(dest_dir)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if _is_unsafe_path(name):
                raise ValueError(f"Unsafe path in archive: {name}")
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted zip entries are not supported")

            target = os.path.abspath(os.path.join(abs_dest, name))
            if not (target == abs_dest or target.startswith(abs_dest + os.sep)):
                raise ValueError(f"Archive entry escapes target dir: {name}")

            if name.endswith("/") or name.endswith("\\"):
                os.makedirs(target, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                data = src.read()
                dst.write(data)

            normalized = name.replace("\\", "/")
            file_names.add(normalized)
            if normalized == "manifest.json":
                manifest_buffer = data

    return manifest_buffer, file_names


def _validate_manifest(manifest: dict, file_names: set[str]) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("Manifest is not a JSON object")
    if not isinstance(manifest.get("name"), str) or not manifest["name"]:
        raise ValueError("Manifest is missing 'name'")
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise ValueError("Manifest is missing 'version'")

    server = manifest.get("server")
    if not isinstance(server, dict):
        raise ValueError("Manifest is missing 'server' object")

    # mAistro accepts any server.type — the mcp_config template defines
    # the runtime via `command`. Upstream mcp-commander hardcodes "node";
    # we relax that since job dispatches are happy with any stdio process.

    entry = server.get("entry_point")
    if not isinstance(entry, str) or not entry:
        raise ValueError("Manifest is missing 'server.entry_point'")
    mcp_config = server.get("mcp_config")
    if not isinstance(mcp_config, dict):
        raise ValueError("Manifest is missing 'server.mcp_config'")
    if not isinstance(mcp_config.get("command"), str) or not mcp_config["command"]:
        raise ValueError("Manifest is missing 'server.mcp_config.command'")

    entry_normalized = entry.replace("\\", "/")
    if entry_normalized.startswith("./"):
        entry_normalized = entry_normalized[2:]
    if entry_normalized not in file_names:
        raise ValueError(f"Declared entry_point not present in archive: {entry}")

    user_config = manifest.get("user_config")
    if user_config is not None:
        if not isinstance(user_config, dict):
            raise ValueError("'user_config' must be an object")
        for field_name, field in user_config.items():
            if not isinstance(field, dict):
                raise ValueError(f"user_config.{field_name} is not an object")
            if not isinstance(field.get("type"), str):
                raise ValueError(f"user_config.{field_name}.type must be a string")


def extract_to_staging(bundle_path: str, project_dir: str) -> tuple[str, dict]:
    """Extract a .mcpb to a staging directory. Returns (staging_dir, manifest_dict).

    Cleans up on any failure so abandoned imports don't leak disk.
    """
    root = storage_root(project_dir)
    os.makedirs(root, exist_ok=True)

    staging_dir = os.path.join(root, f"{STAGING_PREFIX}{_short_id()}")
    os.makedirs(staging_dir, exist_ok=True)

    try:
        manifest_buffer, file_names = _extract_zip(bundle_path, staging_dir)
    except Exception as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise ValueError(f"Failed to extract bundle: {e}") from e

    if manifest_buffer is None:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise ValueError("Bundle is missing manifest.json")

    try:
        manifest = json.loads(manifest_buffer.decode("utf-8"))
    except json.JSONDecodeError as e:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise ValueError(f"manifest.json is not valid JSON: {e}") from e

    try:
        _validate_manifest(manifest, file_names)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return staging_dir, manifest


# ── user_config schema → JSON Schema for the frontend form ────

def manifest_user_config_schema(manifest: dict) -> dict | None:
    """Convert the manifest's user_config field map into a JSON Schema object.

    Returns None when the manifest declares no user_config or an empty map —
    that's the signal to skip the form and install immediately.
    """
    uc = manifest.get("user_config") if isinstance(manifest, dict) else None
    if not uc or not isinstance(uc, dict) or not uc:
        return None

    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, field in uc.items():
        field_type = field.get("type", "string")
        if field_type in ("number", "integer"):
            json_type = field_type
        elif field_type == "boolean":
            json_type = "boolean"
        else:
            # string, directory, file, and anything unknown collect as strings
            json_type = "string"

        prop: dict = {"type": json_type}
        description = " — ".join(p for p in (field.get("title"), field.get("description")) if p)
        if description:
            prop["description"] = description
        if "default" in field:
            prop["default"] = field["default"]
        if field.get("sensitive"):
            prop["sensitive"] = True
        properties[name] = prop

        if field.get("required"):
            required.append(name)

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


# ── Substitution: ${__dirname} / ${user_config.field} ──────

_TOKEN_RE = re.compile(r"\$\{([^}]+)\}")


def _substitute_string(template: str, ctx: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        if key == "__dirname":
            return ctx["__dirname"]
        if key.startswith("user_config."):
            field = key[len("user_config."):]
            value = ctx["user_config"].get(field)
            if value is None:
                return ""
            return str(value)
        return match.group(0)

    return _TOKEN_RE.sub(replace, template)


def _substitute_deep(value, ctx: dict):
    if isinstance(value, str):
        return _substitute_string(value, ctx)
    if isinstance(value, list):
        return [_substitute_deep(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_deep(v, ctx) for k, v in value.items()}
    return value


def _build_def(manifest: dict, user_config_values: dict, bundle_dir: str) -> dict:
    """Run substitution against the manifest's mcp_config template."""
    ctx = {"__dirname": bundle_dir, "user_config": user_config_values or {}}
    template = manifest["server"]["mcp_config"]

    command = _substitute_string(template["command"], ctx)
    args_template = template.get("args") or []
    if not isinstance(args_template, list):
        raise ValueError("mcp_config.args must be a list")
    args = [_substitute_string(a, ctx) if isinstance(a, str) else a for a in args_template]

    env = {}
    env_template = template.get("env") or {}
    if not isinstance(env_template, dict):
        raise ValueError("mcp_config.env must be an object")
    for k, v in env_template.items():
        env[k] = _substitute_string(v, ctx) if isinstance(v, str) else v

    return {
        "command": command,
        "args": args,
        "env": env,
        "bundle_dir": bundle_dir,
    }


def finalize_bundle(
    staging_dir: str,
    manifest: dict,
    user_config_values: dict | None,
    final_name: str,
    project_dir: str,
) -> tuple[str, dict]:
    """Promote a staging dir into a stable bundle dir and produce the server def.

    Returns (bundle_dir, def_dict). Caller persists the def via db.create_mcp_server.
    On any failure after the rename, rolls back the bundle dir so we never
    leak a finalized dir without a corresponding DB row.
    """
    root = storage_root(project_dir)
    safe = _sanitize_for_dir(final_name)
    bundle_dir = os.path.join(root, f"{safe}-{_short_id()}")

    os.rename(staging_dir, bundle_dir)

    try:
        server_def = _build_def(manifest, user_config_values or {}, bundle_dir)
    except Exception:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise

    return bundle_dir, server_def


def cleanup_staging(staging_dir: str | None) -> None:
    if not staging_dir:
        return
    shutil.rmtree(staging_dir, ignore_errors=True)


def remove_bundle(bundle_dir: str | None) -> None:
    if not bundle_dir:
        return
    shutil.rmtree(bundle_dir, ignore_errors=True)


def reap_staging(project_dir: str) -> int:
    """Remove orphan staging directories from prior crashed imports.

    Identified by the .staging- prefix; finalized bundles never carry it,
    so this is safe to bulk-clean on startup.
    """
    root = storage_root(project_dir)
    if not os.path.isdir(root):
        return 0

    removed = 0
    for entry in os.listdir(root):
        if not entry.startswith(STAGING_PREFIX):
            continue
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except Exception as e:
            log.warning("[mcpb] failed to reap staging dir %s: %s", path, e)
    return removed
