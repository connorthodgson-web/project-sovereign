# Remote Browser Worker Plan

Project Sovereign should not assume heavy browser automation runs on the VPS first. The VPS should remain the always-on CEO/backend, API, Slack worker, scheduler, memory process, and lightweight research/search host. Browser-use and Playwright should prefer a home-computer worker for realistic browser execution.

This document is planning and configuration readiness only. It does not implement the full worker.

## Intended Design

VPS CEO/backend:

- Receives a user request from Slack or the dashboard.
- Uses the shared CEO/operator path to decide whether a browser task is needed.
- Creates a structured browser job with goal, target URL, constraints, and evidence requirements.
- Sends the job to a trusted home-computer browser worker.
- Receives structured results, screenshots, errors, and blockers.
- Reviews the evidence and returns an honest result to the user.

Home computer worker:

- Runs Playwright, browser-use, or both.
- Has stronger local resources than the VPS.
- Can use a local browser/session when appropriate.
- Captures screenshot evidence and structured observations.
- Returns completed results, partial results, or clear blockers.

The user-facing experience should still feel like one AI. The worker is an execution capability behind the CEO/operator, not a separate assistant brain.

## MVP Environment Variables

The VPS template reserves these values:

```text
BROWSER_WORKER_MODE=remote
BROWSER_WORKER_URL=
BROWSER_WORKER_SHARED_SECRET=
```

The browser integration should remain disabled until the remote worker is built and tested:

```text
BROWSER_ENABLED=false
BROWSER_USE_ENABLED=false
BROWSER_BACKEND_MODE=remote_worker
```

## Safety And Evidence

- Do not store raw passwords in memory, normal conversation history, screenshots, logs, or Git.
- The browser worker should escalate for CAPTCHA, 2FA, account recovery, payment, or other sensitive checkpoints.
- Browser job outputs should include evidence such as screenshots, final URL, extracted structured result, and error details when blocked.
- Readiness should be honest: disabled, missing worker URL, failed auth, timeout, or CAPTCHA are blockers, not fake completion.
- The VPS should review worker output before telling the user a browser task is complete.

## MVP Job Shape

A future browser job can be represented as:

```json
{
  "job_id": "browser-job-id",
  "goal": "What the user wants done",
  "url": "https://example.com",
  "constraints": ["Do not submit payment", "Escalate for 2FA"],
  "evidence_required": true
}
```

The worker response should include:

```json
{
  "job_id": "browser-job-id",
  "status": "completed_or_blocked",
  "summary": "What happened",
  "evidence": {
    "screenshot_path": "path-or-url",
    "final_url": "https://example.com/result",
    "observations": []
  },
  "blockers": []
}
```

## Not Implemented Yet

Do not claim remote browser readiness until these exist:

- Authenticated worker endpoint or queue.
- Shared secret validation or stronger auth.
- Browser job schema in code.
- Evidence upload/storage path.
- Timeout/retry behavior.
- Tests for completed, blocked, and misconfigured worker paths.
