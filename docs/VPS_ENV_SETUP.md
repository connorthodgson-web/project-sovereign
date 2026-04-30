# VPS Environment Setup

`env/vps.env.template` is a safe template for the live VPS environment. It contains blank secret values and conservative integration flags. The real file lives only on the VPS at `/opt/project-sovereign/.env`.

Never commit real `.env` files, provider keys, Slack tokens, OAuth token files, or browser worker shared secrets. Real secrets belong only in the VPS `.env`, GitHub repository secrets, Vercel environment variables, or ignored files under `/opt/project-sovereign/secrets/`.

Enable integrations one at a time after testing. The first production test should focus on the CEO/backend, Slack worker, dashboard CORS, reminders, and local memory.

## 1. Open The VPS Env

On the VPS:

```bash
nano /opt/project-sovereign/.env
```

## 2. Paste The Template

Paste the contents of:

```text
env/vps.env.template
```

into:

```text
/opt/project-sovereign/.env
```

This is a template, not a secret file. Fill values directly on the VPS.

## 3. Fill Only The Initial Required Values

For the first production testing pass, fill only:

- OpenAI/OpenRouter keys needed by the active model path.
- Slack keys: `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, and `SLACK_APP_TOKEN` as available.
- Vercel CORS origin: `CORS_ALLOWED_ORIGINS=https://YOUR_REAL_VERCEL_URL`.
- Reminder config: keep `REMINDERS_ENABLED=true`, `SCHEDULER_BACKEND=apscheduler`, and `SCHEDULER_TIMEZONE=America/New_York`.

Do not paste Windows `C:\...` paths into the VPS env. Linux paths must use `/opt/project-sovereign/...`.

## 4. Keep Risky Integrations Disabled First

Keep these disabled until they are proven one at a time:

- Browser: keep `BROWSER_ENABLED=false` and `BROWSER_USE_ENABLED=false` until VPS browser execution is explicitly tested. Preferred future design is a home-computer browser worker.
- Codex CLI: keep `CODEX_CLI_ENABLED=false` until Codex CLI is installed and authenticated on the VPS.
- Google Calendar, Gmail, and Google Tasks: keep disabled until credential and token files exist under `/opt/project-sovereign/secrets/`.
- Zep: keep `MEMORY_PROVIDER=local` and `MEMORY_BACKEND=local` until `ZEP_API_KEY` and `ZEP_BASE_URL` are configured.
- Search: keep `SEARCH_ENABLED=false` and `WEB_SEARCH_ENABLED=false` until a search provider and key are configured.

## 5. Restart Services

After saving `/opt/project-sovereign/.env`:

```bash
systemctl restart sovereign-backend.service
systemctl restart sovereign-worker.service
```

## 6. Check Services And Health

```bash
systemctl status sovereign-backend.service
systemctl status sovereign-worker.service
python /opt/project-sovereign/scripts/health_check.py --url http://127.0.0.1:8000/health
```

Expected health output includes:

```text
healthy: http://127.0.0.1:8000/health
```

## 7. Then Test The Product

After both services are healthy:

- Vercel dashboard chat: confirm it reaches `POST /chat`.
- Slack DM: confirm the Slack worker replies in a direct message.
- Reminder creation: send `Remind me in 2 minutes to check Sovereign deployment.`
- Memory recall: ask Sovereign to remember a harmless preference, then ask what it remembers.

## 8. Vercel Environment Variable

In Vercel, set:

```text
VITE_SOVEREIGN_API_URL=http://187.124.213.208:8000
```

Later, replace that with the HTTPS backend domain when TLS/reverse proxy is ready.

The VPS env should use the real Vercel app origin for CORS:

```text
CORS_ALLOWED_ORIGINS=https://YOUR_REAL_VERCEL_URL
```

Do not leave `CORS_ALLOWED_ORIGINS=*` for production testing.
