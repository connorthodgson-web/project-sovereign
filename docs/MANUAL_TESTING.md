# Manual Testing

Use this checklist before and after deployment changes.

Current production-test shape:

- Vercel dashboard and Slack DM should both reach the same `/chat -> core.transport -> supervisor` path.
- VPS owns the backend API, Slack worker, scheduler/reminders, API, memory, and lightweight research/search.
- Heavy browser automation should stay disabled on the VPS until explicitly tested; future browser-use/Playwright execution should prefer a home-computer worker.

## 1. Backend Health Check

```bash
python scripts/health_check.py --url http://127.0.0.1:8000/health
```

Expected:

- HTTP 200
- JSON body includes `"status": "ok"`

## 2. Slack Message To CEO Response

1. Start the backend API.
2. Start the Slack worker with `python -m app.slack_main`.
3. Send a DM to the Sovereign Slack app.
4. Confirm the app replies in the DM.
5. Confirm the backend logs show the shared operator path and no raw secrets.

Expected:

- Slack message is normalized.
- Slack transport reaches the CEO/operator loop.
- Execution tasks get a short progress acknowledgement when appropriate.

## 3. Dashboard Message To Same CEO Path

1. Start the backend API.
2. Start the dashboard with `cd frontend && npm run dev`.
3. Open the dashboard.
4. Send a chat message from the main operator panel.

Expected:

- The dashboard calls `POST /chat`.
- The payload includes `transport: "dashboard"`.
- The response shape matches `ChatResponse`.
- The same supervisor/operator loop handles the message.

## 3a. Future iOS Transport Contract

Use the same backend endpoint:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"hello from mobile contract","transport":"ios","channel_id":"ios-manual-test","user_id":"manual-user"}'
```

Expected:

- HTTP 200.
- Response shape matches `ChatResponse`.
- The request reaches `core.transport.handle_operator_message(...)`.
- There is no iOS-only assistant logic.

## 4. Memory Recall Test

Send:

```text
Remember that I prefer concise answers.
```

Then send:

```text
What do you remember about me?
```

Expected:

- Sovereign recalls the preference.
- The dashboard memory panel can refresh without exposing secrets.

## 5. Reminder Or Task Test

If reminders are enabled:

```text
Remind me in 2 minutes to check Sovereign deployment.
```

Expected:

- Sovereign schedules the reminder.
- The reminder appears in `/life/reminders`.
- Slack delivery works if Slack outbound credentials are configured.

If Google Tasks is enabled:

```text
What tasks do I have?
```

Expected:

- Sovereign reports task readiness or live task results honestly.

## 6. Browser Or Tool Readiness Test

If browser execution is enabled:

```text
Open https://example.com and summarize it.
```

Expected:

- Sovereign reports real browser evidence or a clear blocker.
- `/browser/status` shows a safe evidence summary.

If browser execution is disabled:

- `/integrations/status` should say the browser is disabled or unavailable.
- Sovereign should not claim browser work succeeded.

## 7. Error Handling Test

Stop the backend and send a dashboard chat message.

Expected:

- The dashboard enters offline/mock fallback mode.
- No stack trace or secret value appears in the UI.

Restart the backend and refresh.

Expected:

- The dashboard returns to live API mode.

## 8. Deployment Test

Frontend:

1. Make a harmless frontend text/style change.
2. Push to `main`.
3. Confirm Vercel builds the `frontend/` app.
4. Confirm the deployed dashboard points at `VITE_SOVEREIGN_API_URL`.

Expected Vercel value for the current VPS test:

```text
VITE_SOVEREIGN_API_URL=http://187.124.213.208:8000
```

Backend:

1. Make a harmless backend change.
2. Push to `main`.
3. Confirm GitHub Actions runs `backend-deploy.yml`.
4. Confirm the VPS runs `scripts/deploy_backend.sh`.
5. Confirm systemd restarts the backend.
6. Confirm `scripts/health_check.py` passes.

Expected GitHub Actions behavior:

- Success means the VPS pulled `main`, installed dependencies, ran the deployment test command, restarted `sovereign-backend.service` and `sovereign-worker.service`, and passed `/health`.
- Failure means the live backend may still be running the previous version. Inspect GitHub Actions logs first, then `journalctl` on the VPS.

VPS env changes are manual. Edit and restart with:

```bash
nano /opt/project-sovereign/.env
systemctl restart sovereign-backend.service
systemctl restart sovereign-worker.service
python /opt/project-sovereign/scripts/health_check.py --url http://127.0.0.1:8000/health
```

## 9. Regression Checklist After Each Bug Fix

Run:

```bash
python -m pytest tests/test_shared_transport.py tests/test_slack_interface.py tests/test_operator_console_api.py tests/test_operator_loop.py
```

For frontend changes, run:

```bash
cd frontend
npm run build
```

Verify:

- Slack and dashboard still share the operator path.
- The dashboard chat is not replaced with mock-only behavior.
- No new hardcoded secrets are introduced.
- Responses still distinguish completed, blocked, planned, and simulated work.
- Meaningful execution paths still produce evidence or honest blockers.
