# Assistant Feel Audit

Date: 2026-04-27

## Prompts Tested

- `hi`
- `what can you do?`
- `what are you working on?`
- `remember that my project priority is memory then calendar`
- `remind me to study at 7`
- `what do I have today?`
- `build a small Python script that solves a quadratic`
- `research this topic and give me a plan`
- `send an email draft to alex@example.com saying hi`
- `open https://example.com and summarize it`
- blocked calendar/email requests with missing account setup

## What Passed

- Simple chat stays in answer mode, returns natural language, and does not trigger Slack progress acknowledgement.
- Memory updates stay conversational and do not leak planner/router/evaluator language.
- Reminder requests route through the fast scheduling path without Slack `On it.` noise.
- `remind me to study at 7` now canonicalizes into the reminder parser and schedules cleanly when Slack delivery context is present.
- Calendar and Gmail requests fail honestly when account access is missing, with user-facing setup language instead of adapter/runtime wording.
- Coding and research prompts enter the graph-backed planning/execution path.
- Browser requests route through the browser lane and return page-level results.
- Normal assistant replies are checked for backend jargon including `LangGraph`, `planner`, `router`, `evaluator`, `AgentResult`, `task_status`, `tool invocation`, and `orchestration graph`.

## Still Weak

- Offline deterministic execution for broad coding/research prompts can still produce generic progress-style completion text because real coding/research execution depends on live tool capability.
- Calendar and Gmail setup blockers are human-readable now, but the product still needs a smoother guided account-connection flow.
- The transport-side Slack progress decision is still a local preview; it is intentionally conservative, but a future LLM-aware nonblocking preview could make edge cases feel smarter.

## Build Next

- Add a small golden-transcript harness that exercises Slack message flows end-to-end and snapshots user-facing copy.
- Add account-connection UX for Gmail and Google Calendar so blocked states can offer a concrete next action inside Sovereign.
- Expand execution-quality tests around real coding artifacts once the coding path is fully connected.
- Add browser blocked-state cases for 2FA/CAPTCHA and verify they produce evidence plus a clean human escalation.
