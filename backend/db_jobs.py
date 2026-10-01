"""Job CRUD and the EAV property system.

Jobs are the persistent configuration entities of mAistro — name, slug, and
a flexible bag of properties (model, schedule, allowed_tools, subscriptions,
etc.). Every property has a default in `job_property_defs` and an optional
override in `job_properties`. Reads merge defaults+overrides and cast the
text values to their declared types.
"""

import json
import logging
import re

from backend.db_core import get_db

log = logging.getLogger("maistro.database")

_property_defs_cache: list | None = None
_property_schema_cache: tuple[dict, dict] | None = None


def _reset_caches():
    """Clear cached property defs — called by db_core.close_db on project switch."""
    global _property_defs_cache, _property_schema_cache
    _property_defs_cache = None
    _property_schema_cache = None


def slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug


def job_for_commit_author(author: str, jobs: list[dict]) -> dict | None:
    """Resolve a git author name to a job.

    Agent commits carry `user.name = <job name>` (the email is the
    operator's own, so commits verify on GitHub). Exact name match first,
    then slugified name against the job slug so a case/punctuation
    difference still attributes.
    """
    author = (author or "").strip()
    if not author:
        return None
    for job in jobs:
        if job.get("name") == author:
            return job
    slug = slugify(author)
    for job in jobs:
        if slug and job.get("slug") == slug:
            return job
    return None


def _cast_property(value: str, type_: str):
    if type_ == "json":
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if type_ == "integer":
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    if type_ == "boolean":
        return value.lower() in ("true", "1", "yes")
    return value


async def _get_property_defs() -> list:
    """Return job_property_defs rows, using a module-level cache."""
    global _property_defs_cache
    if _property_defs_cache is None:
        conn = await get_db()
        rows = await conn.execute_fetchall("SELECT key, default_value, type FROM job_property_defs")
        _property_defs_cache = [dict(r) for r in rows]
    return _property_defs_cache


async def _get_property_schema() -> tuple[dict, dict]:
    """Return (default_props, def_types).

    `default_props` maps key → cast default value (ready to copy into a job).
    `def_types` maps key → declared type, for casting overrides.

    Cached alongside the underlying defs cache. Cleared on project switch
    via `_reset_caches`.
    """
    global _property_schema_cache
    if _property_schema_cache is None:
        defs = await _get_property_defs()
        def_types = {d["key"]: d["type"] for d in defs}
        default_props = {
            d["key"]: _cast_property(d["default_value"], d["type"])
            for d in defs
        }
        _property_schema_cache = (default_props, def_types)
    return _property_schema_cache


def _overlay_properties(default_props: dict, def_types: dict,
                        raw_overrides: dict) -> dict:
    """Build a per-job property dict by overlaying raw text overrides on defaults.

    `raw_overrides` is the {key: text-value} shape stored in `job_properties`
    (or an empty dict for a job with none). Each override is cast to the
    declared type before assignment. Unknown keys (not in `def_types`)
    fall through as `string`.
    """
    props = default_props.copy()
    for key, value in raw_overrides.items():
        props[key] = _cast_property(value, def_types.get(key, "string"))
    return props


async def create_job(name: str, properties: dict | None = None) -> dict:
    slug = slugify(name)
    db = await get_db()
    cursor = await db.execute("INSERT INTO jobs (slug, name) VALUES (?, ?)", (slug, name))
    job_id = cursor.lastrowid
    if properties:
        for key, value in properties.items():
            val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
            await db.execute(
                "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
                (job_id, key, val)
            )
    await db.commit()
    return await get_job(job_id)


async def get_job(job_id: int, running_ids: set | None = None) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT id, slug, name, created_at FROM jobs WHERE id = ?", (job_id,)
    )
    if not row:
        return None
    job = dict(row[0])

    job_props = await db.execute_fetchall(
        "SELECT key, value FROM job_properties WHERE job_id = ?", (job_id,)
    )
    raw = {p["key"]: p["value"] for p in job_props}

    default_props, def_types = await _get_property_schema()
    props = _overlay_properties(default_props, def_types, raw)

    # Derive running status from tasks table
    if running_ids is not None:
        props["running"] = job_id in running_ids
    else:
        r = await db.execute_fetchall(
            "SELECT 1 FROM tasks WHERE job_id = ? AND status = 'active' LIMIT 1",
            (job_id,)
        )
        props["running"] = bool(r)

    job["properties"] = props
    return job


async def list_jobs() -> list[dict]:
    """Return all jobs. Uses 3 batch queries instead of 2N+2 (N+1 avoided)."""
    conn = await get_db()
    job_rows = await conn.execute_fetchall("SELECT id, slug, name, created_at FROM jobs")
    if not job_rows:
        return []

    # Task state counts per job for command bar indicators
    state_rows = await conn.execute_fetchall(
        "SELECT job_id, status, COUNT(*) as cnt FROM tasks "
        "WHERE status IN ('pending', 'queued', 'active') AND coalesced_id IS NULL "
        "GROUP BY job_id, status"
    )
    running_ids = set()
    pending_counts: dict[int, int] = {}
    queued_counts: dict[int, int] = {}
    for r in state_rows:
        jid, st, cnt = r["job_id"], r["status"], r["cnt"]
        if st == "active":
            running_ids.add(jid)
        elif st == "pending":
            pending_counts[jid] = cnt
        elif st == "queued":
            queued_counts[jid] = cnt

    prop_rows = await conn.execute_fetchall("SELECT job_id, key, value FROM job_properties")
    props_by_job: dict[int, dict] = {}
    for p in prop_rows:
        props_by_job.setdefault(p["job_id"], {})[p["key"]] = p["value"]

    default_props, def_types = await _get_property_schema()

    jobs = []
    for row in job_rows:
        job = dict(row)
        props = _overlay_properties(default_props, def_types,
                                    props_by_job.get(job["id"], {}))
        props["running"] = job["id"] in running_ids
        props["pending_count"] = pending_counts.get(job["id"], 0)
        props["queued_count"] = queued_counts.get(job["id"], 0)
        job["properties"] = props
        jobs.append(job)
    jobs.sort(key=lambda j: j["properties"].get("sort_order", 0))
    return jobs


async def update_job(job_id: int, updates: dict) -> dict | None:
    db = await get_db()
    row = await db.execute_fetchall("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None

    if "name" in updates:
        new_name = updates.pop("name")
        new_slug = slugify(new_name)
        await db.execute("UPDATE jobs SET name = ?, slug = ? WHERE id = ?", (new_name, new_slug, job_id))

    for key, value in updates.items():
        val = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        await db.execute(
            "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, ?, ?)",
            (job_id, key, val)
        )
    await db.commit()
    return await get_job(job_id)


async def reorder_jobs(job_ids: list[int]):
    """Set sort_order for multiple jobs in a single transaction."""
    db = await get_db()
    await db.executemany(
        "INSERT OR REPLACE INTO job_properties (job_id, key, value) VALUES (?, 'sort_order', ?)",
        [(job_id, str(i)) for i, job_id in enumerate(job_ids)],
    )
    await db.commit()


async def delete_job(job_id: int) -> bool:
    """Delete a job. CASCADE FKs on tasks, chat_sessions, and job_properties
    automatically remove dependent rows."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cursor.rowcount > 0


async def get_cascade_targets(completed_job_id: int) -> list[dict]:
    """Return jobs that cascade from the completed job.

    Each job may declare cascades_from: a list of upstream job IDs.
    When a job completes, we find all jobs whose cascades_from list
    includes the completed job and enqueue them.
    """
    conn = await get_db()
    rows = await conn.execute_fetchall(
        "SELECT job_id, value FROM job_properties WHERE key = 'cascades_from'"
    )

    # Parse JSON and filter to jobs that list completed_job_id as upstream
    candidate_ids = []
    for r in rows:
        try:
            upstreams = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(upstreams, list):
            continue
        # Coerce to ints for comparison
        resolved = []
        for item in upstreams:
            try:
                resolved.append(int(item))
            except (ValueError, TypeError):
                continue
        if completed_job_id in resolved:
            candidate_ids.append(r["job_id"])

    if not candidate_ids:
        return []

    placeholders = ",".join("?" * len(candidate_ids))

    job_rows = await conn.execute_fetchall(
        f"SELECT id, slug, name, created_at FROM jobs WHERE id IN ({placeholders})",
        candidate_ids,
    )
    if not job_rows:
        return []

    running_rows = await conn.execute_fetchall(
        "SELECT DISTINCT job_id FROM tasks WHERE status = 'active'"
    )
    running_ids = {r["job_id"] for r in running_rows}

    prop_rows = await conn.execute_fetchall(
        f"SELECT job_id, key, value FROM job_properties WHERE job_id IN ({placeholders})",
        candidate_ids,
    )
    props_by_job: dict[int, dict] = {}
    for p in prop_rows:
        props_by_job.setdefault(p["job_id"], {})[p["key"]] = p["value"]

    default_props, def_types = await _get_property_schema()

    jobs = []
    for row in job_rows:
        job = dict(row)
        props = _overlay_properties(default_props, def_types,
                                    props_by_job.get(job["id"], {}))
        props["running"] = job["id"] in running_ids
        job["properties"] = props
        jobs.append(job)
    return jobs
