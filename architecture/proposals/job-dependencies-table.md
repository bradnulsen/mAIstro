---
title: Replace depends_on LIKE scan with a normalized goal_dependencies table
status: deferred — LIKE fragility resolved via json.loads fix (66183d1); normalization deferred to scale need
author: Backend
reviewed-by: Architect
---

## Problem

Job dependency resolution uses a JSON LIKE pattern scan:

```python
# database.py:331
'SELECT job_id FROM job_properties WHERE key = "depends_on" AND value LIKE ?',
(f'%"{job_id}"%',)
```

This runs after every job completion (`_enqueue_dependents` in `worker.py`). The issues:

1. **No index coverage.** The query must do a full scan of `job_properties` filtered on `key = "depends_on"`. The existing `idx_goal_properties_key` index helps locate the key rows, but then `LIKE '%"job-id"%'` is applied as a post-filter — it cannot be range-bounded.

2. **False positive risk.** If a job ID is a substring of another (e.g., `"build"` matching inside `"build-assets"`), the LIKE pattern can match incorrectly. The quotes around the ID (`%"job-id"%`) narrow this, but only for well-formed JSON values.

3. **Fragile JSON dependency.** The logic depends on `depends_on` being stored as `["job-a","job-b"]` without spaces before/after quotes. Any serialization change would silently break the scan.

## Proposed Change

Extract job dependencies into a dedicated relation:

```sql
CREATE TABLE goal_dependencies (
    dependent_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    upstream_job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    PRIMARY KEY (dependent_job_id, upstream_job_id)
);

CREATE INDEX idx_goal_deps_upstream ON goal_dependencies (upstream_job_id);
```

`get_goals_depending_on(job_id)` becomes a simple equi-join:

```sql
SELECT g.id, g.name, g.created_at
FROM jobs g
JOIN goal_dependencies d ON d.dependent_job_id = g.id
WHERE d.upstream_job_id = ?
```

The `idx_goal_deps_upstream` index makes this O(dependents) instead of O(all dependency properties).

## Migration Considerations

- On schema init, parse existing `depends_on` JSON properties and populate `goal_dependencies` rows.
- `update_goal` must keep `goal_dependencies` in sync when `depends_on` changes.
- The `depends_on` property can remain for API compatibility (read/write via EAV), but dependency resolution switches to the normalized table.
- Cascade delete on both FKs means job deletion automatically cleans up the relation.

## Impact

- `get_goals_depending_on` hot path: O(n) table scan → O(d) index lookup (d = number of dependents, usually 0–3).
- Correctness: exact match replaces approximate string matching.
- Surface area: `create_goal`, `update_goal`, `delete_goal` need to maintain the new table.

## Architect Review

**Deferred.** The problem diagnosis is accurate — LIKE on JSON is fragile and the false-positive risk is real. However, the proposed solution is heavyweight relative to the actual scale:

1. **Scale context**: mAistro projects typically have 5–15 jobs. The `job_properties` table for `depends_on` has at most that many rows. A full scan of 15 rows with a LIKE filter is sub-millisecond. The performance argument doesn't apply at this scale.

2. **Simpler fix available**: The false-positive and fragility concerns can be addressed without a schema change. Replace the LIKE scan with a JSON parse in the query function: fetch all `depends_on` properties, `json.loads()` each value, check for exact membership. This eliminates substring matching risk while keeping the EAV model intact. One function change, no migration.

3. **Dual-write burden**: Maintaining both EAV (`job_properties`) and a normalized table in sync across create/update/delete adds surface area for bugs. The API already reads/writes `depends_on` through EAV — a second source of truth increases the consistency boundary.

**Recommended path**: fix `get_goals_depending_on` to parse JSON and do exact list membership. Revisit normalization if/when the job count reaches a scale where the scan matters (hundreds of jobs) or if dependency queries become more complex (transitive closure, cycle detection).
