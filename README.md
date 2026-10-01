# mAistro

**A canvas for building your own LLM harness.**

mAistro lets you turn ad-hoc "ask the AI to do something" into a configurable, controllable, observable system. You describe the work you want done, decide how much autonomy the agent gets, and the platform handles the rest — triggering, prompting, streaming, recording, and gating.

There is no single right way to use it. You can run it as a fully manual tool — type a prompt, watch the agent work, approve or reject the result. You can run it as a fully autonomous teammate — agents wake up on a schedule, react to your commits, hand work off to each other, and only ping you when something needs your attention. Most usage is somewhere in between, and the platform is designed for you to slide along that spectrum without rebuilding anything.

Every dimension is yours to tune: when work happens, what tools it can use, whether you approve before it runs, how long it can think for, which other agents it can call. mAistro is the substrate; your jobs are the configuration.

---

## Quick Start

You need the [Claude Code CLI](https://claude.ai/code) installed and authenticated — mAistro invokes it as a subprocess to run agents. Then pick a path:

### Run the installed build (Windows)

Grab `maistro-setup-<version>.exe` from a release, run it, and click the new Start-Menu icon. The launcher opens `http://localhost:8420` in your default browser. From there, pick a project directory — mAistro initializes a `.maistro/` folder inside it (gitignored), installs a git post-commit hook, and you're ready.

Per-user state (recent projects, job templates) lives at `%APPDATA%\mAistro\` and survives uninstall. Per-project state lives at `<project>\.maistro\`.

### Run from source (any platform)

```bash
# Backend
pip install -r requirements.txt
python run.py

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 (Vite proxies `/api` and `/health` to the backend on :8420). Source runs default the app-data directory to a repo-local `.maistro/` so an active checkout keeps its own state.

### Build the installer yourself

```powershell
pip install pyinstaller
# plus Inno Setup 6 from https://jrsoftware.org/isdl.php
./installer/build.ps1 -Version 0.1.0
```

See [installer/README.md](installer/README.md) for what the build produces and what survives uninstall.

---

## Getting Started: Scenarios

Pick the one that's closest to what you want to do. Each takes 1–2 minutes to set up.

### "I just want to ask an agent to do one thing"

The simplest mode. Open the **Jobs** view, create a job:

- **Name**: e.g. "Cleanup"
- **Description**: what good output looks like — "Remove unused imports across the project."

Drop into the **Dispatch** view, click "Run" on the job. A task lands in **Queued**, the worker picks it up, and you watch it stream in real time. The agent commits its own changes as it works. When it's done, you see the diff inline.

That's it. No triggers, no schedule, no autonomy beyond the single run you initiated.

### "I want an agent to maintain my docs whenever I change the code"

Set up a watch. Create a job:

- **Description**: "Update README.md so it accurately reflects the current state of the code in src/. Don't add fluff."
- **Subscriptions** (glob patterns): `src/**/*.ts`, `src/**/*.tsx`

Now every commit you make to anything under `src/` enqueues a task for this job. By default, tasks land in **Pending** — a staging area where you decide whether they're worth running. Promote one to **Queued** by dragging it; the worker picks it up.

If you'd rather skip the staging step entirely, flip auto-queueing on in **Settings**. New tasks now skip Pending and run automatically.

If you commit a flurry of changes, mAistro **coalesces** them — instead of stacking 12 redundant runs, it merges the triggers into one task that addresses all of them.

### "I have a chore I keep forgetting"

Set a schedule. Create a job, set the **schedule** property to a cron expression:

- `0 9 * * 1` — every Monday at 9 AM ("weekly triage")
- `0 18 * * *` — every day at 6 PM ("end-of-day summary")

The scheduler enqueues the task when the expression fires. Schedule triggers always coalesce — if a scheduled task is still pending when the next fire happens, it absorbs the new trigger rather than piling up.

### "I want a multi-step pipeline"

Chain jobs with dependencies. Job A's `depends_on` field is empty; Job B's `depends_on` references Job A. When A completes successfully, B is enqueued automatically with A's commit range as context.

Common pattern: **Designer** → **Implementer** → **Reviewer**. Each is a job with focused instructions. The platform does the hand-off.

Only successful completion triggers downstream — if A fails, times out, or runs out of turns, B does not fire. That's intentional: you want the next agent reasoning about real progress, not partial garbage.

### "I want to gate risky agents"

Set `require_approval` on the job. Tasks land in pending-approval and wait for you to explicitly approve or reject. The worker skips them.

Manual dispatches bypass the gate (you initiated it, you approved it). Triggered runs (commit, schedule, dependency, agent) all wait.

Use this for jobs with broad blast radius — anything touching configuration, deployment, or destructive operations.

### "I want to ask the agent why it did something after the fact"

Open a resolved task and use **Reply** to ask a follow-up question. The session resumes with full prior context.

For one-off corrections, use **Resume** to continue the same CLI session, or **Retry** to re-run a failed task.

---

## Building a Mental Model

mAistro is built around five concepts. Once these click, the rest of the surface is just configuration.

### Jobs vs. Tasks

A **job** is a *named configuration*. It says: "here's a kind of work, here are the instructions, here's the model, here's when to run." Jobs are the nouns of the system. You create, edit, and delete them.

A **task** is a *single execution* of a job. Tasks are the verbs — what actually runs. One job produces many tasks over time.

The platform's whole UI revolves around this split. The **Jobs** view is where you configure; **Dispatch** is where tasks live and run.

### The Two-Stage Queue

Every task moves through:

```
Pending  →  Queued  →  Active  →  Resolved
```

- **Pending** is the staging area. New tasks land here by default. You decide if they're worth running.
- **Queued** is the runway. Tasks here are committed to run; the worker pulls the top one when it's free.
- **Active** is the one task currently executing. Only one runs at a time.
- **Resolved** is everything terminal — completed, failed, cancelled, exhausted, timed out, interrupted, rejected.

The split between Pending and Queued is the system's main lever for control. The further left a task sits, the more involvement you have. Auto-queue everything if you want the system aggressive; leave auto-queue off to curate every run.

### Triggers Decide When Work Happens

There are five ways a task gets created:

| Trigger | Fires when… |
|---|---|
| **Manual** | You click Run |
| **Commit** | A git commit changes files matching the job's subscriptions |
| **Schedule** | A cron expression fires |
| **Dependency** | An upstream job completes successfully |
| **Agent** | Another running agent calls `dispatch_task` |

Plus three continuation triggers — **Resume**, **Reply**, **Retry** — that operate on existing tasks.

A single job can have any combination. A docs-maintainer might use Commit + Schedule (weekly health-check). A reviewer might use Dependency + Manual.

### Tools Are Granted, Not Assumed

Each job has three independent tool surfaces:

- **CLI tools** (`allowed_tools`) — the built-in Claude Code tools (Edit, Write, Bash, etc.)
- **Internal MCP** (`allowed_internal_tools`) — mAistro-provided tools: git ops, project context, inter-agent dispatch
- **External MCP** (`mcp_servers`) — third-party MCP servers (your own integrations)

You compose them per job. A read-only investigator gets only `Read`, `Grep`, `Glob`. A full implementer gets the lot. A scheduled metrics agent gets a Slack MCP server but no file write access.

The default is *narrow*. Grant only what the job needs.

### Git Is the Source of Truth

mAistro never auto-commits. Agents commit their own work with the job name as git author name (`user.name = <JobName>`) so you can always trace who did what. The author email is your own global git email, so your `commit.gpgsign` signature verifies on GitHub.

Project content lives in git. Operational state (job configs, task records, audit logs) lives in `.maistro/maistro.db` — a SQLite file inside the project, gitignored. Two databases per project: nothing leaks across project boundaries.

If you want to undo what an agent did, you use git. mAistro is not the system of record for your code; git is.

---

## More Advanced

Once the basics click, the rest is dimensions you can tune.

### Tuning Autonomy

The autonomy spectrum runs from "ask me before every step" to "run while I sleep." You move along it by setting:

- **`require_approval`** — gate triggered runs behind manual approval
- **Auto-queueing setting** — whether new tasks land in Pending (curate) or Queued (just run)
- **`max_turns`** — how much agent reasoning per task (default 100). Lower = tighter scope, more failures from hitting the wall. Higher = more freedom, more cost.
- **`timeout`** — wall-clock kill switch (default 900s). The watchdog terminates and marks the task `timed_out`.
- **`coalesce_tasks`** — globally collapse all triggers for a job to at most one pending task. Aggressive deduplication.
- **`allowed_dispatch_targets`** — which other jobs an agent inside this job can call. Leave empty to disable inter-agent dispatch entirely.

You're not picking a single "level" — you're tuning each axis. A job can be aggressive on triggering (broad subscriptions, watch active) and conservative on execution (low turn limit, approval required, narrow tools).

### Coalescing Is Your Friend

Most "I committed 30 things in a row, did I just trigger 30 docs runs?" worries are handled automatically. Triggers with the same intent collapse into one task as long as the original is still pending or queued.

When automatic coalescing isn't enough, you can do it yourself: drag-merge tasks together in the Pending column to combine them into one run. Drag-split a coalesced task to break it apart. You see exactly which triggers contributed to a given task.

A coalesced task carries *all* the trigger contexts — the agent sees every reason it was woken up.

### Inter-Agent Dispatch

Agents can call other agents. Inside an active task, the agent has access to a `dispatch_task` MCP tool — it picks a job (within `allowed_dispatch_targets`), gives a reason, and the platform enqueues a new task with that context.

This is how you build composable agent systems: a high-level "ship feature X" job dispatches sub-jobs for design, implementation, and review. Loop prevention is built in (depth limits, cycle detection).

Coalescing applies — if the dispatched job already has a pending task, the new trigger merges into it.

### The Governor

Every 10 successful tasks, the **Governor** runs. It's a meta-agent — its job is to look at how *your* jobs are performing and surface what's worth your attention.

You see Governor output in the **Governor** view as **threads**: a list of conversations, each with a subject and a chronological message log. Threads can be opened by either side — the Governor opens a thread when a survey turns up something worth flagging; you open one when you want a question answered or a change considered. Reply, close, or reopen any thread. Closed threads disappear from the open list (they're still queryable, but the Governor itself has no notion of mute or escalation — open/closed is a human-only signal).

When the Governor wants to propose a configuration change, it attaches a *proposal card* to its message — a structured payload describing what it would do and why. You can approve a card to apply it, or just reply in prose to keep the thread going. Plain text answers don't need approval; only proposal cards mutate state.

The Governor doesn't touch your project code. It reads task history and threads, and writes back to its own conversation log. Think of it as an embedded operator — the eyes you'd otherwise need to keep on the dashboard.

You can also trigger Governor runs manually if you want a fresh assessment.

### Job Templates

Jobs you find yourself recreating across projects can be saved as templates in the app-level store. New project, switch into it, paste the template. Cross-project knowledge without copy-pasting configuration.

### External MCP Servers

Beyond the platform's internal tool surface, you can register your own MCP servers per project — Slack, GitHub, your CRM, anything that speaks MCP. Add the server in **Settings**, then list it in the relevant job's `mcp_servers`. The platform health-checks before dispatch and surfaces failures by server name.

### Resume, Reply, Retry — Three Continuations

- **Resume** continues the agent's session. The CLI re-enters with full prior context. Use this when an agent stopped short and you want it to keep going.
- **Reply** starts a new session but provides the prior task as context. Use this for follow-up questions or new directions.
- **Retry** re-runs a failed or timed-out task from scratch.

All three preserve provenance — the original task remains as a coalesced subordinate of the new one, so you can always trace the chain.

### Tool Manifests and the Job Registry

Every task's prompt includes a manifest of *all* jobs in the project — their names, summaries, and subscriptions. Agents know what other agents exist. This is what makes inter-agent dispatch sensible: an agent isn't guessing what's available; it has the directory.

### Observability

- **Dispatch** view: live three-column kanban, drag to merge/split/transfer, click a task for the bottom drawer with output, diff, and timeline.
- **Feed**: git activity for the project, with task associations.
- **Dashboard**: aggregate health, task timeline, dispatch chains, tool-usage patterns. Time-window filtered.
- **Files**: project file browser with syntax highlighting.

Every CLI event is persisted as a raw audit row. Every status transition is event-sourced. You never have to guess what an agent did.

---

## Addendum: Patterns for Industrial Controls

The mechanisms above are domain-agnostic. This section is targeted: industrial controls and the software engineers who support them.

The reality of industrial controls work is that artifacts span many file types — PLC exports (`.L5X`, `.ACD`, `.xef`), HMI projects (FactoryTalk View, Ignition, WinCC), tag databases (CSV/Excel), drawings (`.dwg`, PDF P&IDs), specs (Word, PDF), historian configs, alarm lists, recipes (S88) — and they constantly drift out of sync. The narrative says one thing, the code does another, the IO list shows a third tag name, the HMI displays a fourth. Tag databases, alarm lists, loop sheets, sequences of operation — all of these need to stay in sync but rarely do because they live in different files maintained by different people. That drift is mAistro's home turf.

mAistro doesn't care that a file is `.L5X` or `.docx` or `.dwg` or a CSV. A glob match is a glob match; a commit is a commit. Once your artifacts are in a git repo (even just exports of vendor binaries), the platform turns them into a reactive substrate.

### 1. The "Drift detector" — commit watch across artifact types

- A **Narrative-vs-code reconciler** with `subscriptions: ["docs/Control_Narrative.docx", "plc_export/**/*.L5X"]`. On any change to either, the agent diffs the described sequence against the actual logic and surfaces discrepancies as a markdown report.
- A **Tag-name compliance** job: watches `tags/iolist.csv`, validates against your naming standard (ISA-5.1, KKS, or whatever the corporate one is), commits a `tag_audit.md` with violations.
- A **Loop-sheet ↔ tag-database** synchronizer: when the IO list changes, regenerate loop sheets (Markdown or even CSV the drawings team can paste into AutoCAD).

Coalescing matters here — a checkin with 200 tag changes shouldn't trigger 200 audits. It collapses to one.

### 2. The "Adversarial pair" — Builder + Skeptic on safety code

Safety-related code (SIF logic, interlock matrices, permissive chains) deserves a paranoid second pair of eyes that *cannot* edit.

- **Interlock-Implementer** writes/modifies routines in the PLC export.
- **Interlock-Skeptic** depends on Implementer; tools restricted to `Read`/`Grep` only — its job is to find every implicit assumption (de-energize-to-trip? fail-safe state on power loss? bypass logic? SIL allocation match?). Outputs a review note, doesn't touch the code.
- The Skeptic's commit triggers a human-approval task that holds the change before it can be merged downstream.

Same pattern for **recipe writer + recipe critic** in S88 batch work, or **HMI navigation builder + ISA-101 critic**.

### 3. The "Council" — multi-discipline review of one change

Multidisciplinary review is built into industrial controls (controls + electrical + process + safety) but it almost never happens *concurrently*.

A change to the PLC export triggers four reviewer jobs via `depends_on` on the same upstream:

- **Electrical** — checks that new IO references match the wiring schedule.
- **Process** — reads the P&ID PDF (via a PDF MCP) and checks tag references exist on the drawing.
- **Functional-safety** — checks SIF integrity, response-time annotations.
- **Standards** — ISA-18.2 alarm rationalization compliance, ISA-5.1 naming.

A **Synthesizer** depends on all four, produces a single review packet for the engineer.

You don't replace the human review — you front-run it with a draft so the human is reviewing the *interesting* findings, not the easy ones.

### 4. The "Stigmergic" pattern — coordination through a working file

Migration projects are perfect for this. Pick a vendor migration (PlantPAx → ABB, RSLogix5 → Logix5000, legacy InTouch → Ignition):

- **Migration-Planner** writes `MIGRATION.md` with a per-routine checklist.
- **Migration-Executor** subscribes to `MIGRATION.md` — picks the next unchecked item, does the conversion (e.g., rewrites a routine into ST), checks it off, commits.
- **Conversion-Verifier** reads both the source and target exports; flags semantic differences.
- You edit `MIGRATION.md` directly to redirect priorities, add manual notes, or override.

Works equally well for **commissioning punch lists**, **PHA finding closure**, **alarm rationalization workflows** — anything where a checklist is the actual state machine.

### 5. The "Reflective journal" — Reply chains for long-running engineering work

Engineering documents that accumulate context over weeks: HAZOP studies, alarm rationalization, SIF analyses, commissioning logs.

- A daily-schedule job: "review yesterday's PLC commits, append a paragraph to `commissioning_log.md` describing what changed and what's left to test."
- Each day, Reply on yesterday's task — the agent has the *whole* multi-week context. By month-end you have a coherent commissioning narrative, not 30 disconnected stub entries.
- Same shape for **HAZOP node-by-node walkthroughs** (one node per Reply), **SAT/FAT test execution logs**, **bug bash debriefs after a startup**.

### 6. The Governor as a config-tightening loop

Industrial controls projects accumulate *jobs* the same way they accumulate tags — sloppily. The Governor watches the watchers.

- After 10 task completions, it observes things like "the alarm-rationalization job exhausts 40% of the time on commits to historian queries — that's outside its scope; recommend tightening subscriptions to `alarms/**` only."
- You approve, it auto-applies. The harness gets sharper as you use it without you babysitting it.
- Particularly valuable on multi-site rollouts where the same job templates run across plants — the Governor surfaces drift between sites.

### 7. The "External-system bridge" — historian / OPC / MES into git

This is where industrial controls gets weird and good. The historian, the OPC server, the MES — they're all sitting outside the engineering files. MCP servers pull them into the trigger graph.

- **Historian MCP + schedule**: every Monday at 6am, query the last week of alarms, write `alarms/2026-04-29.md` with top-20 nuisance alarms (>10 occurrences/day, ISA-18.2 standard). Commit.
- That commit is now a trigger source for *other* jobs — a **Rationalization-Drafter** subscribes on `alarms/*.md`, proposes priority/setpoint/deadband changes for top offenders.
- **OPC-UA MCP + manual dispatch**: "snapshot live values for tags in this loop, generate a startup baseline doc."
- **MES MCP + dependency**: production-order changes trigger a recipe-validator agent that checks the active S88 recipe against the order spec.
- **CMMS MCP**: pull this week's work orders into a maintenance-impact briefing for the controls team.

You've turned external state (alarms, production data, work orders) into commits — and once it's a commit, every other job in mAistro can react to it.

### 8. "Cheap drafter, expensive reviewer" — model selection for industrial work

- **Tag-Triager** runs Haiku — reads new tag CSV rows, classifies (analog, discrete, calc'd, alarm), edits in suggested data type and engineering units. Cheap, fast, runs on every IO list change.
- **Logic-Implementer** runs Opus — picks up well-prepped, classified tags and generates corresponding PLC instructions/AOI calls.
- The two-stage queue does the gating: Triager works on Pending, Implementer pulls from Queued only after a human transfers tasks. You're not paying Opus to look at malformed CSV rows.

This applies broadly: **doc-parser** (cheap) extracts info from vendor manuals, **integrator** (expensive) places that info into your controls project.

### 9. Approval gates as the actual blast-radius firewall

In industrial controls, the consequence dimension is non-negotiable. Use `require_approval` to enforce that.

- **PLC-Online-Push** job: tools include a hypothetical OPC-write MCP. `require_approval: true`, narrow `allowed_dispatch_targets: []` (no agent can call it). It only runs when a human explicitly approves a specific task. Belt and suspenders.
- **Recipe-Activator**: same shape. Pushes a new batch recipe to active. Always gated.
- **PHA-finding-closer**: marking a HAZOP finding closed has compliance implications. Gated.
- Conversely, low-blast-radius jobs (tag-naming audit, doc generation, narrative drafting) run free — coalescing keeps the queue sane.

This is mAistro's most underrated feature for regulated environments. The flag is per-job, so a project can have a mix of fully autonomous work and white-knuckle gated work without compromise.

### 10. Brownfield discovery — manual + Reply for legacy decode

Walking into a 20-year-old facility with sparse documentation is the most common industrial controls situation.

- Manual dispatch: "read every `.L5X` under `legacy_export/`, build `discovered/equipment_map.md` cataloging every PID loop, motor starter, and interlock. Don't guess — flag uncertain items as TODO."
- Reply on the resulting task: "now extract the alarm logic specifically — which tags drive which alarms, what are the priorities?"
- Reply again: "now compare this against the existing `docs/Functional_Spec.docx` and tell me what's undocumented."

A narrow `allowed_tools` set (Read, Grep, Glob only) makes this safer — the agent is a forensic investigator, not an editor. You get an interview transcript with the legacy code.

### 11. The "Site-visit-prep" pattern — manual + dependency for handoff packages

Specific to consulting / integrator work: every site visit needs a briefing pack.

- **Manual** trigger on a "Prep-Visit" job, with a parameter for site name.
- It dispatches (via `dispatch_task`) a half-dozen specialist jobs in parallel: pull recent commits, summarize open punch list items, generate a tag-change diff since last visit, compile alarm trends from the historian, list outstanding HAZOP findings.
- All complete → a **Synthesizer** assembles them into a single PDF/markdown briefing.
- Coalescing keeps repeat preps efficient — running it Monday and again Tuesday absorbs the duplicate parallel dispatches.

Same shape for **FAT prep**, **SAT prep**, **commissioning daily standup**, **end-of-startup turnover packages**.

### Why this fits

In industrial controls, **the artifacts already exist** — you have tag DBs, narratives, P&IDs, alarm lists, work orders, historian data. The problem isn't generating them; it's keeping them coherent and surfacing the drift. mAistro's `subscriptions × triggers × multi-agent` composition is unusually well-suited because the cost of running an agent against a stale artifact is low, the value of catching drift early is high, and the audit trail (every task, every commit, every event sourced) is exactly what regulated environments need anyway.

---

## Where to Look Next

- **`DESIGN.md`** — the full requirements and rationale.
- **`STRATEGY.md`** — what's currently in flight.
- **`architecture/`** — deep dives into specific subsystems (storage, task lifecycle, streaming, dispatch engine, prompt assembly, tool mediation, Governor, etc.).
- **`CLAUDE.md`** — orientation for agents (including Claude Code) working *on* mAistro itself.

mAistro is opinionated about its primitives — jobs, tasks, the two-stage queue, event-sourced lifecycle, three-dimensional tool control — and unopinionated about what you do with them. Go build the harness you want.
