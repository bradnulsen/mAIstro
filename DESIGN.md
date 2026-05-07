# Design

## What mAistro Is

mAistro is a development engine where LLM-powered agents coordinate through git. The user defines jobs — configurable units of autonomous work — and the platform dispatches them as tasks, streams their output, and records what they did. The app itself is the agent; jobs define what work exists, tasks are specific executions of that work.

The user points mAistro at a project directory. From that point, the platform owns the operational loop: triggering tasks, building prompts, invoking the LLM, and letting the LLM commit its own changes. The user's role shifts from writing code to defining what work should happen, when, and under what constraints.

### Naming

Two concepts, precisely defined:

- **Job** — a named, persistent configuration of autonomous work. The user creates, edits, and deletes jobs. Jobs define *what* work exists: instructions, model, subscriptions, dependencies, schedule. Jobs are the nouns of the system.
- **Task** — a scoped, contextualized execution of a job. When a job is dispatched (by any trigger), it produces a task in the dispatch queue. Tasks have lifecycle (pending, queued, active, completed, failed), trigger context, and linked output. Tasks are the verbs of the system.

A job is a template. A task is an instance. One job produces many tasks over time.

---

## Requirements

### Distribution

mAistro ships as an installable end-user application, not as a developer setup. The deployment shape is single-user, single-tenant, single-active-project, running locally on the operator's machine. The audience extends beyond developers to non-developer subject-matter experts coordinating with autonomous agents.

- **One-click installation.** The platform installs via a standard installer for the host operating system. The installer drops the application, registers a launch entry, and sets up per-user data storage in the operating system's conventional location for application data. The user does not run package managers, edit configuration files, or operate from a terminal to get the platform running.
- **Per-user data separation.** Application-level state (the recent-projects list, cross-project job templates) lives in the user's standard application data directory, distinct from the install location. Project-level state (job configuration, task records, worktrees, output sessions) stays inside each project's directory. The installation itself is read-only; user data is writable and never inside the install location.
- **Claude CLI dependency.** The Claude CLI is a runtime requirement that the installer cannot embed — it requires the user's own authentication. On startup, the platform detects whether the CLI is present and authenticated. If it is missing or unauthenticated, the platform surfaces a setup screen explaining what is needed. A CLI status indicator in the application header reflects the current state — present and authenticated, missing, or unauthenticated — alongside MCP server health.
- **Single-instance behavior.** Launching the application a second time while it is already running does not start a second instance. The second launch detects the running instance and brings its surface to focus. Concurrent instances are not a supported deployment shape — they would compete for project locks and produce undefined behavior.
- **Localhost-only.** The platform binds only to the local loopback interface. There is no remote access, no team sharing, no hosted-service mode. Multi-user, multi-tenant, and remote-access deployments are out of scope.

### Projects

- The user opens a project directory. The platform initializes it for use: ensures a git repository exists, installs the post-commit hook, manages gitignore entries, creates the `.maistro/` directory (gitignored) with an operational database inside it.
- Opening a project makes it the active context. All dispatch, job, and Governor operations target the active project.
- Switching projects closes the current context and opens a new one. A project switch is blocked while any task is active.
- The platform maintains a recent-projects list in a separate app-level store, enabling quick project switching.

### Jobs

- A job is a named, configurable unit of work. It has a short description, detailed instructions (the prompt body), a model, and behavioral properties.
- Job identity is derived from the name (slugified). The name is the human-facing label; the slug is the system-facing key. Once created, the slug is immutable — renaming a job does not change its identity.
- Each job has a **color** — a visual identifier that distinguishes it across every surface where jobs appear. Color is assigned randomly on creation from a curated palette of distinguishable hues. The user can change the color at any time via a simple color picker (a hue wheel or equivalent minimal control). The color is cosmetic metadata — it carries no behavioral semantics. It exists purely to let the user visually parse job-related elements at a glance: queue items, task cards, status indicators, and anywhere else the job's identity appears.
- Job deletion cascades: removing a job removes its properties, dispatch history, and task output sessions.

### Job Properties

- Properties follow an entity-attribute-value pattern: a registry of property definitions (with types and defaults) and per-job overrides.
- Property types: `string`, `json`, `integer`, `boolean`. The platform casts stored strings to the declared type on read.
- Core properties: `description`, `instructions`, `model`, `subscriptions`, `depends_on`, `schedule`, `timeout`, `max_turns`, `require_approval`, `coalesce_tasks`, `allow_learning_self_modification`, `allowed_tools`, `allowed_internal_tools`, `allowed_dispatch_targets`, `mcp_servers`, `sort_order`, `color`.

### Dispatch

- Every dispatch — regardless of trigger — creates a task in the dispatch queue before execution. The queue is the single entry point to the execution engine.
- The background worker pulls from the queue and processes tasks sequentially (one at a time). The worker only pulls tasks in the **queued** state — pending tasks are invisible to the worker.
- **Two-stage queue**: tasks progress through two pre-execution states before the worker picks them up:
  - **Pending** — the staging area. Newly created tasks land here by default. The user reviews, coalesces, and curates pending tasks before promoting them to queued. Pending tasks are *not* eligible for execution.
  - **Queued** — the execution runway. Tasks here are committed to run. The worker pulls the highest-priority queued task when ready. The user reorders queued tasks to control execution sequence.
- **Auto-queueing** — a global setting that controls the routing of newly created tasks. When enabled, new tasks skip pending and go directly to the queued state. When disabled, new tasks always enter pending. This replaces the former auto-dispatch concept. The distinction: auto-queueing controls *where tasks land on creation*, not whether the worker runs. The worker always runs — it simply has nothing to do when no queued tasks exist.
- The user can override auto-queueing for any individual task by dragging it back from queued to pending. The routing decision happens only at initial trigger time — once a task exists, the user has full manual control over its state.
- Each task records its full lifecycle: creation, queueing, start, and completion timestamps plus a terminal status. A task that has started but not completed is "active."
- Each task captures **execution metadata** when the agent finishes: stop reason (why the agent stopped — natural completion, max turns reached, or error), turns consumed, duration, and cost. This metadata is surfaced to the user alongside the task's outcome. It is the difference between "the agent finished" and "the agent finished in 3 turns at $0.02" — the latter lets the operator assess efficiency, detect anomalies, and tune job configuration.
- On startup, the worker sweeps any tasks that were active when the process died and marks them as interrupted.

#### Terminal States

A task reaches a terminal state when execution ends (or is prevented). Seven distinct terminal states exist:

- **Completed** — the agent finished its work naturally. This is the success state. Only completed tasks trigger downstream dependencies.
- **Exhausted** — the agent consumed all available turns without finishing. The platform's per-task turn limit was reached and the CLI stopped the agent. This is a resource-limit stop, not a natural stop — the agent was cut off, not done. Partial commits may exist. Exhausted tasks do not trigger downstream dependencies. The user response is to investigate (what was the agent trying to do?), potentially increase the turn limit, and resume or re-dispatch.
- **Failed** — the task started but did not reach completion. Either the agent encountered an unrecoverable error during execution, or the agent finished but the platform could not integrate its work into the project's main branch (conflicts, divergence). The error context carries the diagnostic message and distinguishes the two cases.
- **Timed out** — the watchdog terminated the agent after exceeding the configured timeout. Partial commits may exist between `start_commit` and `result_commit`.
- **Cancelled** — the user explicitly stopped the task while it was running.
- **Interrupted** — the platform process died while the task was active. Detected and marked on startup by the stale sweep.
- **Rejected** — the user rejected a task awaiting approval. The task never executed.

The distinction matters: **completed is success; everything else is a form of non-success.** Each non-success state implies a different user response — retry a failure, resume a timeout, increase turns and re-dispatch after exhaustion, investigate after an interruption — so the UI must make the distinction immediately visible without requiring the user to inspect error details.

The difference between **exhausted** and **timed out** is the resource that ran out: turns vs. time. Both produce partial work. Both are non-success. But they have different operational responses: exhaustion suggests the task needs more turns (or the instructions are too open-ended), while timeout suggests the task needs more time (or the agent is stuck). The distinction prevents misdiagnosis.

### Task Workspace Isolation

Each task executes in its own filesystem workspace — a git worktree dedicated to that task. The platform creates the workspace when the task activates, the agent operates exclusively within it, and the platform reconciles its state into the project on terminal transition.

The reason: tasks are atomic units of work. Without isolation, an agent's in-progress edits live in the shared project directory, where they can collide with concurrent human edits, leak into the next task's dispatch context, or strand work in the working tree if the task ends without committing. Per-task workspaces eliminate this coupling — the agent never modifies the project's main checkout directly, and orphaned changes vanish with the workspace.

- **Workspace creation** — when a task transitions to `active`, the platform creates a worktree branched from the project's current main HEAD. The agent's working directory for the entire dispatch is this worktree. The user's main checkout is structurally untouchable by the agent.
- **Agent operations within the workspace** — every read, edit, and commit the agent makes targets the worktree. Tool calls that operate on project content resolve against the workspace path, not the user's main checkout. The project's main working tree is unaffected throughout.
- **Reconciliation on terminal transition** — depends on the terminal state:
  - **Completed** — the platform integrates the task's commits into the project's main branch. `result_commit` is the post-integration HEAD on main. The worktree is then removed. If integration cannot proceed cleanly (conflicts with main, divergence the platform cannot resolve automatically), the task does not reach completed — it transitions to `failed` with the conflict surfaced as the error context, and the workspace is preserved like other non-success states. Completion is contingent on the work being in main, not just on the agent finishing.
  - **Exhausted, failed, timed out, cancelled, interrupted** — the worktree and task branch are preserved. The detail drawer surfaces the branch name, commit count, and file change summary, with affordances to discard the workspace or merge it manually if the partial work is salvageable. The project's main branch is untouched.
  - **Rejected** — the task never executed; no workspace was created.
- **Concurrent human edits are unaffected** — the user's main checkout is a separate filesystem location. The agent never sees or modifies it. Watch triggers fire on commits to the project's main branch (whether from the user, a successful task's integration, or external pushes); commits on task branches do not trigger watch.
- **Startup recovery** — on platform startup, any worktree whose task is in a terminal state and was not cleanly handled before shutdown is swept and recorded for review, never blindly deleted. This complements the stale-task sweep for `active` tasks.

The workspace is the agent's sandbox; the main checkout is the user's. Reconciliation between them happens through git, on the platform's terms, at well-defined transitions.

### Task Ordering

- The queued column maintains a sort order that determines execution priority. The worker pulls the highest-priority queued task (lowest order position). The user controls execution order by reordering queued tasks via drag-and-drop.
- The pending column does not maintain a meaningful sort order — it is a staging area for curation, not a priority queue. Tasks in pending are displayed by creation time.
- Newly created tasks are appended to the end of their target column (pending by default, or queued if auto-queueing is enabled).
- Tasks transferred from pending to queued are appended to the end of the queued column. The user then reorders them to set priority.

### Triggers

Five trigger types cause tasks to be enqueued:

- **Manual** — the user explicitly dispatches a job. Always creates a new task. Never coalesces. Bypasses approval gates.
- **Commit (watch)** — a git post-commit hook notifies the platform. Jobs whose subscription glob patterns match changed files are enqueued as tasks. Coalesces with other pending commit-triggered tasks for the same job.
- **Schedule** — cron expressions evaluated by a background scheduler. Enqueues a task when the expression fires. Always coalesces globally (repeated fires while a task is pending produce one run, not many). The first evaluation after a schedule is set establishes a baseline without firing — a newly configured schedule does not immediately dispatch.
- **Dependency** — when a task completes successfully, jobs declaring its job as an upstream dependency are enqueued. Coalesces with other pending dependency-triggered tasks for the same job. Only successful completion triggers dependents — exhausted, failed, timed-out, cancelled, interrupted, and rejected tasks do not.
- **Agent** — another agent's task programmatically dispatches a job via the `dispatch_task` MCP tool, with a message explaining why. The trigger context carries the dispatching task's identity and message. Coalesces with other pending agent-triggered tasks for the same job. Agent dispatch is subject to job-level dispatch control — a job property governs which other jobs an agent can dispatch.

Three continuation triggers operate on existing tasks:

- **Resume** — continues a previous task using the CLI's session resume capability. Creates a new task and coalesces the original under it (inverted — the new task is the root, the original becomes subordinate). The CLI resumes the original session.
- **Reply** — creates a follow-up task targeting a resolved task with additional user context. Like resume, the original task is coalesced under the new one. Unlike resume, reply starts a fresh session — it does not resume the original CLI session.
- **Retry** — re-enqueues a failed or timed-out task. Resurrects the original record in-place (resets lifecycle fields and created_at to maintain fair queue ordering).

Resume and reply use **inverted coalescing**: the new task becomes the root and the original becomes a subordinate. In chains (A → reply B → reply C), the latest task is always the root and all predecessors are flat subordinates. This preserves full provenance while keeping the newest intent as the dispatchable unit.

### Coalescing

Coalescing prevents redundant pre-execution tasks. Two mechanisms exist: **automatic coalescing** (at enqueue time, driven by trigger rules) and **manual coalescing** (user-initiated, from the queue).

#### Automatic Coalescing

- When a new trigger would create a task but a compatible pre-execution task (pending or queued) already exists, the trigger is appended to the existing task's trigger list instead of creating a new record. Coalescing checks both columns — a new trigger coalesces into whichever matching task exists, regardless of whether it is pending or queued.
- `commit`, `dependency`, and `agent` triggers coalesce with other pre-execution tasks of the same trigger type for the same job.
- `schedule` triggers coalesce globally (any pre-execution task for the same job absorbs the new trigger).
- The `coalesce_tasks` job property enables global coalescing for all trigger types — the job will never have more than one pre-execution task.
- `manual` and `retry` never coalesce — each represents distinct explicit intent.
- `resume` and `reply` do not participate in automatic coalescing (they always create a new task), but they establish an inverted coalesce relationship with the original task — the original becomes subordinate to the new task.

#### Manual Queue Composition

The user can merge and split tasks in the pending column directly from the Dispatch view. This gives explicit control over the grouping that automatic coalescing performs implicitly. Composition is a pending-column operation — it belongs to the curation stage, not the execution runway.

**Merge** — the user drags a pending task directly onto another pending task for the same job. The two tasks combine into a single task. All trigger entries from both tasks are collected into the surviving task's `triggers` array. The older task (by `created_at`) survives; the dragged task is removed from the queue. The surviving task retains its position.

- Only pending tasks can be merged (not queued, not started, not completed, not pending-approval). Merge is a curation operation that belongs to the staging area.
- Only tasks belonging to the same job can be merged. Merging across jobs would produce a task with ambiguous identity — one task cannot represent two jobs. The UI enforces this by suppressing the merge affordance when the dragged task and the drop target belong to different jobs.
- The merge operation is the manual equivalent of what automatic coalescing does at enqueue time: multiple reasons to run become one run that addresses all of them.
- Trigger context is preserved verbatim. Each trigger entry retains the context string it was created with — merge does not rewrite history.

**Drag Interaction Model** — drag operations are specialized by column, reflecting the distinct purpose of each stage:

- **Pending column (coalescing)** — the pending column is for grouping and curating work before it runs. Drag operations in pending support **merge** (drop onto a same-job task to coalesce) and **transfer** (drop into the queued column to promote). Reordering within pending is not meaningful — pending is a staging area, not a priority queue. The order tasks leave pending is determined by when the user transfers them to queued.
- **Queued column (sorting)** — the queued column is the execution runway where order determines priority. Drag operations in queued support **reorder** (drop between tasks to change execution priority) and **transfer** (drop into the pending column to demote). Merge is not available in queued — coalescing decisions are made during staging, not after commitment to run.
- **Transfer** (between columns) — drop into the other column. Moves the task from pending to queued or from queued to pending. The transferred task is appended to the end of the target column. Visual feedback: the target column highlights as a drop zone.

**Each column has one primary drag operation plus transfer.** Pending owns coalescing (merge); queued owns sorting (reorder). This separation reflects the workflow: curate and group work in pending, then commit and prioritize in queued. Cross-column drag is exclusively a transfer — it changes state without merging or reordering within the target column.

**Split** — the user takes a pending task that has multiple trigger entries and breaks it into individual tasks, one per trigger. The original task keeps its first trigger entry and position; new tasks are created for each remaining trigger and appended to the end of the pending column.

- Only pending tasks with more than one trigger entry can be split. Split is a coalescing operation and coalescing belongs to the pending column.
- Each resulting task is an independent queue entry with its own lifecycle.
- Split reverses a previous merge or automatic coalescing. The user can inspect the accumulated triggers on a task and decide they should run separately.
- New tasks created by split inherit the job's current approval gate setting. If `require_approval` is enabled, split-off tasks enter pending-approval state.

**Why same-job only**: a task's identity is bound to exactly one job. Prompt assembly, subscriptions, tool configuration, and commit authorship all derive from that single job. Merging tasks across jobs would require either a compound identity (one task, two jobs — breaks prompt assembly, tool surfaces, authorship) or a synthetic super-job (implicit, unmanageable). Neither is coherent. The constraint preserves the foundational invariant: one task, one job, one execution context.

### Subscriptions

Subscriptions are glob patterns (supporting `**` recursion) that define a job's **relevant files**. They serve two purposes:

- **Context injection** — every task resolves the job's subscription patterns and lists matching files in the prompt as "files relevant to your task." The job's instructions define the relationship — ownership, input, reference, or anything else.
- **Watch triggering** — when a commit changes files matching a job's subscription patterns, the job is enqueued as a task. The trigger context carries the commit summary; the full set of subscribed files (changed or not) is always present in the prompt, giving the agent enough information to infer what happened.

A job with non-empty subscriptions is watch-active. There is no separate toggle.

A job can use subscriptions purely for context (by gating automatic triggers with `require_approval`) or purely for triggering (by not referencing the file list in its instructions).

### Job Learnings

A job's instructional surface has two complementary forms: the prose **description** (cohesive narrative — mission, scope, voice) and **learnings** (discrete, individually-addressable rules, examples, and constraints). Both flow into every dispatch's prompt; both shape what the agent does. The distinction is structural — prose reads worse when fragmented, and learnings benefit from individual toggle, reorder, source provenance, and agent write-back.

A learning is a single piece of guidance attached to a job. Each learning has:

- **Body** — the guidance itself, free-form text, typically a sentence to a paragraph.
- **Enabled flag** — disabled learnings are stored but excluded from prompt assembly. Disabled learnings cost nothing at dispatch time. Useful for muting stale guidance without losing the artifact, or for A/B-style behavior tweaks.
- **Position** — ordering within the job's learning list, set by drag-reorder.
- **Source** — `human` (operator-authored) or `agent` (agent-authored). Provenance is preserved for the lifetime of the learning.
- **Created and updated timestamps** — for visibility into recency.

Learnings are append-only in spirit but mutable: edits update the timestamp, deletions are hard deletes. There is no soft-delete state; the disabled flag covers "I want it gone but might want it back."

#### Prompt Effect

Enabled learnings are concatenated into the dispatch prompt as a `## Learnings` section appended after the description. Disabled learnings are omitted entirely. A job with no enabled learnings produces a prompt byte-identical to a job that has the feature unused — the section header is not emitted unless there is content. New jobs ship with an empty learnings list; existing jobs gain the surface without any prompt change. Learnings accumulate when the operator (or, where permitted, the agent) has a discrete rule worth pinning.

#### Agent Self-Modification

The per-job property `allow_learning_self_modification` (boolean, default off) controls whether the agent can write to its own learnings. When off, the agent has read-only access — it can introspect "what guidance shapes my behavior?" but cannot change anything. When on, the agent can add, update, and delete learnings, but only those it itself authored.

The write surface is **asymmetric**. An agent can manage its own (`source='agent'`) learnings but cannot edit or delete operator-authored (`source='human'`) ones. Operator authority over operator-authored guidance is structural, not policy. The reasoning: agents may discover and pin self-applicable rules from their own work; they should not retract or rewrite the operator's directives.

The platform's internal MCP tool surface includes a read tool (`list_learnings`, always available) and write tools (`add_learning`, `update_learning`, `delete_learning`, gated by the property). All four are audit-logged like every other MCP tool call.

#### Operator UI

Job configuration shows the learnings list below the description. Each row exposes the body (truncated, click to expand and edit), an enabled toggle, a source badge (`human` / `agent`), a drag handle for reorder, and a delete button. An "Add learning" affordance creates a new row inline. The operator can do anything in the UI — add, edit, delete, toggle, reorder — regardless of source. The asymmetric restriction applies only to agents.

### Dependencies

- Jobs declare upstream dependencies via `depends_on` (a list of job IDs). When an upstream job's task completes successfully, dependent jobs are auto-enqueued.
- Circular dependency chains are permitted. Coalescing and sequential execution absorb redundant triggers — a job that already has a pending task absorbs the new trigger rather than creating an infinite queue.
- Dependency context includes the upstream job name, task ID, and commit range.

### Approval Gates

- Jobs with `require_approval` enabled produce tasks with `approval='pending'` status (except manual dispatches, which bypass the gate).
- The worker skips pending-approval tasks. A user must explicitly approve or reject.
- Rejection marks the task as completed with an error of "rejected."

### Timeout Enforcement

- Each job has a configurable timeout (in seconds, default 900).
- A watchdog cancels the CLI subprocess after the timeout elapses. The task is marked as timed out. Timed-out tasks do not trigger dependents.
- Cancellation is graceful: the platform signals termination, waits a grace period, then forces termination if the process has not exited.

### Turn Limit

- Each job has a configurable maximum number of agent turns (`max_turns`). A turn is one cycle of the agent receiving context, reasoning, and producing output (text or tool calls).
- When the agent reaches the turn limit, the CLI stops the session. The platform marks the task as exhausted. Exhausted tasks do not trigger downstream dependencies.
- The turn limit is a safety bound, not a target. Most tasks finish well within the limit. When a task hits the limit, it typically means the instructions are too broad, the agent is stuck in a loop, or the work genuinely requires more interaction than anticipated.
- The user sees turns consumed alongside the outcome — "completed in 5/50 turns" vs. "exhausted at 50/50 turns" — so the limit's effect is always visible, not just when it triggers.

### Prompt Assembly

- The platform builds two prompts per task: a **system prompt** (execution mode, documentation principles, git workflow, working directory) and a **user prompt** (job identity, instructions, enabled learnings, invocation context, job registry, subscribed files, action directive).
- The system prompt establishes headless autonomous behavior: no questions, no clarification requests, commit-based workflow.
- The user prompt layers job-specific instructions with runtime context: why this task was triggered, what other jobs exist, which files are subscribed, and the job's enabled learnings (when any exist).
- The job registry — a manifest of all jobs with their names, descriptions, and subscriptions — is included in every task prompt. Agents know what other agents exist.
- Agents commit their own changes. The platform does not auto-commit.

### Task Output Sessions

- Every task creates a linked chat session for durable output storage. The session holds the agent's streamed text as chat messages and raw events as an audit trail.
- Chat sessions persist across application restarts. Sessions can be resumed through the CLI's session continuation capability.

### Task Session Interrogation

Completed and other terminal tasks produce outcome summaries and diffs, but the user needs to ask follow-up questions: "Why did you change this file?" "What alternatives did you consider?" "What did you see that you didn't act on?" This requires conversational access to the agent's session — the same context, the same reasoning chain.

- **Session resume from Resolved** — resolved tasks in the Dispatch view's Resolved column provide a "Chat" action that opens the task's session in a conversational interface. The agent resumes with its full prior context intact via the CLI's `--resume` capability. The resumed session runs against the workspace the task produced — the preserved task worktree for non-success terminals, or a fresh read-only workspace at the integrated `result_commit` for completed tasks. The agent inspects exactly the state it produced.
- **Read-only tool restriction** — the resumed session is dispatched with write tools removed. No `Edit`, no `Write`, no `git_commit`, no branch operations, no inter-agent dispatch. The agent can read files, search code, and reason about its prior work, but cannot modify the project. Interrogation does not produce side effects.
- **Work flows through tasks** — if the conversation reveals work that should be done, the user dispatches a new task. The interrogation surface offers a "convert to task" affordance that pre-populates a task draft from the conversation context. The read-only session is an investigation tool, not an execution channel. This preserves the queue-first invariant: all modifications flow through the task queue where they are visible, auditable, and controllable.
- **Optional thread linking** — insights surfaced during interrogation can be promoted to a Governor thread. The user marks a span of the conversation and posts it as the opening message of a new thread, closing the loop between micro-investigation (this specific task) and macro-oversight (the Governor's pattern view).

### Governor

The Governor is an internal autonomous agent that monitors the health and effectiveness of the mAistro system within a project. It is entirely meta-scoped — it observes how jobs and tasks are performing, identifies friction and opportunities for improvement, and corresponds with the operator about them. The Governor does not touch project code, does not participate in task dispatch, and does not modify a job's prose description directly.

The Governor's relationship with the operator is a **two-way conversation organized into threads**. A thread is a persistent meta-management discussion about a single concern — a friction the Governor noticed, a question the operator wants answered, a configuration change one of them is proposing. The operator and the Governor exchange messages within a thread; either may propose a change; either may push back or refine. This is not a chat agent — every Governor message is the output of a discrete, fresh invocation. Continuity comes from the thread record, not from a long-running session.

This replaces an earlier one-way model in which the Governor produced "findings" that the operator could only approve or decline. One-way findings could not absorb operator pushback, could not be refined, could not be questioned. The thread shape preserves the meta-awareness the findings model had while opening the channel both directions.

#### Threads and Messages

A **thread** is the persistent unit of Governor↔Operator exchange. Each thread has a title, an opener (Governor or operator), an ordered append-only list of messages, and a status (open or closed). A **message** is one post within a thread, authored by either the Governor or the operator. Messages are append-only — no edits, no deletions.

A Governor message may carry a structured **proposal** — a precise, machine-readable action the Governor wants the system to take (modify job properties, create a new job, change queue settings). The proposal renders alongside the message as a structured card, but it carries no buttons. The operator's response is the thread's normal compose box. The operator writes back "yes, do it" or "I disagree because..." or "what about doing X instead?" The proposal's fate is decided by the next Governor invocation, which reads the conversation, interprets the operator's intent, and acts (or refuses to act) accordingly.

Approvals are not a separate UI surface. There are no Approve/Decline buttons. Assent is prose. Refusal is prose. Refinement is prose. The two-way conversation absorbs everything the one-way feed could not.

**Closed threads are muted.** Closing a thread is operator-only — the Governor cannot close threads, structurally. A closed thread is invisible to the Governor: excluded from every invocation's context, even its title. The thread remains visible to the operator for reading, and the operator can reopen it at any time. The Governor cannot reopen a closed thread; closure is the operator's mute control over the Governor's awareness.

#### Invocations

Every Governor activity is one of two invocation types. Each is a fresh, isolated invocation with its own context packet. None resume a session. At most one Governor invocation runs at a time across the platform.

- **Survey** — the autonomous trigger. Runs after every 10 executed terminal tasks (counted project-wide, not per-job). "Executed" means the task actually consumed agent turns: `completed`, `exhausted`, `failed`, and `timed_out` all count. `cancelled` and `rejected` are excluded because they reflect operator intent, not system behavior, and contain no signal about agent or platform health. The counter resets after each survey. A survey reviews project state and the list of currently open threads, then either posts updates in existing open threads or opens new threads for new concerns. Surveys are read-only — the Governor cannot modify the project, jobs, or queue settings during a survey. A survey may produce zero operations; silent surveys are allowed.

- **Reply** — triggered by any operator action inside a thread (creating the thread, posting in it). The Governor reads the thread's full history along with the current project state and produces exactly one response message in that thread. Reply invocations are uniformly write-capable: the Governor's discretion, informed by the conversation, decides whether to invoke a write tool. If the conversation contains an unexecuted proposal that the operator has clearly assented to, the Governor applies the change and posts a result message describing what was done. If intent is unclear, ambivalent, or contradicted, the Governor does not write — it posts a clarifying response instead. Before applying a stale proposal, the Governor verifies the current state of the target — if the world has moved on since the proposal was made (the property already changed, the job no longer exists), it does not blindly apply.

There is no manual trigger. The operator initiates Governor conversation by creating a thread, not by pinging the analysis engine. The Governor's cadence, scope, model, and personality are not user-configurable.

Counting failures in the survey trigger is deliberate. A job that consistently fails or exhausts its turn limit is exactly what the Governor should be reviewing. Counting only successes would mean a broken job that never completes also never triggers oversight — the opposite of what proactive review requires.

#### Reply Coalescing

Reply invocations are per-thread coalesced. If a reply for thread X is already queued or in flight and the operator posts again in thread X, the new message merges into that pending invocation instead of enqueueing a second one. The Governor produces one response that addresses everything; the latest operator message is operative when interpreting intent. The operator never sees queue state — only that a Governor message eventually appears in the thread.

#### What the Governor Analyzes

The Governor's concern is the effectiveness of the mAistro system, not the quality of the project's code:

- **Job effectiveness** — success rates, recurring failures, turn-limit exhaustions, timeouts. Whether descriptions or learnings are too vague or too broad for reliable execution.
- **Scope drift** — whether subscription patterns and instructions have grown stale relative to actual project activity.
- **Configuration friction** — turn limits, timeouts, subscription patterns, and approval gates calibrated against observed behavior.
- **Implied operator intent** — patterns in manual dispatch, approval decisions, reply/resume frequency, and prior thread responses that suggest unmet needs or misconfiguration.
- **Inter-agent coordination** — health of agent dispatch chains, missing dependencies, redundant overlapping jobs.

The Governor may propose a learning as part of a thread proposal — a discrete rule it believes belongs on a specific job. The operator assents in prose; the next reply invocation, if it interprets assent, writes the learning via the same write surface that reaches every other configuration change.

#### The Governor's Tool Surface

The Governor connects to a dedicated MCP tool surface scoped to meta-operations.

**Read tools** (always available):
- `list_jobs` — all jobs with their full configuration
- `get_recent_tasks` — recent terminal tasks with outcomes, metadata, trigger info, and error context
- `get_git_log` — recent commit history with authorship attribution
- `get_job_health` — per-job aggregates: success rate, failure count, timeout count, exhaustion count, average turns, average cost
- `list_open_threads` — thin list of open threads (id, title, opener, last activity)
- `get_thread` — full message history of a specific thread

The Governor has no read access to closed threads. Closed threads are invisible at the tool surface, not just by convention.

**Write tools** (available only on reply invocations):
- `update_job_properties` — modify any job property
- `create_job` — create a new job with specified properties
- `update_queue_settings` — modify global queue settings

The write surface is **constructive only**. There is no `delete_job`, no `disable_job`, no `close_thread`. Removing or pausing a job is an operator-only action performed in the Jobs view. Closing a thread is an operator-only UI action. The Governor can flag a job as low-value or recommend that a thread be closed in prose, but it cannot itself remove, disable, or mute.

The Governor never sees write tools during a survey; it always has them on a reply (whether to use them is its judgment). This is structurally enforced, not policy.

#### What the Governor Is Not

- **Not a chat agent.** Each Governor invocation is discrete and isolated, with its own fresh context packet. The thread record provides continuity across invocations; no long-running session exists.
- **Not a notification system.** The Governor does not fire alerts for individual task events. It synthesizes patterns across multiple tasks. A single task failure is a queue event; a pattern of failures is a thread.
- **Not configurable.** The Governor's personality, analysis scope, trigger cadence, and model are not user-configurable. The Governor is a platform feature, not a user-defined agent.
- **Not a task dispatcher.** The Governor cannot create tasks or dispatch work. Its write scope is the meta layer — job configuration and queue settings. Project work flows through the normal task queue.
- **Not destructive.** The Governor can create and modify configuration but cannot delete jobs, disable jobs, or close threads. The destructive surface is operator-only.

### Streaming

- Task output streams to the frontend via Server-Sent Events (SSE). Event types: `text`, `thinking`, `tool_use`, `result`, `error`, `session_id`.
- **Thinking content** — when the agent produces reasoning or thinking blocks, these are streamed as `thinking` events. The user sees the agent's reasoning process alongside its actions. Thinking content is part of the agent's output — dropping it silently degrades the user's ability to understand what the agent is doing and why.
- Live subscribers receive events in real time. The stored session provides the same content for later retrieval.
- **Incremental persistence** — chat events are persisted incrementally during streaming, not accumulated in memory and flushed once at the end. A crash or failure mid-stream must not discard all events captured up to that point. The stored session is the authoritative record of what the agent did; its durability cannot depend on clean termination.

### Dispatch Outcomes

When a task completes, the platform must make the result understandable without requiring the user to read the full streamed output.

- **Outcome summary** — the platform derives a short summary from the commits produced during the task's execution window (between `start_commit` and `result_commit`). The summary captures what the agent actually did — commit messages and change statistics — not a restatement of the instructions. Displayed inline in the Dispatch view so users can scan completed tasks at a glance.
- **Execution metadata** — each resolved task displays its execution metadata: stop reason, turns consumed (relative to the limit), duration, and cost. This is visible alongside the outcome summary — the user sees not just what the agent did but how much it cost to do it. Turns consumed relative to the limit is especially important: "3/50 turns" is healthy; "50/50 turns (exhausted)" is a signal to investigate.
- **Outcome in dependency context** — when a completed task triggers downstream dependents, the outcome summary is included in the trigger context passed to the dependent task's prompt. This gives downstream agents concrete information about what their upstream actually produced, not just that it completed.

The outcome summary is derived, not authored. The platform computes it from git artifacts that already exist. The user does not write summaries; the agent does not produce them explicitly. The platform reads what happened and describes it.

### Git Integration

- The platform installs a post-commit hook in the project directory to notify the backend of new commits, enabling watch triggers. The hook is asynchronous and fails silently — hook failures do not affect git operations.
- The platform reads git state (log, diff, head hash, changed files). Only agents commit changes through MCP tools.
- Task diffs are tracked via `start_commit` and `result_commit` for before/after comparison.

### Platform-Mediated Tool Access

The platform hosts an internal MCP server that dispatched agents connect to. This server mediates agent operations — every tool call flows through platform code, making actions observable, auditable, and policy-governed.

- The server is context-aware: it reads the dispatching job's configuration and presents only relevant tools. Different jobs get different tool surfaces based on their properties and subscriptions.
- **Git operations as structured tools** — `git_commit`, `git_diff`, `git_log`, `git_status` with enforced conventions (commit authorship, message format). These replace unmediated shell-based git access.
- **Git branch operations** — `git_branch_create` (create a new branch from a specified base, with enforced naming conventions such as `<job-id>/<description>`), `git_branch_switch` (switch the working directory to a named branch, with the platform tracking which branch a task operates on for audit purposes), and `git_branch_merge` (merge a source branch into the current branch, surfacing merge conflicts as structured tool output rather than silent failures).
- **Read-only project context tools** — file listing, file reading, and job information retrieval, scoped by the job's subscriptions and configuration.
- **Inter-agent coordination tools** — `dispatch_task` (enqueue a task for another job with a message, creating an `agent` trigger attributed to the dispatching task) and `get_queue_status` (read-only view of queue state — what's pending, running, and backed up). These give agents situational awareness and imperative coordination beyond the declarative trigger system.
- **Learning self-management tools** — `list_learnings` (always available) lets the agent introspect the rules attached to its job; `add_learning`, `update_learning`, and `delete_learning` (gated by `allow_learning_self_modification`) let the agent pin discrete rules to its own job. Write access is asymmetric: the agent can manage learnings it authored, but cannot modify or delete operator-authored ones. This is the only platform-mediated path by which an agent edits its own instructions.
- **Tool invocation logging** — every MCP tool call is recorded as a structured event in the task session, creating an audit trail richer than NDJSON stream parsing. Branch operations are logged identically — the platform can reconstruct which branches a task created, switched to, and merged.
- Agents retain access to native CLI tools (including Bash) alongside MCP tools. MCP tools are structured alternatives, not an exclusive replacement.

### Tool Configuration

Each job configures what tools its agent can access:

- **Allowed Tools** (`allowed_tools`) — the CLI tool names the agent can use (e.g. `Read`, `Edit`, `Bash`, `Write`). When set, the agent is restricted to exactly these tools plus any tools from connected MCP servers. When empty, the agent gets the default tool set.
- **Allowed Internal Tools** (`allowed_internal_tools`) — selects which internal MCP tools the job's agent can access. When set, only the specified internal tools are presented. When empty, all internal tools are available (backward compatible). A read-only job might be restricted to `list_files`, `read_file`, `git_log`, `git_diff`. A writer job gets the full set including `git_commit` and branch operations.
- **MCP Servers** (`mcp_servers`) — selects which registered external MCP servers are available to this job's agent. Only servers listed here (and enabled globally) are connected during dispatch. The platform's internal MCP server is always connected.

Tool configuration is **opt-in only**. The user defines what a job *can* do — one concept, one property. There is no complementary "disallowed" property. The platform computes the inverse internally: tools not in the allowed set are removed from the agent's environment entirely. The agent never sees them, never reasons about them, never attempts to work around restrictions.

This is not a stylistic choice — it follows from headless execution. A headless agent cannot ask for permissions, cannot negotiate tool access, cannot be told "you're not allowed to use X" in a meaningful way. The only coherent model is to present exactly the tools the agent can use and nothing else. The restriction surface is the platform's responsibility, not the agent's.

The three tool configuration properties — `allowed_tools`, `allowed_internal_tools`, and `mcp_servers` — compose into the agent's complete tool surface at dispatch time. CLI tools are selected by `allowed_tools`, internal MCP tools are selected by `allowed_internal_tools`, and external MCP tools come from the servers selected by `mcp_servers`. Each dimension is independently configurable, and the default for each is "everything available."

### Tool Discoverability

The platform must make the available tool inventory visible and selectable. A user configuring a job's tools should never need to guess names, remember spelling, or consult external documentation.

Three tool sources exist, each requiring discovery:

- **Built-in CLI tools** — the default tools provided by the Claude CLI (e.g. `Read`, `Edit`, `Write`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `Agent`, `NotebookEdit`). The platform queries the CLI for its available tool list and presents them. These are the tools that `allowed_tools` selects from.
- **Internal MCP tools** — the tools hosted by the platform's own MCP server (`git_commit`, `git_diff`, `git_log`, `git_status`, `git_branch_create`, `git_branch_switch`, `git_branch_merge`, `list_files`, `read_file`, `list_tasks`, `dispatch_task`, `get_queue_status`, `list_learnings`, `add_learning`, `update_learning`, `delete_learning`). The platform knows these directly — it defines them. The internal server is always connected, but individual internal tools are subject to per-job selection via `allowed_internal_tools`. When no selection is made, all internal tools are available. The learning write tools (`add_learning`, `update_learning`, `delete_learning`) are additionally gated by the per-job `allow_learning_self_modification` property — a job that does not opt in does not see them regardless of `allowed_internal_tools`.
- **External MCP server tools** — tools provided by registered external servers. When a server is registered and enabled, the platform connects to it and discovers its tool list. These tools become visible in the per-job configuration surface alongside built-in tools.

The configuration surface for `allowed_tools` presents the full inventory of available tools as a selectable list — checkboxes, multi-select, or equivalent. The user selects from available options.

This is a discoverability requirement, not a UI prescription. The essential behavior: the user sees what is available and selects what they want.

### External MCP Servers

External MCP servers extend the tool surface available to agents beyond the platform's built-in and internal tools. They are the platform's extensibility mechanism — every third-party integration, every domain-specific tool, every custom capability flows through this surface. The platform manages their full lifecycle: registration, validation, connection, discovery, per-job assignment, and dispatch-time verification.

**Registration** — external servers are registered at the platform level (MCP Servers view). Each registration specifies a server name, the command to launch it, command arguments, and environment variables. A registered server can be enabled or disabled globally — disabled servers are not available to any job regardless of per-job configuration.

**Registration Validation** — the platform validates server configuration at registration time, not at dispatch time. The command must be a valid executable (exists on PATH or is a valid absolute path). Arguments must parse correctly as a structured list. Environment variables must be well-formed key-value pairs. Invalid registrations are rejected with specific error messages explaining what is wrong. The user fixes problems when they create them, not when a task fails minutes later with an opaque error.

**Environment Variables** — external MCP servers frequently require environment variables (API keys, configuration paths, service URLs). The registration surface provides explicit key-value environment variable management. Env vars are stored as part of the server configuration and passed to the server process at launch. This keeps server configuration self-contained within the platform — the operator does not need to set env vars outside the platform for servers to function.

**Connection and Discovery** — when an external server is registered and enabled, the platform can connect to it and discover its tool inventory. The discovered tools are what the user sees when configuring per-job server assignments. If a server cannot be reached or fails to report its tools, the platform surfaces this state clearly — the user knows which servers are healthy and which are not.

**Per-Job Assignment** — a job's `mcp_servers` property controls which registered external servers are connected during that job's task execution. The configuration surface presents registered servers as selectable options (not free-text). Only servers that are both registered and globally enabled appear as options. The platform's internal MCP server is always connected and is not subject to per-job selection. The per-job configuration surface shows each server's current health status (healthy, unreachable, disabled) so the user sees problems before dispatching.

**Pre-Dispatch Health Check** — before a task dispatches, the platform probes all external MCP servers assigned to the task's job. If any server is unreachable, the task fails immediately with a specific error naming the server and the failure reason (command not found, timeout, handshake failure, disabled). The operator sees exactly what broke and can fix it before retrying. This is a dispatch-time gate, not a background monitor — the check happens at the moment of execution.

**Error Attribution** — when an MCP server fails during task execution, the error is surfaced with the server name and failure mode, not as a generic CLI error. The task detail view shows which external MCP servers were included in the dispatch configuration, so when a task fails the operator can immediately correlate the failure with a specific server.

**Stale Reference Integrity** — when a server is deleted, the platform removes it from all jobs' `mcp_servers` lists. A job configured with a server that no longer exists never silently loses tools — the reference is cleaned up at the source. When a server is disabled, jobs referencing it see a visible warning that tools from this server will not be available at dispatch time. Disabled servers are never included in dispatch configuration.

**Tool Surface Composition** — during dispatch, the agent's available tools are the union of: (1) CLI tools selected via `allowed_tools` (or the full default set if empty), (2) internal MCP server tools (always present), and (3) tools from external MCP servers enabled for the job. The user can see this composed tool surface when configuring a job — what the agent will actually have access to.

### User Interface

The product presents a persistent command bar and eight views.

#### Command Bar

The command bar is a persistent operational control surface pinned to the top of every view. It provides the primary interaction point for dispatch and queue management — the controls the user reaches for most often, accessible without navigating to any specific view.

The command bar contains:

- **Job indicators** — one colored indicator per job, using the job's identity color. Each indicator shows the job's current operational state: idle, has pending tasks, has queued tasks, or has an active (running) task. The indicators provide at-a-glance awareness of which jobs have work in the pipeline.

  **Clicking a job indicator opens a dispatch popout** — a lightweight panel anchored to the indicator that lets the user dispatch a manual task for that job. The popout provides a context field for the dispatch message and a dispatch action. This is the primary manual dispatch surface — the user triggers work from anywhere in the application without navigating to the Dispatch or Jobs view. The popout closes after dispatch or on click-away.

  The dispatch popout replaces inline dispatch on the Jobs view. Manual dispatch is an operational action — it belongs on the persistent operational surface, not on the configuration view where it competes with job properties for attention.

- **Auto-queue toggle** — controls whether newly created tasks skip pending and go directly to queued. This is the same global setting previously located in Settings, elevated to the command bar because it directly governs how every trigger routes into the queue. The toggle provides immediate visual feedback of the current state (auto-queueing on/off).

- **Queue all** — a batch action that transfers all pending tasks to the queued state. This is the batch equivalent of dragging each pending task individually into the Active column. The action is only available when pending tasks exist.

- **Shelve all** — a batch action that transfers all queued tasks back to the pending state. This pulls everything off the execution runway back into the staging area for reconsideration. The action is only available when queued (non-active) tasks exist. It does not affect the currently running task — active execution is cancelled, not shelved.

The command bar's design rationale: the user's primary interaction with mAistro is through dispatches. A summary of job state plus core dispatch controls pinned to the top aligns the interface with this reality. The user should never need to navigate away from their current view to dispatch a job, check queue routing, or batch-manage queue state.

#### Views

- **Dispatch** — the operational center. A single three-column kanban that makes the entire task lifecycle visible at once:
  - **Upcoming** (left column) — the staging area. Newly created tasks land here by default. The user reviews, coalesces (merge and split), and curates tasks before promoting them. Pending tasks are not eligible for execution. Tasks are displayed by creation time. This column answers: "what work is waiting for my attention?"
  - **Active** (center column) — the execution pipeline. Contains queued tasks awaiting their turn and the currently running task. The running task (if any) appears at the top of the column, visually distinct from queued tasks below it. The user reorders queued tasks to control execution priority. This column answers: "what is running and what runs next?"
  - **Resolved** (right column) — all terminal states. Every task that has finished — completed, exhausted, failed, timed out, cancelled, interrupted, rejected — lands here. Each card carries a status badge identifying its terminal state. Completed (success) cards display the outcome summary and commit range. Non-success cards surface the error context inline — the user sees *why* it didn't succeed at a glance. Ordered by completion time (most recent first). This column answers: "what happened?"

  Tasks flow left to right through their lifecycle: Upcoming → Active → Resolved. The user drags tasks between Upcoming and Active to promote (pending → queued) or demote (queued → pending). Provides controls for cancelling active tasks, approving/rejecting tasks awaiting approval, and resuming/retrying/replying to resolved tasks.

  **Reply** — the user can reply to a resolved task with additional context or follow-up instructions. Reply creates a new task (via the `reply` trigger) and coalesces the original under it. The reply action requires a text input — the user must provide the follow-up message that becomes the new task's context. This is distinct from resume (which continues the same session without new input) and from session interrogation (which is read-only and does not create a task).

  **Detail drawer** — selecting any task opens a drawer that slides up from the bottom of the view. For active tasks, the drawer shows live streamed output (text, tool use, thinking indicators). For resolved tasks, it shows the stored session output, outcome summary, diff, and execution metadata (stop reason, turns consumed relative to the limit, duration, cost). For upcoming tasks, it shows trigger context and task metadata. For any task that has dispatched or will dispatch with external MCP servers, the drawer shows which servers were included in the dispatch configuration — so when a task fails, the operator can immediately see whether the failure correlates with a server issue. The drawer is resizable — the user controls how much vertical space it occupies. Closing the drawer returns full space to the columns. The drawer keeps the column layout visible above it, preserving spatial context while the user inspects a specific task.
- **Feed** — git history enriched with task metadata. Shows what changed and which tasks produced those changes.
- **Jobs** — the job configuration surface. Job configuration: create, edit, delete. Drag-to-reorder sets default execution priority for new tasks. Properties are organized by concern (definition, triggers). Manual dispatch is not on this view — it is on the command bar, where operational actions belong. The Jobs view is purely for defining *what* jobs are, not for triggering them.
- **Files** — a project file browser. The user searches for files by glob pattern and reads their contents. Markdown files render as formatted documents. Code files render with syntax highlighting for readability. This view provides direct, read-only access to project content without leaving the application.
- **MCP Servers** — tool server management as a dedicated surface. See MCP Servers View below.
- **Dashboard** — aggregated operational visibility. Answers "how are my agents doing?" without requiring the user to inspect individual tasks. Shows job health, task timing, and coordination patterns across configurable time windows. Read-only — no actions, no state changes. See Activity Dashboard below.
- **Settings** — platform configuration: default model, default timeout.
- **Governor** — the thread-based correspondence surface with the autonomous Governor agent. A two-pane layout: a thread list on the left (open threads with unread/proposal indicators, closed threads collapsed into a separate disclosure), and the selected thread's message history on the right with a compose box at the bottom. Governor messages that carry a proposal render the proposal as a structured card alongside the message body — there are no Approve/Decline buttons; the operator's response is the compose box. The operator can create new threads, post in open threads, close open threads, and reopen closed ones. The view shows no queue state and no diagnostic plumbing in its primary surface — those live in a collapsed-by-default debug drawer for operators who need to verify the engine is running. There is no manual "trigger Governor" button; conversations are initiated by creating threads. See the Governor section for full specification.

### Contextual Help (Tooltips)

Configuration fields that involve syntax rules, non-obvious behavior, or domain-specific concepts provide hover tooltips. The tooltip appears on a help indicator adjacent to the field label — not on the input itself — so it does not interfere with interaction.

Tooltips explain *rules and behavior*, not just labels. They answer: "what do I type here?" and "what will this do?"

Required tooltip surfaces:

- **Subscriptions (glob patterns)** — syntax: `*` matches files in one directory, `**` matches recursively across directories. One pattern per line. Dual purpose: patterns determine which commits trigger the job *and* which files are included as context in the task prompt.
- **Schedule (cron expression)** — five-field format: `minute hour day-of-month month day-of-week`. Ranges (`1-5`), lists (`0,15,30`), steps (`*/10`), and wildcards (`*`). Examples: `*/30 * * * *` (every 30 min), `0 9 * * 1-5` (weekdays at 9am). First evaluation after setting a schedule establishes a baseline — does not fire immediately.
- **Allowed Tools** — select which CLI tools the agent can use. The platform presents the full inventory of available tools; the user selects from this list. When any tools are selected, the agent sees only those tools plus tools from connected MCP servers. When none are selected, the agent gets the full default tool set. Tools not selected are removed from the agent's environment entirely — the agent has no awareness they exist.
- **Allowed Internal Tools** — select which internal MCP tools the agent can access. When any are selected, only those internal tools are presented. When none are selected, all internal tools are available. Use this to create read-only jobs (restrict to `list_files`, `read_file`, `git_log`, `git_diff`) or to grant branch management tools (`git_branch_create`, `git_branch_switch`, `git_branch_merge`) only to orchestrator jobs.
- **Allowed Dispatch Targets** — select which jobs this agent can programmatically dispatch via the `dispatch_task` tool. When none are selected, the agent cannot dispatch other jobs. This prevents unconstrained cross-agent triggering.
- **MCP Servers (per-job)** — select which registered external MCP servers this job's agent can connect to. Only checked servers are available during dispatch. The platform's internal server (git operations, file access) is always connected. Each server in the selection list shows its current health status (healthy, unreachable, disabled) — the user sees problems before dispatching, not after. Register servers in the MCP Servers view first, then enable them here per-job.
- **Require Approval** — when enabled, automated triggers (commit-watch, schedule, dependency) produce tasks that wait for manual approval before executing. Manual dispatches bypass this gate.
- **Coalesce Tasks** — when enabled, the job will never have more than one pending task. Any new trigger merges into the existing pending task instead of creating a new queue entry. Useful for jobs that should catch up in one run rather than queuing redundant work.
- **Allow Learning Self-Modification** — when enabled, the agent can add, update, and delete its own learnings (those it authored). When disabled (the default), the agent has read-only access to its learnings — it can introspect what guidance shapes its behavior but cannot change anything. Operator-authored learnings are never writable by the agent regardless of this setting. Use this when you want the agent to record discrete rules it learned from its own work that should shape next dispatch.
- **Dependencies** — the job auto-dispatches when *any* selected upstream job completes successfully. Circular chains are allowed — coalescing prevents runaway queuing. Timed-out, failed, or cancelled tasks do not trigger dependents.
- **Timeout** — maximum execution time in seconds. When reached, the platform gracefully terminates the agent, then force-kills if it does not exit. Timed-out tasks do not trigger downstream dependencies. Set to 0 for no limit.
- **Max Turns** — maximum number of agent turns before the session is stopped. A turn is one cycle of reasoning and output. Most tasks complete in well under the limit. When reached, the task is marked as exhausted (not completed) — the agent was cut off, not done. Exhausted tasks do not trigger downstream dependencies. Increase the limit if a job consistently needs more interaction, or tighten the instructions if the agent is doing unnecessary work.
- **Auto-queueing (Command Bar)** — when enabled, newly created tasks skip the pending column and go directly to queued, where the worker will pick them up. When disabled, all new tasks enter the pending column and must be manually transferred to queued before they can execute. The worker always runs — auto-queueing only controls the initial routing of new tasks. The user can override any individual task by dragging it between columns after creation.
- **Model** — the LLM model for this job. Opus: highest capability, slowest, most expensive. Sonnet: balanced capability and speed. Haiku: fastest, cheapest, best for simple or high-frequency jobs.

### MCP Servers View

MCP server configuration is a first-class surface with its own rail item. It is not buried in Settings. The reason: MCP servers are how users extend what agents can do — they are a primary capability concern, not a secondary platform setting. The view must be simple enough that a user who has never configured an MCP server can succeed on the first attempt.

**Design principles:**

- **Progressive disclosure.** The empty state is not a blank page — it explains what MCP servers are, why you might add one, and how to do it. Configuration complexity is revealed only as the user engages. A user with zero servers sees guidance. A user with three servers sees status and management.
- **Guided registration.** Adding a server requires a name and a command. Arguments and environment variables are optional but first-class. The form makes each field obvious — labeled inputs with placeholder examples that show real, working patterns (e.g., command: `npx`, args: `@modelcontextprotocol/server-filesystem`, `/path/to/dir`). Arguments are a structured list — each argument is a discrete entry, not a space-separated text field. This eliminates the class of errors where quoting or spaces produce wrong arguments. Environment variables are key-value pairs with add/remove controls. The form validates before submission: the command must be a resolvable executable, args and env vars must parse correctly. Validation errors explain what went wrong in plain language.
- **Immediate feedback.** After adding a server, the platform tests connectivity automatically and shows the result — healthy with a tool count, or an error with a plain-language explanation. The user never wonders "did it work?" The test action is also available on demand for any registered server.
- **Health at a glance.** Each server shows its current state: enabled/disabled, healthy/unreachable, and the tools it provides. The user can scan the list and immediately understand which servers are working.
- **Safe defaults.** New servers are enabled by default — the most common intent when adding a server is to use it. Disabling is a deliberate choice the user makes later if needed.

**What the view shows:**

- The list of registered servers, each displaying: name, command, arguments, environment variable count, enabled state, health status, and discovered tools (when healthy).
- A registration form for adding new servers.
- Per-server actions: enable/disable toggle, test connection, edit configuration, remove. Editing allows changing the command, arguments, and environment variables after registration.
- When a server is unhealthy, the error is shown inline — not hidden behind a click. Error messages should help the user fix the problem: "command not found" means the command isn't installed or isn't on PATH; "connection refused" means the server started but isn't responding correctly; "handshake failed" means the process started but didn't respond with a valid MCP protocol exchange.
- When a server is deleted, a confirmation shows which jobs currently reference it. After deletion, the server is removed from all jobs' `mcp_servers` lists — no stale references remain.

**What the view does not do:**

- No per-job assignment. That stays on the job configuration surface where it belongs — the MCP Servers view manages the global registry; jobs select from it.

**Relationship to job configuration:** The MCP Servers view is where servers are registered and managed. The job configuration surface (Jobs view) is where servers are assigned to specific jobs via the `mcp_servers` property. The per-job tooltip directs users to the MCP Servers view when they need to register new servers. This separation keeps each surface focused: one place to manage servers, another to assign them.

### Activity Dashboard

The Dispatch view shows individual tasks — what's running, what happened. As task volume grows through automated triggers, scheduled jobs, and agent-initiated dispatches, the user needs aggregated visibility: patterns across tasks, not just the tasks themselves. The Activity Dashboard is this surface.

**What the dashboard answers:**

- "How are my agents doing?" — per-job success and failure rates over time.
- "What needs attention?" — recurring failures, timeout patterns, jobs that consistently underperform.
- "When did things run?" — temporal patterns in task execution: scheduling conflicts, long-running outliers, idle gaps.
- "How do agents coordinate?" — which jobs dispatch other jobs, how deep chains go, where coordination breaks down.

**The dashboard is read-only.** It does not create, modify, or dispatch anything. It aggregates existing data from the `tasks` table and MCP tool call logs. No schema changes, no new data collection — the platform already records everything the dashboard needs. The dashboard is a lens on data that exists.

#### Job Health Summary

Each job shows a health summary across a selectable time window (today, 7 days, 30 days):

- **Task counts** — total tasks completed, failed, timed out, cancelled, interrupted, rejected. The breakdown by terminal state is essential — a job with 10 failures and 10 timeouts has two different problems.
- **Success rate** — completed tasks as a proportion of all terminal tasks. This is the single number that captures job health. A job running at 70% success needs investigation; one at 95% is healthy.
- **Trend indicator** — whether the success rate is improving, stable, or degrading compared to the previous equivalent window. The user needs to know not just current health but direction.

Jobs with low success rates or degrading trends are visually prominent — the dashboard surfaces what needs attention without the user hunting for it. The ordering and emphasis are health-driven, not alphabetical.

#### Timeline

A temporal view of task execution: when tasks ran and how long they took.

- **Horizontal bars** per task, positioned by start time and sized by duration. Color-coded by job. The user sees scheduling density, idle gaps, and outliers at a glance.
- **No chart library.** The timeline is rendered with basic HTML/CSS — positioned elements, not SVG or canvas. This keeps the implementation minimal and the rendering predictable.
- **Configurable window** — the same time windows as the health summary (today, 7d, 30d). The timeline and health summary share a time selector so the user sees consistent data.
- **Long-running outliers** are visually distinct. A task that took 10x the job's median duration stands out without the user calculating.

The timeline reveals patterns that individual task inspection cannot: bunching (too many tasks in a window), gaps (periods of no activity when activity was expected), and overlap awareness (even though execution is sequential, queued-at times reveal demand patterns).

#### Agent Dispatch Chains

When agents dispatch other agents, the resulting chains are a new dimension of system behavior that is invisible in the Dispatch view's flat task list.

- **Dispatch graph** — for each agent-initiated task, show the chain: which task dispatched it, which task dispatched that one, back to the original trigger. This is a tree, not a cycle — each task has at most one originating task.
- **Chain depth** — how many levels deep agent-initiated dispatches go. A chain of depth 1 (agent dispatches one task) is normal coordination. Depth 3+ may indicate emergent behavior worth inspecting.
- **Job-to-job patterns** — which jobs dispatch which other jobs, aggregated over time. This reveals the coordination topology: "Architect always dispatches Engineer," "Engineer never dispatches anything." The user understands agent relationships without reading individual task histories.

This surface makes the `agent` trigger type legible. Without it, agent coordination is an invisible graph embedded in trigger metadata.

#### Tool Usage Patterns

Each job's agent uses tools differently. The audit trail (MCP tool call logs) already records every tool invocation. The dashboard surfaces patterns:

- **Per-job tool frequency** — which internal MCP tools each job uses most. A job that calls `git_commit` 20 times per task works differently than one that calls it once. A job that never uses `read_file` despite having file subscriptions may have misconfigured instructions.
- **Tool errors** — tool calls that return errors, aggregated by job and tool. Persistent tool errors indicate a configuration or instruction problem.

Tool patterns are secondary to health and timing — they support investigation, not triage. The user notices a job has low success rates (health summary), checks when it runs (timeline), then looks at what it does (tool patterns) to diagnose the problem.

#### Design Principles

- **Aggregation, not raw data.** The dashboard never shows individual task records — that's what the Dispatch view does. Every element is a summary, a count, a rate, or a pattern derived from multiple tasks.
- **Time-windowed.** All data is scoped to a configurable time window. The dashboard shows the recent picture, not all-time history. The time selector is global to the view — health summary, timeline, and dispatch chains all respond to the same window.
- **Health-driven emphasis.** Jobs that need attention are visually prominent. A healthy system fades into the background; problems surface. This is the opposite of a status board that treats everything equally.
- **Derived from existing data.** The dashboard reads from `tasks` (lifecycle timestamps, trigger metadata, job associations) and MCP tool call session events (tool names, results). It introduces no new data collection, no new tables, no new event types. If the data doesn't already exist, the dashboard doesn't show it.
- **No actions.** The dashboard is purely informational. The user cannot dispatch, cancel, retry, or configure from the dashboard. Actions belong on the surfaces designed for them (Dispatch, Jobs). The dashboard informs decisions; other views execute them.

---

## Constraints

### Operational Integrity

- **Queue-first invariant**: every dispatch passes through the queue as a task before execution. All code paths — manual, watch, schedule, dependency — enqueue first, then execute. Tasks enter as either pending or queued (determined by auto-queueing setting) but always exist as queue records before execution.
- **Two-stage progression**: tasks must be in the queued state before the worker will pick them up. Pending tasks are invisible to the worker. This ensures the user always has an opportunity to review and curate work before it executes (unless auto-queueing is deliberately enabled).
- **Sequential execution**: exactly one task runs at a time. The worker holds a lock during processing.
- **Project isolation**: each project has its own SQLite database. The app-level database holds only the recent-projects list.
- **Git is content source-of-truth**: all project content lives in git. The SQLite database holds only operational state (job configs, task records, chat sessions).
- **Single active project**: the platform operates on one project at a time. The active project is global state that all operations reference.
- **Dashboard is read-only**: the Activity Dashboard performs only read queries on existing data. It introduces no new tables, no new event types, and no write operations. All aggregations derive from `tasks` lifecycle columns and MCP tool call session events that already exist.

### Data Integrity

- **Job identity is immutable**: a job's slug ID, once derived from its initial name, stays constant. All references (tasks, properties, dependencies) use the slug. Renaming changes only the display label.
- **Task lifecycle is monotonic**: a task progresses from created → queued → started → terminal. Terminal states are: completed (success), exhausted (turn limit), failed, timed out, cancelled, interrupted, or rejected. A task may skip pending (via auto-queueing) or move back from queued to pending (via manual transfer), but once started, progression is forward-only. Retry creates a new cycle by resetting lifecycle fields on the same record, preserving task identity.
- **Trigger context is immutable at enqueue time**: each trigger entry's context string is built when the trigger fires. This preserves the causal record — the prompt reflects what was true when the trigger occurred.
- **Job deletion cascades**: removing a job removes all associated data (properties, tasks, sessions). This prevents orphaned records.
- **Task status is authoritative**: each task has a well-defined status that progresses through a validated state machine. Status transitions are enforced — invalid transitions (e.g., pending directly to completed, or any transition out of a terminal state) are rejected. The status is the single source of truth for where a task is in its lifecycle. Lifecycle timestamps record *when* transitions happened; the status records *where the task is now*.
- **Outcome summaries are derived from git**: the summary is computed from commits between `start_commit` and `result_commit`. It reflects what the repository records, not what the agent claims. A task that produces no commits has no summary.
- **Job color is non-null**: every job has a color from creation. The platform assigns a random color from a curated palette when a job is created. The palette is chosen for visual distinguishability — high saturation, evenly distributed hues, readable against both light and dark backgrounds. The user can override the color at any time. Color has no behavioral effect — it is purely visual metadata.
- **Merge preserves trigger history**: merging pending tasks concatenates their trigger arrays. No trigger entry is lost or rewritten. The surviving task's triggers are the union of all source tasks' triggers, ordered by original creation time.
- **Split produces valid tasks**: each task created by split carries exactly one trigger entry from the original. The original task retains its first trigger and identity; new tasks get fresh IDs and are appended to the pending column.
- **Coalescing belongs to Upcoming**: merge and split operate exclusively on pending tasks in the Upcoming column. This is the curation stage where work is grouped and decomposed. Once a task is promoted to Active, its composition is fixed — the Active column is for prioritization and execution, not restructuring.
- **Sorting belongs to Active**: reordering operates exclusively on queued tasks in the Active column. Queue position determines execution priority. The Upcoming column displays tasks by creation time — it has no user-controlled sort order.
- **Cross-column drag is transfer only**: dragging between Upcoming and Active changes state (pending ↔ queued) without merging or reordering within the target column. Transfer and composition/sorting are distinct user intentions that must not be conflated in a single gesture.
- **Same-job constraint on merge**: only tasks belonging to the same job can be merged. A task's identity is bound to one job; cross-job merging would violate prompt assembly, tool configuration, and commit authorship invariants. The UI enforces this structurally — the merge affordance does not appear when tasks belong to different jobs, so the invalid operation is never offered.

### External MCP Integrity

- **Registration validates eagerly**: an external MCP server registration is rejected if the command is not a resolvable executable or if arguments and environment variables do not parse correctly. Invalid configuration never reaches the database — errors surface at the moment the user submits the form, not when a task dispatches minutes or hours later.
- **Dispatch gates on server health**: before a task begins execution, all external MCP servers assigned to its job are probed. If any server is unreachable, the task fails with a specific error naming the server and the failure reason. A task never runs with a silently missing tool surface.
- **Disabled servers are never dispatched**: a disabled server is excluded from dispatch configuration unconditionally. The `enabled` flag is authoritative — there is no default-to-enabled fallback for missing or ambiguous state. Jobs referencing a disabled server see a visible warning on their configuration surface.
- **Server deletion cascades to job references**: deleting a server removes it from every job's `mcp_servers` list. No job silently loses tools because it references a server that no longer exists — the reference is cleaned up atomically with the deletion.
- **MCP errors are attributed to their source**: when an MCP server failure occurs during task execution, the error identifies the server by name and describes the failure mode. Generic CLI errors that originate from MCP server failures are enriched with server-specific context before being surfaced to the user.
- **Config assembly validates before dispatch**: the assembled MCP configuration — the composite of all servers assigned to a job — is validated as well-formed JSON before being written to disk and passed to the CLI. Malformed arguments, invalid environment variable structures, or any composition error is caught at assembly time with a specific error naming the problematic server, not propagated as an opaque CLI failure. This is defense-in-depth: registration validates individual server configs; assembly validates the composed whole.
- **Server configuration is self-contained**: environment variables required by a server are stored as part of the server's registration, not as external system state. The platform passes them to the server process at launch. A server registration contains everything needed to launch and connect to the server.

### Accountability

- **Tool mediation is observable**: every tool call that flows through the internal MCP server is logged as a structured event. The platform can reconstruct exactly what an agent did, not just what it produced.
- **Context-aware tool surfaces**: the set of tools available to an agent is determined by the job's configuration, not by the agent's own choices. The platform controls what actions are possible. This applies to all three tool dimensions: CLI tools (`allowed_tools`), internal MCP tools (`allowed_internal_tools`), and external MCP servers (`mcp_servers`).
- **Tool restrictions are invisible to agents**: an agent only sees tools it is allowed to use. Tools outside the allowed set are removed from the agent's environment — not mentioned, not instructed against, not present. A headless agent has no mechanism to negotiate access; presenting tools it cannot use would only produce failed attempts or prompt-level workarounds. The platform owns the restriction surface; the agent owns only its allowed capabilities.
- **Tool inventory is discoverable**: the platform presents the complete set of available tools — built-in CLI tools, internal MCP tools, and external MCP server tools — so the user configures from known options rather than guessing. Tool configuration surfaces select from what exists; they do not accept arbitrary text that may not correspond to real tools.
- **Dispatch attribution**: tasks created by agents via `dispatch_task` are tagged with the originating task, creating a provenance chain visible in history. The user can trace any agent-dispatched task back to the task that requested it.
- **Branch operations are auditable**: all git branch tool calls (create, switch, merge) are logged in the task session like any other MCP tool call. The platform can reconstruct which branches a task created, operated on, and merged.

### Safety

- **Active task locks project state**: while a task is running, the platform keeps the project directory unchanged. This prevents state corruption from changing the working directory mid-execution.
- **Task workspace is isolated**: each task operates in a dedicated git worktree, not the project's main checkout. The agent's working directory for the duration of a dispatch is the worktree; the user's main checkout is structurally untouchable. Orphaned changes from non-success terminal states never pollute the project's working tree — they are confined to the task's workspace and surfaced with explicit discard / merge controls. See [Task Workspace Isolation](#task-workspace-isolation).
- **Stale sweep on startup**: any task marked as in-flight when the process starts is marked interrupted. This eliminates zombie tasks.
- **Approval gates apply to all automated triggers**: manual dispatch (explicit human intent) bypasses the approval check; all other trigger types — including `agent` triggers — respect `require_approval`.
- **Timeouts are enforced**: every task has a configurable timeout (inherited from its job). The watchdog runs unconditionally. Jobs have bounded execution time.
- **Dependency cycles coalesce gracefully**: circular dependency chains produce redundant triggers that coalescing absorbs, preventing unbounded task growth.
- **Post-commit hook is non-blocking**: the hook runs asynchronously and fails silently. Hook failures never prevent or delay git operations.
- **Session interrogation is read-only**: resumed task sessions for interrogation strip all write tools. The agent can read and reason but cannot modify the project. This preserves the queue-first invariant — all modifications flow through the task queue.
- **Agent dispatch is governed**: an agent can only dispatch tasks for jobs listed in its `allowed_dispatch_targets`. No self-dispatch. A depth limit on agent-initiated dispatch chains prevents runaway cascades. Coalescing absorbs redundant agent-triggered enqueues.
- **Branch operations are explicit**: branch creation, switching, and merging are structured tool calls — not unmediated shell commands. Merge conflicts surface as structured output, not silent failures. Naming conventions on branch creation prevent namespace collisions between jobs.
- **Governor is meta-scoped**: the Governor agent observes and modifies only the meta layer — job configuration, learnings, properties, queue settings. It cannot modify project files, commit code, or dispatch tasks. Write tools are available only on reply invocations (responding to operator action in a thread), never on surveys.
- **Governor write surface is constructive only**: the Governor can update job properties, create jobs, and update queue settings. It cannot delete jobs, disable jobs, or close threads. Destruction and muting are operator-only actions — there is no MCP write tool that reaches them. The Governor may recommend deletion or closure in prose; the operator is the only one who can act on it.
- **Closed threads are invisible to the Governor**: closing a thread is operator-only and removes the thread entirely from every subsequent Governor invocation's context — title and content alike. The Governor cannot read closed threads, cannot post in them, and cannot reopen them. Reopening is operator-only. Closure is the operator's mute control over the Governor's awareness.
- **Approval is prose, not buttons**: the Governor's proposals carry no Approve/Decline affordances. The operator's response is a thread reply. The next reply invocation reads the conversation, interprets intent, and writes only when assent is clear. Stale proposals are verified against current state before execution — if the world moved, the Governor does not blindly apply.
- **Reply invocations coalesce per-thread**: if a reply for a given thread is already queued or in flight when the operator posts again in that thread, the new message merges into the pending invocation rather than enqueueing a second one. The Governor produces one response covering everything; the latest operator message is operative when interpreting intent.
- **Learning self-modification is asymmetric**: an agent with `allow_learning_self_modification` enabled can add, update, and delete only the learnings it itself authored (`source='agent'`). Operator-authored learnings (`source='human'`) are never writable by the agent regardless of the property. This is enforced at the tool surface, not by prompt instruction. The operator UI permits anything on either source — operator authority is absolute on operator-authored content.
- **Learnings preserve provenance**: every learning is stamped at creation with its source (`human` or `agent`) and that source is immutable. The platform can always tell who authored a piece of guidance.
