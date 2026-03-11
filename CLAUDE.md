# mAistro v2

Intent-to-reality development engine driven by LLM agents coordinating through git.

## Quick Start

```bash
# Backend
pip install -r requirements.txt
python run.py

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Backend runs on http://localhost:8420, frontend on http://localhost:5173.

## Architecture

- **Backend**: Python + FastAPI + SQLite (aiosqlite, WAL mode)
- **Frontend**: React + Vite
- **LLM**: Claude CLI subprocess with NDJSON streaming
- **Realtime**: SSE (Server-Sent Events)

## Key Files

- `backend/main.py` — FastAPI app, all API routes
- `backend/database.py` — SQLite schema, CRUD helpers
- `backend/dispatch.py` — Claude CLI invocation, prompt assembly, dispatch engine
- `frontend/src/App.jsx` — Shell, project opener, view router
- `frontend/src/components/Feed.jsx` — Git activity feed
- `frontend/src/components/Agents.jsx` — Agent list + config + dispatch
- `frontend/src/components/Chat.jsx` — Agent chat interface
- `frontend/src/api.js` — API client with SSE helper

## Conventions

- All project truth lives in git. Database holds only operational state.
- `.maistro/` dir is gitignored — contains maistro.db
- Agent IDs are slugified from names
- Backend port: 8420, Frontend port: 5173
- SSE events: `text`, `result`, `error`, `session_id`, `dispatch`, `tool_use`
