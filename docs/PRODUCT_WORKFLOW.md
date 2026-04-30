# Product Workflow

Project Sovereign's real product workflow is:

```text
local edit
-> local test
-> reviewed commit
-> push to GitHub
-> Vercel deploys frontend
-> GitHub Actions deploys backend to VPS
-> Slack and dashboard talk to the same CEO/operator
-> manual product test
-> fix locally and push again
```

GitHub is the source of truth at `connorthodgson-web/project-sovereign`. Codex, Cursor, and VS Code may edit files locally, but they should not create uncontrolled commits or push secrets. The safe path is: edit, inspect the diff, run tests, commit, push, and let the configured deployment systems update the live product.

## Intended Workflow

### 1. Local Development

Develop from this repository on a local machine:

```bash
python -m pytest
cd frontend
npm run build
```

Use `.env` only for local secrets. Keep `.env`, `secrets/`, virtual environments, browser artifacts, logs, and generated files out of Git.

### 2. Commit And Push

After reviewing changes:

```bash
git status --short
git diff
git add <files>
git commit -m "Describe the product change"
git push origin main
```

Codex can prepare commands, but the commit/push should remain a deliberate reviewed step.

### 3. GitHub Source Of Truth

The `main` branch should contain only product source, docs, tests, deployment scripts, workflow files, and safe examples. Runtime state, credentials, dependency folders, generated browser artifacts, and unrelated local projects should not be added.

### 4. Frontend Deployment

Vercel owns the dashboard deployment from `frontend/`.

- Framework preset: Vite
- Root directory: `frontend`
- Install command: `npm ci`
- Build command: `npm run build`
- Output directory: `dist`
- Required env var for current VPS testing: `VITE_SOVEREIGN_API_URL=http://187.124.213.208:8000`
- Future HTTPS value: `VITE_SOVEREIGN_API_URL=https://your-backend-domain.example`

Preview deployments come from branches or pull requests. Production deployments come from `main`.

### 5. Backend Deployment

GitHub Actions owns backend deploys to the VPS. On `main` pushes affecting Python/backend/deploy files, `.github/workflows/backend-deploy.yml` SSHes into the VPS and runs:

```bash
bash "$VPS_PROJECT_PATH/scripts/deploy_backend.sh" "$VPS_PROJECT_PATH"
```

The script pulls the latest code, creates or reuses `venv`, installs dependencies, runs deployment tests, restarts systemd services, and checks `/health`.

### 6. VPS Runtime

The VPS runs the always-on product backend:

- API/CEO backend: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Slack worker: `python -m app.slack_main`
- Scheduler/reminder runtime: enabled inside the backend/worker configuration through `.env`
- Tool integrations: configured through VPS `.env` and files under `secrets/`, never through Git

The VPS should not be treated as the first heavy browser runtime. Browser-use and Playwright should prefer a future home-computer remote worker, while the VPS remains the always-on CEO/backend and lightweight execution host.

### 7. Shared Operator Path

All clients should be thin transports over one CEO/operator path:

```text
client message
-> /chat or shared inbound adapter
-> core.transport.handle_operator_message(...)
-> supervisor.handle_user_goal(...)
-> ChatResponse
-> client-specific formatting
```

Current clients:

- Dashboard sends `transport: "dashboard"` to `POST /chat`.
- Slack sends `transport: "slack"` through `SlackOperatorBridge`.
- Future iOS should call `POST /chat` with `transport: "ios"` or use a small authenticated mobile endpoint that creates the same `OperatorMessage`.

Dashboard mock/offline mode is allowed only as a labeled fallback. It must not replace the live backend path or pretend to be live AI.

### 8. Manual Testing Loop

After deploy:

1. Check backend health.
2. Send a Slack DM.
3. Send a dashboard chat message.
4. Confirm both reach the same operator path.
5. Test memory, reminders, integration readiness, and browser evidence honesty.
6. Fix bugs locally.
7. Re-run tests.
8. Commit and push.
9. Watch Vercel and GitHub Actions deploy.
10. Retest live.

### 9. Rollback And Recovery

Preferred rollback:

```bash
git revert <bad_commit_sha>
git push origin main
```

This triggers the same frontend/backend deployment paths. If the VPS is unhealthy, SSH in and restart services:

```bash
sudo systemctl restart sovereign-backend.service
sudo systemctl restart sovereign-worker.service
journalctl -u sovereign-backend.service -n 100 --no-pager
journalctl -u sovereign-worker.service -n 100 --no-pager
```

## Current Repo State Compared To Target

### Already Works

- Root-level backend packages are suitable for the current VPS entrypoints.
- `POST /chat` routes through `core.transport.handle_operator_message`.
- Slack uses the shared transport path with `transport="slack"`.
- Dashboard sends `transport: "dashboard"` to `/chat`.
- `ChatRequest` and `OperatorMessage` already accept future `transport="ios"`.
- Vite frontend is ready for Vercel-style builds.
- Backend deploy and frontend check workflows exist.
- `scripts/deploy_backend.sh` and `scripts/health_check.py` exist.
- Dashboard mock data is visibly labeled as mock fallback.
- GitHub repo is live at `connorthodgson-web/project-sovereign`.
- Vercel frontend is deployed.
- VPS clone exists at `/opt/project-sovereign`.
- Backend systemd service is running.
- Slack worker systemd service is created.
- GitHub Actions backend auto-deploy is working with a green check.

### Requires Live Configuration Or Manual Testing

- VPS `.env` with provider keys, Slack tokens, CORS origin, and integration flags.
- Vercel `VITE_SOVEREIGN_API_URL=http://187.124.213.208:8000`, or the future HTTPS backend domain.
- Manual dashboard chat, Slack DM, reminders, and memory recall testing.
- DNS/TLS/reverse proxy for a production backend domain.

### Missing Or Future Work

- Public dashboard/API auth.
- Durable production memory choice, such as Supabase or Zep.
- iOS client implementation.
- Live browser stream in the dashboard.
- Remote home-computer browser worker.
- More complete integration setup UIs.
- Secret manager integration beyond filesystem/env conventions.

### Blocked By Repo Clutter

- Tracked `venv/` files must be removed from Git tracking.
- Tracked `__pycache__` and `.pyc` files must be removed from Git tracking.
- Tracked local runtime memory/browser artifacts must be removed from Git tracking.
- `.envz` was tracked even though it is local environment material.
- `Chess Engine/` is unrelated and should not be added to the Sovereign repo.

### Requires Manual User Action

- Review the final diff before first commit.
- Move `Chess Engine/` outside the repo or keep it ignored.
- Create GitHub repo and push.
- Configure Vercel.
- Prepare VPS services and `.env`.
- Add GitHub Actions secrets.
- Run first live manual product test.
