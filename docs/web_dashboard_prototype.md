# Web Dashboard Prototype

## What Was Built

The `frontend/` directory now contains a Vite React TypeScript prototype for the Project Sovereign operator console. It is a UI shell only: the backend remains the source of truth, and no supervisor, agent, memory, credential, or integration logic was moved into the frontend.

The console includes:

- Main chat with the CEO/operator
- Active workstreams for planning, scheduling, coding, browser, and reviewer agents
- Life ops panels for reminders, calendar items, and task status
- Memory/project context cards with a visible secrets boundary
- Integration status cards for Slack, Google Calendar, Google Tasks, Gmail, Browser, Codex CLI, and Chroma
- Settings placeholders for model provider, autonomy, notifications, theme accent, and safety controls

## How To Run

From the repository root:

```powershell
cd frontend
npm install
npm run dev
```

Then open the local Vite URL, usually:

```text
http://127.0.0.1:5173
```

To build the prototype:

```powershell
cd frontend
npm run build
```

## Mock vs Real

Mock data powers the dashboard sections by default so the UI loads without Google, Slack, Gmail, browser, or memory credentials.

Real backend calls are limited to:

- `GET /health` to determine whether the backend is reachable
- `POST /chat` when a message is sent from the chat panel

If the backend is offline or unreachable, the console switches to a friendly mock/offline state and keeps the full UI usable.

## Expected Backend Endpoints

During local Vite development, the API client defaults to a `/api` proxy that targets the FastAPI backend at:

```text
http://127.0.0.1:8000
```

This avoids requiring CORS changes for the prototype. The target can be overridden with:

```text
VITE_SOVEREIGN_API_URL=http://127.0.0.1:8000
```

Expected endpoint shapes:

- `GET /health` returns `{ "status": "ok" }`
- `POST /chat` accepts `{ "message": "..." }`
- `POST /chat` returns the existing `ChatResponse` shape from `core.models`

The app is structured so future `/tasks`, `/memory`, `/integrations`, and `/runs` endpoints can replace the mock arrays without changing the visual shell.

## Production Next Steps

1. Add backend CORS or a Vite proxy for local browser-to-FastAPI calls.
2. Add read endpoints for active runs, agent lanes, reminders, calendar events, memory summaries, and integration readiness.
3. Replace mock data section by section with typed API fetchers.
4. Add authenticated access once a real deployment target exists.
5. Add loading, empty, and error states for each real data source.
6. Add a final verifier/evidence panel once backend run evidence is exposed.
7. Add focused frontend tests after the data contract stabilizes.
