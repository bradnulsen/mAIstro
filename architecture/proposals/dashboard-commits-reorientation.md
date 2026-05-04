# Proposal: Dashboard Reorientation — Commits-Centric Activity View

## Status

Draft.

## Summary

Reorient the Dashboard around git history. Keep the current Job Health and Timeline panels at the top (the parts that work and are operationally useful), then replace the two broken bottom sections — Agent Dispatch Chains and Tool Usage — with a single scrollable commit history table absorbed from the standalone Feed view. The Feed rail entry goes away; the Dashboard becomes the one place to see "what jobs have been doing" both in aggregate (top half) and concretely (bottom half).

The top-half metrics also pivot in flavor: the current health panel is about *task outcomes* (completed / failed / exhausted). We add commit-derived metrics — per-job commit count, lines added/removed, files touched — because those are what the user actually cares about when assessing job impact, and they're cheap to compute by parsing git log authorship.

## Goals

- Combine the Dashboard view and the Feed view into a single Activity surface.
- Delete two sections (Dispatch Chains, Tool Usage) that are broken and not worth fixing.
- Add commit-derived metrics ("this job committed N times, +X / -Y lines across Z files in the window") because that's the question operators ask about agent jobs.
- Preserve the diff drilldown that Feed currently provides — clicking a commit row still opens the same detail panel with changed files and diff text.
- No new backend storage, no schema changes. Read git log + parse the existing `<slug>@maistro.local` authorship convention.

## Non-goals (scope guard)

- **Job/task system, worker, dispatch, queue, coalescing.** None of those are touched.
- **`tasks` / `task_executions` / `task_events` schema.** Read-only consumers; no changes.
- **Worktree integration logic.** The reorientation reads what git already records; it doesn't change *how* tasks land commits.
- **Reintroducing dispatch-chain or tool-usage analytics in a different shape.** They are out, full stop. If a future need surfaces, it gets its own proposal.
- **Operator-vs-agent commit segregation as a UX feature.** Authorship is parsed for attribution, but we don't build a "show only agent commits" filter in v1 — that's a deferred follow-up.

## Conceptual Model

### Layout

The Dashboard becomes a single vertically-stacked view:

```
┌─────────────────────────────────────────────────────────┐
│  Activity                            [Today][7d][30d]   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ── Top half: per-job metrics (kept + extended) ──      │
│                                                         │
│  Job Health (existing) — task outcome %, breakdown      │
│  Job Impact (new)      — commits, +/- lines, files      │
│  Timeline (existing)   — swimlanes of task durations    │
│                                                         │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ── Bottom half: scrollable commit history ──           │
│                                                         │
│  Commit row | author/job | message | files | +/- lines  │
│  Commit row | ...                                       │
│  Commit row | ...                       (paginate/scroll)│
│                                                         │
│  Click a commit → side drawer with diff (existing UX)   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

The window selector ("Today / 7d / 30d") at the top filters **everything below it** — both the metrics and the commit list — to the same time bound. Today's selector only filters metrics; the commit list is fetched independently. Unifying the bound is part of the reorientation.

### What we delete

| Section | Status | Why |
|---|---|---|
| Agent Dispatch Chains (`DispatchChains`, [Dashboard.jsx:276-379](frontend/src/components/Dashboard.jsx#L276-L379)) | Removed entirely | Trigger-detail parsing is fragile, the resulting pattern view is hard to read and rarely surfaces actionable signal. Aggregates are derived in JS from a 50-row sample, so numbers wobble. |
| Tool Usage (`ToolUsage`, [Dashboard.jsx:383-429](frontend/src/components/Dashboard.jsx#L383-L429)) | Removed entirely | Reads `chat_events` joined through `chat_sessions.task_id` → `tasks.job_id`. Has been broken / showing zeros since the chat-session removals; the underlying audit log path is no longer the right place to read tool usage from. Repairing it is out-of-scope churn. |
| Feed view (`Feed.jsx`, rail entry) | Folded into Dashboard | The commit list and diff drilldown are useful, but they don't deserve their own rail tab once Dashboard has them. |

### What we keep (and slightly extend)

- **Job Health** — current task-outcome panel (`HealthSummary`, [Dashboard.jsx:93-147](frontend/src/components/Dashboard.jsx#L93-L147)). Unchanged.
- **Timeline** — current swimlane view (`Timeline`, [Dashboard.jsx:159-243](frontend/src/components/Dashboard.jsx#L159-L243)). Unchanged.
- **Job Impact** *(new)* — commit-derived per-job metrics. See below.

### What we add: Job Impact panel

A grid of per-job cards parallel to Job Health, showing:

- Job name + identity dot
- Commits in window
- Lines added / removed
- Files touched (distinct count)
- (Optional, v2) sparkline of commits per day

Attribution: parse `git log --format=%ae` and match against `<job-id>@maistro.local` to attribute a commit to a specific job. Operator commits (any other email) are aggregated into a single "Operator" pseudo-job for parity, or hidden — see Open Questions.

This panel is the answer to "what have my jobs *actually shipped* this week" — the existing Job Health only tells you success rates, not output volume.

### What we add: Commit history table

A scrollable, virtualized table replaces both defunct sections in the bottom half. Each row shows:

- Author/job dot + name (parsed from email)
- Commit subject
- File count + insertions/deletions stat block
- Relative timestamp
- Trigger icon (carry-over from current Feed)

Click a row → existing detail panel opens (changed files list + diff text via `/api/git/diff/{hash}`). Same UX as today's Feed; we are moving it, not redesigning it.

The table should virtualize at ≥200 rows (current Feed loads 100 unconditionally, which is fine). Scrolling near the bottom triggers a "load more" fetch (`limit` and an offset, or the existing `path` filter parameter). v1 can ship without infinite scroll if 100 rows is enough — most projects have shallower history visible in 7d.

## Backend Changes

### `backend/db_dashboard.py`

- Remove `dashboard_chains()` and `dashboard_tool_usage()` (and any helpers used solely by them).
- Add `dashboard_job_impact(window_days)` that returns per-job commit metrics. Implementation reads `git log --numstat --format=...` for the window, parses authorship, and aggregates. **It does not query `task_executions` or `tasks`** — git is the source of truth here. This keeps the panel honest: it shows what landed, not what was planned.

The git read is bounded by date (`--since=<window>`), so cost scales with window size, not project history depth.

### `backend/dashboard_routes.py`

`/api/dashboard` returns `{window_days, health, timeline, job_impact}` — drop `chains` and `tools`, add `job_impact`. Same gather pattern, three parallel queries instead of four.

### `backend/git_routes.py`

No changes required — `/api/git/log` and `/api/git/diff/{hash}` already serve what the new commit table needs. The existing `limit` parameter is the pagination knob.

### Backend module touch list

- `backend/db_dashboard.py` — drop two helpers, add one
- `backend/dashboard_routes.py` — adjust response shape
- *(no other backend module changes)*

## Frontend Changes

### `frontend/src/components/Dashboard.jsx`

- Delete `DispatchChains` and `ToolUsage` components and their imports.
- Add `JobImpact` component parallel to `HealthSummary`, consuming `data.job_impact`.
- Add `CommitHistory` component that absorbs Feed's commit-list + diff-drawer behavior. The implementation can lift `Feed.jsx` wholesale into a sub-component, with the diff drawer staying as a side panel inside the Dashboard's bottom region.
- The window selector continues to drive `getDashboard(window)`. The commit history fetches via `getGitLog({ limit: 100 })` independently — and **is filtered client-side** by the same window the metrics use, so the commit table and the metrics agree on what "this week" means. (Server-side filtering by `--since` in `git.log()` is a small extension; see Open Questions.)

### `frontend/src/components/Feed.jsx`

Delete. Its logic moves into `Dashboard.jsx` as the `CommitHistory` sub-component. The keyboard ESC-to-close, autorefresh, and diff-drawer behavior all carry over verbatim.

### `frontend/src/App.jsx`

Remove the Feed rail entry and its route case. The Dashboard rail entry covers both responsibilities now. (If a user has a deep link to `/feed` or similar, that's a one-off; there's no router infrastructure to migrate.)

### `frontend/src/api.js`

No new endpoints. `getFeed`, `getGitDiff`, and `getDashboard` all stay — the Feed deletion is purely a UI consolidation.

### CSS

`App.css` already has a `feed-*` class hierarchy and a `dashboard-*` hierarchy. The CommitHistory absorption can reuse the existing `.feed-*` styles unchanged — they're scoped enough to live inside the dashboard container.

## Open Questions

These remain genuinely unresolved.

1. **Operator commits in Job Impact.** Three options: (a) include them as a pseudo-job called "Operator" alongside agent jobs, (b) hide them and show only agent jobs, (c) show them at the bottom collapsed. Recommended default: **(a) — include as "Operator" with a neutral identity dot**, because the operator's commit volume is part of project context. v2 can add a toggle.
2. **Window-bound commit history.** v1 fetches `limit=100` from `/api/git/log` and filters client-side. A cleaner version threads `since=<date>` into `git.log()` so the server returns exactly the window's commits. Low-risk addition but slightly outside this proposal — recommend deferring to "polish" follow-up.
3. **Pagination strategy for the commit table.** Virtualized list with infinite scroll vs. simple `limit + load-more button`. Recommended default: **load-more button** for v1 (simpler, matches the rest of the app's polling-based UX). Virtualization only if the perf benchmark shows 100+ rows is sluggish.
4. **What about jobs that ran a task but produced no commits?** They'll show in Job Health (with whatever terminal status — likely `failed` per the no-commits-fails rule from worktree integration), and absent from Job Impact (zero commits). That's the correct model — Job Impact answers "what shipped," Job Health answers "what ran." But it's worth confirming the asymmetry is intentional in the spec.
5. **Commit table → task linkage.** A commit landed on main is either (a) an operator commit or (b) the integration of an agent task branch. We could decorate agent rows with a "view task #N" link by joining commit hash against `task_executions.result_commit`. Recommended default: **defer to v2** — useful but adds a join + UI affordance, scope creep for v1.
6. **What replaces the Feed rail icon's purpose for muscle memory?** Operators with the Feed tab in their habit will find it gone. Not a real problem — Dashboard is the obvious replacement — but worth noting in release messaging.

## Sequencing

Each step leaves the system working at every commit.

1. **Backend trim.** Remove `dashboard_chains` and `dashboard_tool_usage` from `db_dashboard.py`; adjust `dashboard_routes.py` response shape. Frontend still references `data.chains` / `data.tools` — those sections render their empty-state branches harmlessly (the components already handle missing data). Ship this first to verify nothing depends on those fields elsewhere.
2. **Frontend trim.** Delete `DispatchChains` and `ToolUsage` components from `Dashboard.jsx`. Dashboard now shows two sections (Health, Timeline). Ship.
3. **Backend add.** Implement `dashboard_job_impact(window)` in `db_dashboard.py` and return it from `/api/dashboard`. Backed by `git log --numstat --since` + email parsing.
4. **Frontend add — Job Impact.** Add `JobImpact` component consuming `data.job_impact`. Goes between Health and Timeline (or after Timeline — design call).
5. **Frontend absorb — Commit History.** Move `Feed.jsx` content into `Dashboard.jsx` as `CommitHistory`. Keep diff drawer behavior. Delete `Feed.jsx`. Remove Feed rail entry from `App.jsx`. Ship.
6. **(Optional polish)** Server-side `since=` filter in `git.log`; "view task" decoration on agent commit rows.

Steps 1–2 can ship as one PR (backend + frontend trim of dead sections). Steps 3–4 as another (Job Impact). Step 5 as a third (Feed absorption). Step 6 is a follow-up.
