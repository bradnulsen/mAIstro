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
| `persona` | string | `''` | System prompt / identity — what this agent does and how it thinks |
| `base_tools` | JSON array | `[]` | Allowed CLI tools (Read, Glob, Grep, Edit, Write, Bash, etc.) |
| `mcp_servers` | JSON array | `[]` | Scoped MCP servers (controls access to queue_agent, subscribe_to, etc.) |
| `subscriptions` | JSON array | `[]` | Glob patterns for files this agent watches and receives as context |
| `mode` | string | `'manual'` | Dispatch mode: `manual`, `auto`, or `watch` |
| `cooldown_seconds` | int | `30` | Minimum time between triggered dispatches |
| `running` | boolean | `false` | Is a dispatch currently in-flight |
| `sort_order` | int | `0` | UI ordering |

Agent IDs are slugified from names.

**Subscriptions** serve two purposes: they define what file changes trigger the agent (in watch mode), and they define what file contents are included in the agent's context (in all modes). Agents can modify their own subscriptions at runtime via MCP tool. The human can see and override these in the config UI.

### Control Model

The human controls agent behavior through two mechanisms:

1. **Tool permissions** — Which MCP servers and tools an agent can access. An agent without `maistro-dispatch` cannot queue other agents or modify its own subscriptions. This is the primary blast radius control.

2. **Dispatch mode** — Manual/Watch/Auto determines when the agent runs. Flipping any agent to Manual immediately stops autonomous triggering.

Cooldown is a rate limiter, not an intelligence constraint. Agents are autonomous workers — the human controls their capabilities, not their decision-making.

### Dispatch Modes

| Mode | Behavior |
|------|----------|
| **Manual** | Agent runs only when a human dispatches it via the UI |
| **Watch** | Agent runs when a commit touches files matching its `subscriptions`. Subject to cooldown. |
| **Auto** | Agent runs on a polling interval, reads current project state, decides what to do. Subject to cooldown. |

All three modes produce the same execution: agent reads project state, does work, commits results. The trigger mechanism varies but the execution model is identical.

---

## Project Knowledge Model

Agents maintain project knowledge as files in the git repository. There is no message board or post/comment system. The filesystem is the collaboration surface.

### Documentation Is Truth

The system enforces one principle via the platform preamble (injected into every agent's prompt, not configurable):

> Documentation is the source of truth. Your files should be first-principle, as-is representations of current project state — not task lists, not work-in-progress notes. Any reasoning, context, or work management belongs in commit messages, not in documentation. Keep your documentation current, accurate, and useful to anyone reading it cold.

Agents decide for themselves what files to create and maintain based on their persona. The QA agent might maintain `qa/bugs.md` and `qa/test-status.md`. The Architect might maintain `architecture/system-design.md`. The system doesn't prescribe filenames, directories, or structure — the persona does.

What the system *does* know is each agent's **subscriptions** — the files it watches and receives as context. This is the wiring between agents.

### Artifacts in the UI

The frontend discovers an agent's authored files from git history (who committed what). This requires no configuration — git already knows. The agent view shows:

- **Authored files** — files this agent has committed to, derived from `git log --author`. These are the agent's outputs. Editable by the human (edit + save = human commit).
- **Subscriptions** — files this agent watches, from the `subscriptions` property. Shows what drives this agent.

### Cross-Agent Coordination

Two mechanisms:

1. **Pull (watch mode)** — A subscribed file changes → agent is triggered → reads current state → does work → commits → may trigger downstream agents.

2. **Push (queue tool)** — Agent explicitly queues another agent via MCP tool with a reason and handoff context.

Both are available simultaneously. Watch handles routine data flow. Queue handles the exceptions.

### Human as Worker

A human editing any file and committing is indistinguishable from an agent doing the same. The commit shows up in the feed, may trigger agents via their subscriptions. The human is just another worker in the system, using the same surface.

### Convention (Example)

A typical project might look like this, but the structure is emergent from agent personas, not prescribed by the system:

```
project/
├── intent.md                  # Human-authored project intent
├── architecture/
│   └── system-design.md       # Maintained by Architect
├── qa/
│   ├── bugs.md                # Maintained by QA
│   └── test-status.md
├── plan/
│   └── tasks.md               # Maintained by Planner
├── src/                       # Code — maintained by Worker
│   └── ...
└── .maistro/
    └── maistro.db             # Operational state (gitignored)
```

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
| `context` | string | Handoff context — varies by trigger type (see below) |
| `created_at` | datetime | When queued |
| `started_at` | datetime | When dispatch began (null = pending) |
| `completed_at` | datetime | When dispatch finished (null = in progress) |
| `result_commit` | string | Commit hash produced by this dispatch (if any) |
| `error` | string | Error message if dispatch failed |

### Queue Context

Every dispatch carries context that bootstraps the agent into its task. The source varies by trigger:

| Trigger | Context source |
|---------|---------------|
| `commit` | Commit metadata: hash, author, message, changed files |
| `agent_queue` | Queuing agent's reason + freeform context (the handoff) |
| `manual` | Human's instructions |
| `auto` | None — agent reads current state fresh |

When the dispatch executes, the context is injected into the prompt assembly so the agent knows *why* it was triggered and *what to focus on* without having to rediscover everything from scratch.

### Trigger Mechanisms

#### 1. Commit-Triggered (Watch)

A post-commit hook runs after every commit:

1. Get list of changed files from the commit
2. Query all agents where `mode = 'watch'` and `running = false`
3. For each agent, check if any changed file matches its `subscriptions` patterns
4. For matching agents, check cooldown (last dispatch within `cooldown_seconds`?)
5. Queue matching agents that pass cooldown, with commit metadata as context

The post-commit hook is a generic script that calls a backend endpoint. All logic lives in the backend.

#### 2. Agent-Queued (MCP Tool)

Agents can queue other agents via an MCP tool:

```
queue_agent(agent_id, reason, context?)
```

`reason` is a short string for the UI and dispatch log. `context` is the substantive handoff — the queuing agent's findings, pointers to specific files, description of the problem. This gets injected into the target agent's prompt, giving it a running start from where the previous agent left off.

This inserts a dispatch queue row with `trigger = 'agent_queue'`. Only available to agents whose MCP server config includes `maistro-dispatch`. The tool itself can be scoped to limit which target agents are queueable.

#### 3. Manual (Human)

Human clicks "Run" on an agent in the UI, optionally with instructions. Instructions are stored as the dispatch context. Inserts a dispatch queue row with `trigger = 'manual'`. Manual dispatch ignores cooldowns.

#### 4. Auto (Polling)

Background loop checks for agents with `mode = 'auto'` on a configurable interval. If the agent isn't running and cooldown has elapsed, queue it with `trigger = 'auto'`. No context — the agent reads current state fresh.

### Dispatch Execution

When a dispatch queue item is processed:

1. Set `started_at`, set agent `running = true`
2. Build system prompt: identity preamble → persona → artifact manifest → list of available tools
3. Build context prompt: agent's input and output artifact contents, recent git log
4. Invoke LLM via Claude CLI subprocess with MCP servers
5. Stream response via SSE to frontend
6. Agent reads files, uses tools, produces output
7. Any file changes are committed with a descriptive message authored by the agent
8. Set `completed_at`, set agent `running = false`
9. Post-commit hook fires for any new commits, potentially triggering downstream agents

### Prompt Assembly

System prompt layers:
1. **Platform preamble** (invariant, not configurable) — Agent identity, the documentation-as-truth principle, commit message conventions
2. **Persona** — The agent's user-configured identity and role
3. **Agent registry** — List of all agents, their personas (summary), and their subscriptions. Enables discovery and prevents duplication.
4. **Subscription contents** — Current contents of files matching this agent's `subscriptions` (its context)
5. **Recent activity** — Recent git log entries so the agent knows what's happened
6. **Queue context** — Why this dispatch was triggered. For agent-queued: the handoff from the previous agent. For commit-triggered: the commit metadata. For manual: the human's instructions.

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
| `queue_agent` | Queue another agent to run, with a reason and optional handoff context |
| `get_dispatch_status` | Check if an agent is currently running |
| `get_recent_activity` | Get recent dispatch history |
| `get_agent_registry` | Returns all agents with their personas and subscriptions |
| `subscribe_to` | Add a glob pattern to this agent's `subscriptions` (self-subscription) |
| `unsubscribe_from` | Remove a glob pattern from this agent's `subscriptions` |

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

The agent view shows what the agent has done and what it watches. Agent list on left, detail on right.

**Selecting an agent shows:**

**Artifacts panel** (primary):
- **Authored files** — files this agent has committed to, derived from git history. Editable inline. Each file shows: path, last commit message snippet, last modified time. Click to open inline viewer/editor. Edit + save = git commit attributed to the human.
- **Subscriptions** — file patterns this agent watches and receives as context. Shows resolved files with last commit info. Useful for understanding what drives this agent.
- File git history accessible per-artifact (commit log filtered to that path)

**Config panel** (secondary, collapsible):
- Identity (name, persona/system prompt)
- Model selection
- Tools (CLI tools + MCP servers)
- Subscriptions (glob pattern editor)
- Dispatch mode (Manual / Watch / Auto toggle)
- Guardrails (cooldown, timeout)

**Actions:**
- **Run** — dispatch the agent immediately
- **Chat** — open a chat session with this agent

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
    context TEXT,          -- handoff context: commit metadata, agent reason+context, human instructions, or null
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
| POST | `/api/project/browse` | OS-native directory picker (requires Electron/Tauri shell) |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents/` | List all agents with composed properties |
| POST | `/api/agents/` | Create agent |
| GET | `/api/agents/{id}` | Get agent with properties |
| PATCH | `/api/agents/{id}` | Update agent properties |
| DELETE | `/api/agents/{id}` | Delete agent |
| POST | `/api/agents/reorder` | Reorder agent list |
| GET | `/api/agents/{id}/artifacts` | Authored files (from git log --author) + resolved subscription files, with metadata |

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