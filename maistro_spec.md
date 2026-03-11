# mAistro v2 — Functional Specification

## System Overview

mAistro is an intent-to-reality development engine driven by LLM agents that coordinate through a git repository. Agents maintain project knowledge as markdown files, produce code, and communicate decisions through commit messages. A lightweight backend manages agent configuration, dispatch, and human-agent chat. A React frontend provides a git-native activity feed, agent configurator, and chat interface.

All project truth lives in git. The database holds only operational state.

---

## Core Architecture

### What Lives Where

| Layer | Contents | Why |
|-------|----------|-----|
| **Git** | Project artifacts, agent knowledge files, specs, code, docs — anything that represents the state of the thing being built | History, diffability, auditability. If losing it means the project lost knowledge, it's git. |
| **Database** | Agent configs, dispatch queue, watch patterns, chat sessions, operational state | Machinery. If losing it means you reconfigure the tool, it's DB. |
| **Dispatch** | Post-commit hook, watch pattern matcher, agent queue, manual dispatch | The glue that decides when agents run. |

### Technology Stack

| Layer | Technology | Role |
|-------|-----------|------|
| Frontend | React + Vite | Git feed, agent config, chat |
| Backend | Python + FastAPI | Agent invocation, dispatch, SQLite state, SSE streaming |
| Realtime | SSE (Server-Sent Events) | Streams LLM output and dispatch events |
| LLM Invocation | Claude CLI subprocess | Async subprocess with NDJSON streaming |
| Tools | MCP + FastMCP | Agents access filesystem and dispatch queue via MCP |
| Database | SQLite (aiosqlite, WAL mode) | Agent config, dispatch queue, chat sessions |
| VCS | Git (via subprocess) | Project state, commit tracking, file watching |

---

## Agents

Agents are user-created LLM personas with configurable identity, tools, and dispatch behavior. Each agent has a defined area of concern and maintains project knowledge within that scope.

### Agent Properties (Database)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `name` | string | required | Display name |
| `persona` | string | `''` | System prompt / identity |
| `base_tools` | JSON array | `[]` | Allowed CLI tools (Read, Glob, Grep, Edit, Write, Bash, etc.) |
| `mcp_servers` | JSON array | `[]` | Scoped MCP servers (controls access to queue_agent, subscribe_to, etc.) |
| `input_artifacts` | JSON array | `[]` | Glob patterns for files this agent reads as input (e.g. `["plan/tasks.md", "src/**"]`) |
| `output_artifacts` | JSON array | `[]` | Glob patterns for files this agent owns and writes (e.g. `["qa/**"]`) |
| `mode` | string | `'manual'` | Dispatch mode: `manual`, `auto`, or `watch` |
| `cooldown_seconds` | int | `30` | Minimum time between triggered dispatches |
| `running` | boolean | `false` | Is a dispatch currently in-flight |
| `sort_order` | int | `0` | UI ordering |

Agent IDs are slugified from names.

**Watch patterns are derived from input artifacts.** When an agent is in `watch` mode, its `input_artifacts` globs are used as the commit trigger patterns. There is no separate `watch_patterns` property — if you want an agent to react to file changes, you declare those files as inputs. This enforces the principle that an agent only triggers on things it actually reads.

Agents can dynamically update their own `input_artifacts` via MCP tool (subscribing to new paths). The human can see and override these in the config UI.

### Control Model

The human controls agent behavior through two mechanisms:

1. **Tool permissions** — Which MCP servers and tools an agent can access. An agent without `maistro-dispatch` cannot queue other agents or modify its own subscriptions. This is the primary blast radius control. If the Planner shouldn't be queued by other agents, don't give other agents the ability to queue it (or scope the queue tool to exclude it).

2. **Dispatch mode** — Manual/Watch/Auto determines when the agent runs. Flipping any agent to Manual immediately stops autonomous triggering.

Cooldown is a rate limiter, not an intelligence constraint. It prevents rapid re-firing but doesn't limit the depth of agent coordination. Agents are autonomous workers — the human controls their capabilities, not their decision-making.

### Dispatch Modes

| Mode | Behavior |
|------|----------|
| **Manual** | Agent runs only when a human dispatches it via the UI |
| **Watch** | Agent runs when a commit touches files matching its `input_artifacts`. Subject to cooldown and chain depth limits. |
| **Auto** | Agent runs on a polling interval, reads current project state, decides what to do. Subject to cooldown. |

All three modes produce the same execution: agent reads project state, does work, commits results. The trigger mechanism varies but the execution model is identical.

---

## Project Knowledge Model

Agents maintain project knowledge as files in the git repository. There is no message board or post/comment system. The filesystem is the collaboration surface.

### Input and Output Artifacts

Each agent has a clear data flow:

- **Input artifacts** — files the agent reads to know what to do. Other agents' outputs, project intent, specs, code. These are the agent's triggers (in watch mode) and its context (in all modes).

- **Output artifacts** — files the agent owns and maintains. Its bug list, test status, executive summary. These are its contribution to the project. No other agent should write to these files. The agent is expected to keep them current on every run.

This makes the data flow between agents explicit. When you configure an agent, you're wiring a pipeline: "this agent reads from here, writes to here." Across all agents, the input/output declarations form a dependency graph.

### Executive Summary Requirement

Every agent must maintain an executive summary as one of its output artifacts: `{agent_dir}/summary.md`. This file captures the agent's current understanding of the project from its perspective. It is updated on every run.

The summary serves as a lightweight coordination mechanism. Rather than reading every line of every agent's artifacts, an agent (or human) can read the summaries to get each agent's filtered perspective. The Architect's summary emphasizes structural concerns, QA's summary emphasizes risk, the Planner's summary emphasizes priorities.

Enforcement is via persona instruction. If the LLM can't maintain a summary reliably, it can't do anything else reliably either.

### Artifact Manifest

Every agent receives an artifact manifest in its system prompt context — a listing of all agents and their output artifact paths. Not the file contents, just the registry:

```
Agents and their output artifacts:
- Planner → plan/
- Architect → architecture/
- QA → qa/
- Worker → src/
```

This tells each agent where project knowledge lives, prevents duplication (the agent knows QA owns the bug list, so it doesn't create its own), and lets agents discover what to read without being explicitly told.

### Artifact Roles

Artifacts serve three roles simultaneously:

1. **Agent state** — The agent reads its own output artifacts to remember what it knows. When dispatched, both input and output artifacts are included in its context.

2. **Cross-agent interface** — One agent's output artifacts are another agent's input artifacts. The dependency graph is the team's communication structure.

3. **Human interface** — A human reads an agent's output artifacts to understand what it knows. They can edit any artifact and commit. The owning agent picks up changes on its next run. Editing another agent's inputs triggers that agent if it's in watch mode. The human is just another worker.

### Convention

Each agent with a knowledge-maintenance role owns a directory:

```
project/
├── intent.md                  # Human-authored project intent (no owner)
├── plan/
│   ├── tasks.md               # Planner output
│   └── summary.md             # Planner's executive summary
├── architecture/
│   ├── system-design.md       # Architect output
│   └── summary.md             # Architect's executive summary
├── qa/
│   ├── bugs.md                # QA output
│   ├── test-status.md         # QA output
│   └── summary.md             # QA's executive summary
├── src/                       # Worker output (code)
│   └── ...
└── .maistro/
    └── maistro.db             # Operational state (not committed)
```

File ownership is enforced by convention through agent personas and the artifact manifest. Any agent can read any file. Only the owning agent should write to its output artifacts.

### Cross-Agent Coordination

Agents coordinate through the input/output artifact graph. Two mechanisms:

1. **Pull (watch mode)** — Agent's input artifacts change → agent is triggered → reads new state → does work → commits to its output artifacts → may trigger downstream agents.

2. **Push (queue tool)** — Agent explicitly queues another agent via MCP tool with a reason. Used when the relationship isn't captured by file changes (e.g., "Architect, I need you to reconsider the auth design based on what I found").

Both mechanisms are available simultaneously. Watch handles the routine data flow. Queue handles the exceptions.

---

## Dispatch System

### Dispatch Queue

A simple database table. Rows represent pending agent runs. Processed rows are deleted or marked complete.

| Column | Type | Description |
|--------|------|-------------|
| `id` | int | Primary key |
| `agent_id` | string | Which agent to run |
| `trigger` | string | `commit`, `agent_queue`, `manual`, `auto` |
| `trigger_detail` | string | Commit hash, queuing agent ID, or null |
| `instructions` | string | Human-provided instructions (manual only) |
| `created_at` | datetime | When queued |
| `started_at` | datetime | When dispatch began (null = pending) |
| `completed_at` | datetime | When dispatch finished (null = in progress) |
| `result_commit` | string | Commit hash produced by this dispatch (if any) |
| `error` | string | Error message if dispatch failed |

### Trigger Mechanisms

#### 1. Commit-Triggered (Watch)

A post-commit hook runs after every commit:

1. Get list of changed files from the commit
2. Query all agents where `mode = 'watch'` and `running = false`
3. For each agent, check if any changed file matches its `input_artifacts` patterns
4. For matching agents, check cooldown (last dispatch within `cooldown_seconds`?)
5. Queue matching agents that pass cooldown

The post-commit hook is a generic script that calls a backend endpoint. All logic lives in the backend.

#### 2. Agent-Queued (MCP Tool)

Agents can queue other agents via an MCP tool:

```
queue_agent(agent_id, reason)
```

This inserts a dispatch queue row with `trigger = 'agent_queue'`. Only available to agents whose MCP server config includes `maistro-dispatch`. The tool itself can be scoped to limit which target agents are queueable.

#### 3. Manual (Human)

Human clicks "Run" on an agent in the UI, optionally with instructions. Inserts a dispatch queue row with `trigger = 'manual'`. Manual dispatch ignores cooldowns.

#### 4. Auto (Polling)

Background loop checks for agents with `mode = 'auto'` on a configurable interval. If the agent isn't running and cooldown has elapsed, queue it with `trigger = 'auto'`.

### Dispatch Execution

When a dispatch queue item is processed:

1. Set `started_at`, set agent `running = true`
2. Build system prompt: identity preamble → persona → artifact manifest → list of available tools
3. Build context prompt: agent's input and output artifact contents, recent git log
4. Invoke LLM via Claude CLI subprocess with MCP servers
5. Stream response via SSE to frontend
6. Agent reads files, uses tools, produces output, updates its summary
7. Any file changes are committed with a descriptive message authored by the agent
8. Set `completed_at`, set agent `running = false`
9. Post-commit hook fires for any new commits, potentially triggering downstream agents

### Prompt Assembly

System prompt layers:
1. **Identity preamble** — "You are {name}, an agent in mAistro. Your role: {persona}"
2. **Artifact manifest** — All agents and their output artifact paths (prevents duplication, enables discovery)
3. **Output artifacts** — Current contents of this agent's owned files (its own state)
4. **Input artifacts** — Current contents of files this agent reads (its context)
5. **Recent activity** — Recent git log entries so the agent knows what's happened
6. **Summary requirement** — "You must update your summary.md with your current perspective on the project"
7. **Instructions** — If manually dispatched with human instructions, append them

### Guardrails

The primary control surface is **tool permissions**, not depth counters. The human controls what each agent can do, not how many times it can cascade.

| Mechanism | Purpose |
|-----------|---------|
| **Tool permissions** | MCP server scoping controls which agents can queue, subscribe, access DB. This is the primary blast radius control. |
| **Dispatch mode** | Flipping an agent to Manual immediately stops autonomous triggering |
| **Cooldown per agent** | Rate limiter — prevents rapid re-firing |
| **Timeout per invocation** | Hard stop on runaway single invocations |
| **Dispatch queue visibility** | UI shows full queue — pending, running, completed, errored. Human can cancel or pause at any time. |

---

## Git Integration

### Commit Authoring

When an agent produces file changes, the backend commits them with:

- **Author**: `Agent Name <agent-id@maistro.local>`
- **Message**: Agent-generated reasoning for the change. The commit message is the agent's explanation of what it did and why.

The backend stages changed files, creates the commit, and returns the commit hash to the dispatch record.

### Commit Tracking

The backend maintains awareness of recent commits for building agent context. This is done via `git log` subprocess calls, not by mirroring git state into the database.

The dispatch queue records `trigger_detail` (commit hash) for commit-triggered dispatches, providing the link from "this agent ran because of that commit."

### Post-Commit Hook

A script at `.git/hooks/post-commit` that calls the backend:

```bash
#!/bin/bash
curl -s -X POST http://localhost:PORT/api/hooks/post-commit \
  -H "Content-Type: application/json" \
  -d "{\"commit_hash\": \"$(git rev-parse HEAD)\"}"
```

The backend handles all matching and queuing logic.

---

## MCP Servers

### maistro-dispatch

Agent coordination and self-configuration tools.

| Tool | Description |
|------|-------------|
| `queue_agent` | Queue another agent to run, with a reason |
| `get_dispatch_status` | Check if an agent is currently running |
| `get_recent_activity` | Get recent dispatch history |
| `get_artifact_manifest` | Returns all agents' input/output artifact declarations — the team registry |
| `subscribe_to` | Add a glob pattern to this agent's `input_artifacts` (self-subscription) |
| `unsubscribe_from` | Remove a glob pattern from this agent's `input_artifacts` |

### maistro-git

Git operations scoped to the project.

| Tool | Description |
|------|-------------|
| `git_log` | Read commit history, optionally filtered by path |
| `git_diff` | Diff between commits or working tree |
| `git_show` | Show a specific commit's contents |
| `git_status` | Current working tree status |

Note: File read/write/edit operations are provided by Claude CLI's built-in tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`). The MCP servers provide only the coordination and git-specific tools that CLI doesn't have natively.

### Custom Servers

Users can register additional MCP servers via settings. Server configs support placeholder substitution for project paths.

---

## Chat Sessions

Human-to-agent chat is the primary manual interaction surface. Chat sessions are persisted in the database.

| Table | Purpose |
|-------|---------|
| `chat_sessions` | Per-agent conversation sessions (id, agent_id, title, created_at) |
| `chat_messages` | Message history (session_id, role, content, created_at) |

Chat supports:
- Freeform conversation with any agent
- Agent has access to all MCP tools during chat (can read/write files, queue agents, check git)
- Session continuity via Claude CLI session resumption
- Dispatch-initiated sessions are saved alongside manual chats

When chatting, the agent operates with full project context — it can read files, check git history, and even commit changes or queue other agents if the conversation leads there. Chat is a collaboration mode, not a read-only interface.

---

## Frontend

### Layout

```
┌──────────────────────────────────────────────────┐
│  ⬡  [☰ Feed] [◉ Agents] [⚙]           🔍       │
├──────┬───────────────────────────────────────────┤
│      │                                           │
│ Rail │              Main Area                    │
│      │                                           │
│      │                                           │
└──────┴───────────────────────────────────────────┘
```

Three views, one rail:

#### 1. Feed (Home)

The activity feed is driven by git log enhanced with dispatch metadata.

**Feed items** are derived from:
- Git commits (with agent attribution, changed files, commit message as reasoning)
- Dispatch events (agent started, completed, errored — from dispatch queue)

**Each feed item shows:**
- Agent avatar + name
- Provenance indicator (⚡ commit-triggered, → agent-queued, 💬 manual/chat, ⏱ auto)
- Commit message (reasoning)
- Changed files summary
- Timestamp

**Status bar** at top shows agent health: running/idle/error per agent.

**Clicking a feed item** opens a detail slide-over showing:
- Full commit message
- File diff viewer
- Link to agent config
- Option to dispatch another agent on this commit ("Invoke" pattern)
- Option to open chat with the committing agent about this change

#### 2. Agents

The agent view is split: artifacts on top, config below. Artifacts are what the agent knows and produces. Config is how it behaves. Most visits are to read or edit artifacts, not to change config.

**Agent list** on left. Selecting an agent shows:

**Artifacts panel** (primary, tabbed into Outputs and Inputs):
- **Outputs** — files this agent owns and maintains. Resolved from `output_artifacts` globs. Editable inline. Each file shows: path, last commit message snippet, last modified time. Click to open inline viewer/editor. Edit + save = git commit attributed to the human. Includes the agent's `summary.md` prominently.
- **Inputs** — files this agent reads as context. Resolved from `input_artifacts` globs. Read-only view (these are owned by other agents or the human). Shows which agent owns each file. Useful for understanding what's driving this agent's behavior.
- File git history accessible per-artifact (commit log filtered to that path)

**Config panel** (secondary, collapsible):
- Identity (name, persona/system prompt)
- Model selection
- Tools (CLI tools + MCP servers)
- Output artifacts (glob editor — what files this agent owns)
- Input artifacts (glob editor — what files this agent reads and watches)
- Dispatch mode (Manual / Watch / Auto toggle)
- Guardrails (cooldown, chain depth, timeout, error threshold)

**Actions:**
- **Run** — dispatch the agent immediately
- **Chat** — open a chat session with this agent
- **Test Run** — dispatch once with current config without saving

#### 3. Chat

Agent selector + chat interface. Contextually aware — if opened from a feed item, the commit/change is pre-loaded as context.

**Chat input area:**
- Agent dropdown (switch mid-conversation)
- Text input with send
- "Post to board" equivalent is gone — agent just commits if the conversation produces file changes

### Detail Slide-Over

Opens from feed items. Tabs:

| Tab | Content |
|-----|---------|
| **Diff** | File changes from this commit, syntax-highlighted |
| **Invoke** | List of agents with "Run" buttons. Agent receives this commit's context. Optional instructions field. |
| **Chat** | Opens chat with the commit's agent, referencing this change |

This is the bumpless transfer surface. You're reading a diff (observing), you invoke an agent on it (directing), you open a chat about it (collaborating). Same commit context, escalating intervention.

### Provenance Indicators

| Icon | Trigger | Meaning |
|------|---------|---------|
| ⚡ | `commit` | Commit-triggered via watch patterns |
| ↗ | `agent_queue` | Another agent queued this one |
| → | `manual` | Human dispatched directly |
| 💬 | `manual` (from chat) | Human initiated via chat conversation |
| ⏱ | `auto` | Scheduled/polling auto-dispatch |

---

## Database Schema

Minimal. Only operational state.

### Tables

```sql
-- Agent registry and configuration
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE agent_property_defs (
    key TEXT PRIMARY KEY,
    default_value TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'string'
);

CREATE TABLE agent_properties (
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    key TEXT REFERENCES agent_property_defs(key),
    value TEXT NOT NULL,
    PRIMARY KEY (agent_id, key)
);

-- Dispatch queue
CREATE TABLE dispatch_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    trigger TEXT NOT NULL, -- 'commit', 'agent_queue', 'manual', 'auto'
    trigger_detail TEXT,   -- commit hash, queuing agent id, or null
    instructions TEXT,     -- human-provided instructions (manual only)
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    result_commit TEXT,    -- commit hash produced by this dispatch (if any)
    error TEXT             -- error message if dispatch failed
);

-- Chat sessions
CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    title TEXT,
    cli_session_id TEXT,  -- Claude CLI session ID for resumption
    created_at DATETIME DEFAULT (datetime('now'))
);

CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,    -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now'))
);

-- MCP server registry
CREATE TABLE mcp_servers (
    name TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1
);

-- App config
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### Views

```sql
-- Composed agent with all properties as JSON
CREATE VIEW v_agents AS
SELECT
    a.id, a.name, a.created_at,
    json_group_object(
        COALESCE(ap.key, apd.key),
        COALESCE(ap.value, apd.default_value)
    ) as properties
FROM agents a
CROSS JOIN agent_property_defs apd
LEFT JOIN agent_properties ap ON ap.agent_id = a.id AND ap.key = apd.key
GROUP BY a.id;
```

---

## API Surface

Significantly reduced from v1. No boards, posts, comments, or notifications.

### System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| POST | `/api/hooks/post-commit` | Post-commit hook endpoint — receives commit hash, triggers watch matching |

### Project

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/project/` | Current project info |
| POST | `/api/project/open` | Open/initialize project directory |
| POST | `/api/project/close` | Unload current project |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/` | List all agents with composed properties |
| POST | `/api/agents/` | Create agent |
| GET | `/api/agents/{id}` | Get agent with properties |
| PATCH | `/api/agents/{id}` | Update agent properties |
| DELETE | `/api/agents/{id}` | Delete agent |
| POST | `/api/agents/reorder` | Reorder agent list |
| GET | `/api/agents/{id}/artifacts` | Resolve agent's `output_artifacts` and `input_artifacts` globs → grouped file lists with metadata (path, owner agent, last commit, last modified) |
| GET | `/api/agents/{id}/artifacts/manifest` | Artifact manifest: all agents' input/output declarations |

### Dispatch

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/dispatch/{agent_id}` | Manual dispatch (SSE stream). Optional body: `{ instructions }` |
| GET | `/api/dispatch/queue` | Current dispatch queue state |
| POST | `/api/dispatch/cancel/{dispatch_id}` | Cancel in-flight or pending dispatch |

### Feed

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/feed/` | Git log + dispatch metadata, merged and sorted. Params: `limit`, `offset`, `agent_id`, `path` |
| GET | `/api/feed/{commit_hash}` | Single commit detail: message, diff, dispatch record if any |

### Chat

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat/` | Stream chat message (SSE). Body: `{ agent_id, session_id?, message, context? }` |
| GET | `/api/chat/sessions` | List sessions, optionally by agent_id |
| GET | `/api/chat/sessions/{id}/messages` | Session message history |
| DELETE | `/api/chat/sessions/{id}` | Delete session |

### Git

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/git/log` | Git log with optional path filter and limit |
| GET | `/api/git/diff/{commit_hash}` | Diff for a specific commit |
| GET | `/api/git/status` | Working tree status |
| GET | `/api/git/file/{path}` | Read file at current HEAD |
| PUT | `/api/git/file/{path}` | Write file and commit. Body: `{ content, message? }`. Author is the human. Triggers post-commit hook. |

### MCP Servers

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/mcp/servers` | List configured servers |
| POST | `/api/mcp/servers` | Register server |
| PATCH | `/api/mcp/servers/{name}` | Update server |
| DELETE | `/api/mcp/servers/{name}` | Remove server |

### Config

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config/` | All config |
| POST | `/api/config/{key}` | Upsert config value |

---

## What Was Removed (vs v1)

| Removed | Replaced By |
|---------|-------------|
| Boards | Agent-owned directories in git |
| Posts | Markdown files committed to repo |
| Comments | Commit messages, or direct file edits |
| Notification/unread queue | Watch patterns + commit triggers |
| Post watchers | Agent watch_patterns property |
| Artifact CRUD API | Direct git file read via `/api/git/file/` |
| Activity endpoint | Git log via `/api/feed/` |
| Auto-dispatch countdown | Watch mode + auto mode with cooldowns |

---

## Conventions

- All datetimes are UTC
- SSE events use `data: {json}\n\n` format with types: `text`, `result`, `error`, `session_id`, `dispatch`
- Agent git authors use format: `Name <id@maistro.local>`
- Commit messages authored by agents should be descriptive of what changed and why
- `.maistro/` directory is gitignored — contains only operational state
- Post-commit hook is installed automatically on project open
