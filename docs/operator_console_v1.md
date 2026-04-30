# Operator Console v1

Project Sovereign's AI Hub / Operator Console v1 is a frontend dashboard for the main CEO-style operator, agent workstreams, browser evidence, life operations, memory, and integration readiness.

## What is live

- The CEO chat posts to the backend `/chat` endpoint when the API is reachable.
- The dashboard reads safe backend summaries from:
  - `/health`
  - `/agents/status`
  - `/runs/status`
  - `/integrations/status`
  - `/memory/summary`
  - `/life/reminders`
  - `/life/calendar`
  - `/life/tasks`
  - `/browser/status`
  - `/browser/artifacts`
- Agent cards are derived from the standing agent catalog, in-memory task state, recent agent results, and integration readiness.
- Integration cards use backend readiness snapshots and do not expose raw credentials, token paths, provider payloads, or stack traces.
- Memory panels show safe counts, safe fact previews, recent actions, and open loops. Secret-like values and contact-like values are filtered.
- Browser status shows latest safe browser evidence from task results and safe screenshot artifact metadata from the workspace.

## What is mock

- When the backend is offline, the frontend falls back to mock data and labels it as mock.
- The mock data lives in `frontend/src/data/mockData.ts`.
- Live browser streaming is not implemented. The browser workspace includes a live-ready placeholder and explicitly reports streaming as unavailable.
- Model/provider controls are readiness placeholders only. The frontend does not edit provider settings or secrets.
- The dashboard does not create reminders, calendar events, tasks, browser runs, or integrations from the UI yet.

## Backend endpoints added

All new console endpoints are read-only:

- `GET /agents/status`: standing agent cards with status, last action, evidence count, and safe blocker text.
- `GET /runs/status`: recent task/run summaries plus agent cards.
- `GET /integrations/status`: readiness summary for model/provider, search, browser, Slack, Gmail, Calendar, Tasks, Codex, and related integrations.
- `GET /memory/summary`: safe memory provider/counts/facts/actions/open loops.
- `GET /life/reminders`: reminder records plus scheduler health.
- `GET /life/calendar`: calendar readiness, and today's events only when the calendar provider is live.
- `GET /life/tasks`: operator task summaries plus Google Tasks only when the provider is live.
- `GET /browser/status`: latest browser status, blocker, evidence summary, screenshot metadata, and future streaming marker.
- `GET /browser/artifacts`: recent safe browser screenshot artifact metadata.

Safety boundary:

- No raw API keys, secrets, refresh tokens, credential paths, token paths, emails, or long secret-like strings should be returned.
- Provider exceptions are collapsed into safe blocker summaries.
- Screenshot artifacts are exposed only when paths resolve under `settings.workspace_root` and are image files.

## Run backend

From the repo root:

```powershell
venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## Run frontend

From `frontend`:

```powershell
npm install
npm run dev
```

The Vite dev server uses `/api` as a proxy to `http://127.0.0.1:8000`.

For production-style local build:

```powershell
npm run lint
npm run build
```

Optional environment override:

```powershell
$env:VITE_SOVEREIGN_API_URL="http://127.0.0.1:8000"
```

## Vercel deployment notes

- Do not deploy secrets to the frontend.
- Set `VITE_SOVEREIGN_API_URL` to the deployed backend URL when the backend is hosted separately.
- The backend must allow the frontend origin with CORS before a hosted Vercel UI can call it directly.
- Keep `/chat` and the console read-only endpoints behind the same API contract; the frontend should not duplicate agent routing or orchestration logic.
- Browser screenshots should remain backend/workspace artifacts. Do not upload or serve them publicly without an explicit artifact access policy.

## Future live browser streaming plan

1. Add a backend browser session model that distinguishes idle, running, blocked, completed, and expired sessions.
2. Emit browser session events with a safe schema: current URL, title, step summary, blocker state, artifact IDs, and sanitized screenshots.
3. Add a streaming transport such as WebSocket or Server-Sent Events for live browser progress.
4. Add a protected artifact preview endpoint that serves only approved workspace browser artifacts by ID, not arbitrary paths.
5. Keep human-in-loop states explicit for login, CAPTCHA, 2FA, payment, and sensitive forms.
6. Keep final completion gated by evidence and reviewer/verifier checks rather than visual streaming alone.
