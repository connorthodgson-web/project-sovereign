# Deployment

Project Sovereign should deploy as one product with separate runtimes:

- GitHub is the source of truth.
- Vercel hosts the `frontend/` dashboard.
- A VPS runs the always-on Python backend and Slack Socket Mode worker.
- Slack, dashboard chat, and future iOS clients call the same backend/operator transport.

## Backend Runtime

Primary API process:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Slack Socket Mode process:

```bash
python -m app.slack_main
```

The API exposes:

- `GET /health`
- `POST /chat`
- dashboard read-only endpoints under the root path, such as `/agents/status` and `/memory/summary`

## Required Backend Environment

Set these on the VPS, not in git:

```bash
OPENROUTER_API_KEY=
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_SIGNING_SECRET=
REMINDERS_ENABLED=true
SCHEDULER_BACKEND=apscheduler
SCHEDULER_TIMEZONE=America/New_York
CORS_ALLOWED_ORIGINS=https://your-vercel-app.vercel.app
```

Optional integrations remain disabled until configured:

```bash
BROWSER_ENABLED=false
BROWSER_USE_ENABLED=false
GMAIL_ENABLED=false
GOOGLE_CALENDAR_ENABLED=false
GOOGLE_TASKS_ENABLED=false
MEMORY_PROVIDER=local
```

## VPS Systemd Service

Create `/etc/systemd/system/sovereign-backend.service`:

```ini
[Unit]
Description=Project Sovereign Backend API
After=network.target

[Service]
Type=simple
User=sovereign
WorkingDirectory=/opt/project-sovereign
EnvironmentFile=/opt/project-sovereign/.env
ExecStart=/opt/project-sovereign/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Optional Slack worker service at `/etc/systemd/system/sovereign-worker.service`:

```ini
[Unit]
Description=Project Sovereign Slack Worker
After=network.target sovereign-backend.service

[Service]
Type=simple
User=sovereign
WorkingDirectory=/opt/project-sovereign
EnvironmentFile=/opt/project-sovereign/.env
ExecStart=/opt/project-sovereign/venv/bin/python -m app.slack_main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sovereign-backend.service
sudo systemctl enable sovereign-worker.service
sudo systemctl start sovereign-backend.service
sudo systemctl start sovereign-worker.service
```

## Backend Deploy Script

The deploy script is `scripts/deploy_backend.sh`.

On the VPS:

```bash
cd /opt/project-sovereign
bash scripts/deploy_backend.sh
```

Useful environment overrides:

```bash
VPS_PROJECT_PATH=/opt/project-sovereign
SOVEREIGN_BACKEND_SERVICE=sovereign-backend.service
SOVEREIGN_WORKER_SERVICE=sovereign-worker.service
SOVEREIGN_HEALTH_URL=http://127.0.0.1:8000/health
SOVEREIGN_DEPLOY_TEST_COMMAND="python -m pytest tests/test_shared_transport.py tests/test_slack_interface.py tests/test_operator_console_api.py tests/test_operator_loop.py"
```

## GitHub Actions Backend Deploy

The backend deploy workflow expects these GitHub repository secrets:

```text
VPS_HOST
VPS_USER
VPS_SSH_KEY
VPS_PROJECT_PATH
```

The workflow SSHes into the VPS and runs:

```bash
bash scripts/deploy_backend.sh "$VPS_PROJECT_PATH"
```

Do not store deployment keys, API keys, Slack tokens, or provider credentials in this repository.

On success, the workflow means the VPS pulled `main`, installed dependencies, ran the deploy test command, restarted the backend and worker services, and passed the health check. On failure, read the GitHub Actions log first, then inspect VPS service logs:

```bash
journalctl -u sovereign-backend.service -n 100 --no-pager
journalctl -u sovereign-worker.service -n 100 --no-pager
```

## Frontend on Vercel

Vercel settings:

- Framework preset: Vite
- Root directory: `frontend`
- Install command: `npm ci`
- Build command: `npm run build`
- Output directory: `dist`

Set this Vercel environment variable:

```text
VITE_SOVEREIGN_API_URL=https://your-backend-domain.example
```

For local frontend development, Vite proxies `/api` to `http://127.0.0.1:8000`.
For deployed frontend calls, the backend `CORS_ALLOWED_ORIGINS` value must include the Vercel app origin.

Vercel preview deployments are useful for branch or pull-request UI checks. Production deployments should come from `main`, and the production `VITE_SOVEREIGN_API_URL` should point at the live VPS backend domain.

## Shared Chat Transport

Dashboard chat posts to:

```http
POST /chat
```

with:

```json
{
  "message": "Give Sovereign a goal",
  "transport": "dashboard"
}
```

Slack DMs use the Slack transport adapter and enter the same shared backend path. The common flow is:

```text
transport message
-> core.transport.OperatorMessage
-> shared normalization/context binding
-> core.supervisor.supervisor.handle_user_goal(...)
-> ChatResponse
-> transport-specific formatting
```

Future iOS should call the same `/chat` endpoint with `transport: "ios"` or use a thin authenticated mobile endpoint that creates the same `OperatorMessage`.

The dashboard may show offline/mock fallback when the backend is unreachable, but that state must remain visibly labeled. Mock data is a UI fallback, not a replacement CEO/operator.

## Production Blockers Before Public Exposure

- Add an auth layer for dashboard/API access.
- Configure CORS for the Vercel domain only.
- Keep `venv/`, `__pycache__`, local browser artifacts, `.env`, `.envz`, and `secrets/` out of Git.
- Move `Chess Engine/` outside this repo before first GitHub push or keep it ignored as unrelated local material.
- Decide whether memory should remain local or move to Supabase/Zep for always-on operation.
