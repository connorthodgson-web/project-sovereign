# VPS Deployment Prep

This document is preparation only. It does not deploy Project Sovereign, provision infrastructure, or claim production readiness.

## Runtime Shape

Expected VPS services:

- FastAPI API service: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- Slack Socket Mode worker: `python -m app.slack_main` or the installed `sovereign-slack` console script.
- Scheduler/reminder runtime: currently starts inside `app.slack_main` through `reminder_scheduler_service.start()`.
- Optional browser runtime dependencies: Playwright browser binaries and Browser Use SDK configuration.
- Optional frontend service or static build host once the dashboard becomes a deploy target.

For production, run API and worker as separate supervised processes, for example separate `systemd` services. Do not depend on an interactive shell session.

## Environment Variables

Core application:

- `APP_NAME`
- `ENVIRONMENT`
- `API_PREFIX`
- `WORKSPACE_ROOT`

LLM/model routing:

- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`
- `OPENROUTER_MODEL_TIER1`
- `OPENROUTER_MODEL_TIER2`
- `OPENROUTER_MODEL_TIER3`
- `MODEL_ROUTING_ENABLED`
- `MODEL_DEFAULT_TIER`
- `MODEL_ESCALATION_ENABLED`
- Optional frontier/OpenAI/managed-agent variables from `.env.example`.

Slack:

- `SLACK_ENABLED`
- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`
- `SLACK_SIGNING_SECRET`
- Optional OAuth values: `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`.

Memory and retrieval:

- `MEMORY_PROVIDER`
- `MEMORY_BACKEND`
- `WORKSPACE_ROOT`
- Optional: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`.
- Optional: `ZEP_API_KEY`, `ZEP_BASE_URL`, `ZEP_USER_ID`, `ZEP_THREAD_ID`.
- Optional semantic retrieval: `SEMANTIC_RETRIEVAL_ENABLED`, `RETRIEVAL_BACKEND`, `RETRIEVAL_URL`, `RETRIEVAL_API_KEY`, `EMBEDDINGS_MODEL`, `CHROMA_PATH`, `CHROMA_COLLECTION_NAME`.

Search:

- `SEARCH_ENABLED`
- `SEARCH_PROVIDER`
- `SEARCH_TIMEOUT_SECONDS`
- `GEMINI_SEARCH_MODEL`
- `OPENROUTER_API_KEY`

Browser:

- `BROWSER_ENABLED`
- `BROWSER_BACKEND_MODE`
- `BROWSER_SAVE_SCREENSHOTS`
- Optional Browser Use: `BROWSER_USE_ENABLED`, `BROWSER_USE_API_KEY`.

Email and Google:

- `EMAIL_ENABLED`
- `EMAIL_PROVIDER`
- `EMAIL_API_KEY`
- `EMAIL_FROM_ADDRESS`
- `GMAIL_ENABLED`
- `GMAIL_CREDENTIALS_PATH`
- `GMAIL_TOKEN_PATH`
- `GMAIL_SCOPES`
- `GOOGLE_CALENDAR_ENABLED`
- `GOOGLE_CALENDAR_CREDENTIALS_PATH`
- `GOOGLE_CALENDAR_TOKEN_PATH`
- `GOOGLE_CALENDAR_SCOPES`
- `GOOGLE_TASKS_ENABLED`
- `GOOGLE_TASKS_CREDENTIALS_PATH`
- `GOOGLE_TASKS_TOKEN_PATH`
- `GOOGLE_TASKS_SCOPES`
- `GOOGLE_TASKS_LIST_ID`

Scheduler/reminders:

- `REMINDERS_ENABLED`
- `SCHEDULER_BACKEND`
- `SCHEDULER_TIMEZONE`

Never bake these into images or source files. Load them from the VPS environment, an ignored `.env`, or a proper secrets manager.

## Startup Notes

FastAPI:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Slack worker:

```bash
python -m app.slack_main
```

The Slack worker currently owns reminder scheduler startup. If reminders need to run when Slack is disabled, split the scheduler into a separate process before production.

## Google OAuth Tokens

Google credential and token files should live outside tracked source or under ignored paths such as `secrets/*.json`.

Required handling:

- Keep OAuth client secrets and token files out of git.
- Mount or provision token files on the VPS with restrictive filesystem permissions.
- Rotate tokens if they were ever exposed in logs, commits, screenshots, or chat.
- Treat Gmail, Calendar, and Tasks token files as separate operational secrets even when they share a Google project.
- Do not print token contents in readiness scripts or logs.

The current local OAuth flow may require browser interaction. For VPS operation, generate tokens through a controlled local/admin process, then copy them through the approved secret path.

## Browser And Playwright

Install Python dependencies, then install Playwright browsers on the VPS:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

If using Browser Use, set `BROWSER_USE_ENABLED=true` and provide `BROWSER_USE_API_KEY`. Browser artifacts and screenshots should go to ignored workspace artifact paths and should not be treated as durable storage unless explicitly archived.

## Persistent Storage

Plan persistent storage for:

- `WORKSPACE_ROOT`
- local memory files, if `MEMORY_PROVIDER=local`
- Chroma data, if `CHROMA_PATH` is used
- scheduler/reminder state, once durable scheduling is required
- browser artifacts needed for audit trails
- Google OAuth token files
- logs retained for operations

For production, prefer managed persistence for memory and operational state. Local JSON files are acceptable for development, but fragile for unattended VPS operation.

## Not Production Ready Yet

Known gaps before a real production deployment:

- No hardened process manager configuration is committed.
- No reverse proxy, TLS, firewall, or domain setup is defined.
- Scheduler durability and missed-run recovery need a production decision.
- Secret provisioning is documented but not automated.
- Browser sandboxing, artifact retention, and quota policies need hardening.
- Memory persistence should move beyond local files for serious use.
- Observability is still basic: logs and readiness checks exist, but no metrics/alerts.
- High-risk actions such as sending email/calendar mutations need explicit confirmation and audit trails.
- Frontend/dashboard deployment is not yet part of the runtime contract.

Use `python scripts/check_deployment_readiness.py` as a local sanity check before VPS work. Passing that script is not a production certification.
