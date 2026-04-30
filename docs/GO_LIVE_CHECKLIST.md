# Go-Live Checklist

Use this as the first real product launch checklist. Do not put secrets in Git.

## A. Local Cleanup Verification

- Run:

```bash
git status --short
git ls-files venv
git ls-files | grep -E '(^|/)__pycache__/|\.pyc$|(^|/)\.env$|(^|/)secrets/'
python -m pytest
cd frontend && npm run build
```

- Expected:
  - `git ls-files venv` prints nothing.
  - No tracked `__pycache__`, `.pyc`, `.env`, or `secrets/` files.
  - Tests pass, or failures are understood and documented before pushing.
  - Frontend build succeeds.

- Do not commit:
  - `.env`, `.envz`, or any real credential file.
  - `secrets/`.
  - `venv/`, `.venv/`, `node_modules/`, `frontend/dist/`.
  - `workspace/.sovereign/`, browser screenshots, logs, or local runtime artifacts.
  - `Chess Engine/` unless you intentionally split it into its own repo.

If you want to move the unrelated local project before the first push:

```powershell
Move-Item -LiteralPath "C:\Users\conno\project-sovereign\Chess Engine" -Destination "C:\Users\conno\Chess Engine"
```

## B. Create GitHub Repo

From `C:\Users\conno\project-sovereign`:

```bash
git status --short
git diff
git add .gitignore .env.example .github docs scripts app api core agents integrations memory prompts tests tools workers frontend requirements.txt pyproject.toml README.md AGENTS.md
git status --short
git commit -m "Prepare Sovereign real product workflow"
git branch -M main
git remote add origin git@github.com:<your-user-or-org>/project-sovereign.git
git push -u origin main
```

If `origin` already exists:

```bash
git remote -v
git remote set-url origin git@github.com:<your-user-or-org>/project-sovereign.git
git push -u origin main
```

## C. Connect Vercel

Create a Vercel project from the GitHub repo.

- Framework preset: Vite
- Root directory: `frontend`
- Install command: `npm ci`
- Build command: `npm run build`
- Output directory: `dist`
- Production branch: `main`

Set environment variable:

```text
VITE_SOVEREIGN_API_URL=https://your-backend-domain.example
```

Confirm:

- Vercel build succeeds.
- Dashboard loads.
- Sidebar shows the configured API base URL.
- If backend is offline, dashboard says `Mock fallback`.
- Once backend is online and CORS allows the Vercel origin, dashboard says `Live API`.

## D. Prepare VPS

On the VPS:

```bash
sudo adduser --system --group --home /opt/project-sovereign sovereign
sudo mkdir -p /opt/project-sovereign
sudo chown -R sovereign:sovereign /opt/project-sovereign
sudo -u sovereign git clone git@github.com:<your-user-or-org>/project-sovereign.git /opt/project-sovereign
cd /opt/project-sovereign
sudo -u sovereign python3 -m venv venv
sudo -u sovereign venv/bin/python -m pip install --upgrade pip
sudo -u sovereign venv/bin/python -m pip install -r requirements.txt
```

Create `/opt/project-sovereign/.env` on the VPS:

```bash
sudo -u sovereign nano /opt/project-sovereign/.env
```

Minimum useful values:

```text
APP_NAME=Project Sovereign
ENVIRONMENT=production
WORKSPACE_ROOT=/opt/project-sovereign/workspace
CORS_ALLOWED_ORIGINS=https://your-vercel-app.vercel.app
OPENROUTER_API_KEY=...
SLACK_BOT_TOKEN=...
SLACK_APP_TOKEN=...
SLACK_SIGNING_SECRET=...
REMINDERS_ENABLED=true
SCHEDULER_BACKEND=apscheduler
SCHEDULER_TIMEZONE=America/New_York
MEMORY_PROVIDER=local
```

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

Create `/etc/systemd/system/sovereign-worker.service`:

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

Start services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sovereign-backend.service sovereign-worker.service
sudo systemctl start sovereign-backend.service sovereign-worker.service
sudo systemctl status sovereign-backend.service --no-pager
sudo systemctl status sovereign-worker.service --no-pager
```

Check logs and health:

```bash
journalctl -u sovereign-backend.service -n 100 --no-pager
journalctl -u sovereign-worker.service -n 100 --no-pager
cd /opt/project-sovereign
venv/bin/python scripts/health_check.py --url http://127.0.0.1:8000/health
```

## E. Configure GitHub Actions Deploy

Add repository secrets:

```text
VPS_HOST
VPS_USER
VPS_SSH_KEY
VPS_PROJECT_PATH=/opt/project-sovereign
```

Confirm deploy:

```bash
git commit --allow-empty -m "Test backend deploy"
git push origin main
```

In GitHub Actions:

- `Backend Deploy` should run for backend/deploy path changes.
- The SSH step should run `scripts/deploy_backend.sh`.
- The VPS should pull the latest commit.
- Deployment tests should pass.
- systemd services should restart.
- Health check should pass.

## F. Manual Product Test

- Health:

```bash
python scripts/health_check.py --url https://your-backend-domain.example/health
```

- Slack:
  - DM the Sovereign app.
  - Confirm a reply.
  - Confirm logs show Slack entering the shared operator path.

- Dashboard:
  - Open Vercel dashboard.
  - Send a chat message.
  - Confirm `transport: "dashboard"` reaches `/chat`.
  - Confirm the reply shape matches `ChatResponse`.

- Memory:
  - Say: `Remember that I prefer concise answers.`
  - Then ask: `What do you remember about me?`
  - Confirm no secrets appear in memory summaries.

- Reminder:
  - Say: `Remind me in 2 minutes to check Sovereign deployment.`
  - Confirm scheduling and delivery behavior.

- Integration readiness:
  - Open dashboard settings/integrations.
  - Confirm disabled tools are labeled disabled or unavailable.
  - Confirm no tool claims success without real evidence.

## G. Fix/Test Loop

```bash
git pull --ff-only
# edit locally with Codex/Cursor/VS Code
python -m pytest
cd frontend && npm run build
cd ..
git status --short
git diff
git add <changed-source-files>
git commit -m "Fix live product issue"
git push origin main
```

Then watch:

- Vercel for frontend deploys.
- GitHub Actions for backend deploys.
- VPS logs for runtime errors.
- Slack and dashboard for live behavior.

## H. Rollback

Preferred:

```bash
git revert <bad_commit_sha>
git push origin main
```

If services need a manual nudge:

```bash
ssh <vps-user>@<vps-host>
cd /opt/project-sovereign
git pull --ff-only
bash scripts/deploy_backend.sh /opt/project-sovereign
sudo systemctl restart sovereign-backend.service sovereign-worker.service
```

Confirm:

```bash
venv/bin/python scripts/health_check.py --url http://127.0.0.1:8000/health
journalctl -u sovereign-backend.service -n 100 --no-pager
```
