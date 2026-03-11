# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start

```bash
# Backend (from repo root)
pip install -r requirements.txt
python run.py

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Backend runs on http://localhost:8420 (uvicorn with `--reload`), frontend on http://localhost:5173. No test suite or linter is configured.

## Architecture

**mAistro v2** — an LLM agent orchestrator where multiple Claude-powered agents coordinate through git commits in a target project directory.

### Stack
- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode) + sse-starlette
- **Frontend**: React 19 + Vite 6 (no TypeScript, no state library)
- **LLM**: Claude CLI invoked as subprocess with `--output-format stream-json` (NDJSON streaming)
- **Realtime**: Server-Sent Events (SSE) for dispatch streaming and chat

### Data Flow
1. User opens a target project directory via the UI → backend initializes `.maistro/maistro.db` inside it and installs a git post-commit hook
2. Agents are configured with personas, subscription glob patterns (files they watch/receive as context), and a model
3. **Dispatch** (manual, watch-triggered, or auto): backend builds system+user prompts from agent config + subscription contents + git log, spawns `claude` CLI as a subprocess, streams NDJSON events back as SSE
4. After CLI completes, any file changes are auto-committed with the agent as git author (`<agent-id>@maistro.local`)
5. Post-commit hook notifies backend → watch-mode agents whose subscriptions match changed files get triggered (with cooldown)

### Key Design Decisions
- **Git as source of truth**: all project content lives in git. The SQLite DB (`.maistro/` dir, gitignored) holds only operational state — agent configs, dispatch queue, chat sessions
- **Two SQLite databases**: project DB at `<project>/.maistro/maistro.db` (async via aiosqlite) holds agent/dispatch/chat state; app DB at `<repo>/.maistro/app.db` (sync, `appstate.py`) holds recent-projects list
- **Agent properties are EAV**: `agent_property_defs` table defines keys with defaults and types; `agent_properties` stores per-agent overrides. Types: `string`, `json`, `integer`, `boolean`
- **Claude CLI via Popen+threads**: `cli.py` uses `subprocess.Popen` with thread readers pushing to `asyncio.Queue` (avoids Windows ProactorEventLoop issues). Pipes prompt and system prompt via stdin to avoid cmd arg quoting issues. Reads NDJSON from both stdout and stderr (CLI writes to stderr on `--resume`)
- **Vite proxies `/api` to backend**: frontend makes API calls to same origin, Vite dev server proxies to port 8420

## Key Files

- `run.py` — Uvicorn launcher (hot-reload on `backend/`)
- `backend/main.py` — FastAPI app, all API routes, global `PROJECT_DIR` state, CORS, lifespan
- `backend/database.py` — Project SQLite schema, EAV property system, all CRUD helpers (async)
- `backend/appstate.py` — App-level SQLite DB for recent-projects list (sync, separate from project DB)
- `backend/git.py` — Git subprocess abstraction (log, diff, commit, authored_files, hook installer)
- `backend/cli.py` — Claude CLI subprocess invocation: stdin piping, NDJSON parsing, event schema
- `backend/dispatch.py` — Prompt assembly (`build_system_prompt`/`build_user_prompt`), dispatch lifecycle, watch trigger matching, agent commit logic
- `frontend/src/App.jsx` — Shell with rail navigation, project opener, view router
- `frontend/src/api.js` — API client with `fetchJSON` and `fetchSSE` helpers
- `frontend/src/components/` — `Feed.jsx` (git activity), `Agents.jsx` (config + dispatch), `Chat.jsx` (chat interface)
- `maistro_spec.md` — Detailed functional spec (authoritative reference for intended behavior)

## Conventions

- Agent IDs are slugified from names (see `database.slugify`)
- Agent commit authorship: `<AgentName> <<agent-id>@maistro.local>`
- SSE event types: `text`, `result`, `error`, `session_id`, `dispatch`, `tool_use`
- Dispatch triggers: `manual`, `commit`, `agent_queue`, `auto`
- Agent modes: `manual` (on demand), `watch` (triggered by commits matching subscriptions), `auto` (polling)
- Agents don't configure output files — authored files are derived from `git log --author`
- Subscriptions serve dual purpose: trigger matching (watch mode) and context injection (all modes)
- dispatch_queue.context column stores trigger-specific data (human instructions, agent handoff, commit metadata)
- Backend port: 8420, Frontend port: 5173