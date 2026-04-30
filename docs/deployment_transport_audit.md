# Deployment and Transport Audit

Date: 2026-04-30

## Current Structure Summary

Project Sovereign currently uses root-level Python packages rather than a nested `backend/` package:

- `app/`: FastAPI bootstrap, runtime config, and Slack process entrypoint.
- `api/routes/`: HTTP routes for health, chat, tasks, and the operator console.
- `core/`: supervisor/operator loop, planning, routing, evaluation, state, memory context, and shared models.
- `agents/`: standing agent implementations and adapters.
- `tools/`: local tool implementations and capability metadata.
- `integrations/`: Slack, browser, calendar, reminders, Gmail, search, OpenRouter, and readiness adapters.
- `memory/`: local, Chroma, Zep, retrieval, contacts, and memory safety code.
- `workers/`: scheduler/job-runner scaffolding.
- `frontend/`: Vite/React operator console.
- `scripts/`: local diagnostics and readiness scripts.
- `docs/`: product and technical audits.

The repository now includes `.github/workflows/` for frontend checks and backend VPS deploys. The repository also contains generated/local content that should not become product source of truth, including tracked `venv/` files, tracked Python bytecode, runtime workspace artifacts, `.envz`, and a separate untracked `Chess Engine/` project with `node_modules/`.

## Current Entrypoints

- Backend API: `app.main:app`
- Slack Socket Mode runtime: `app.slack_main:main` and the `sovereign-slack` package script.
- Health endpoint: `GET /health`
- Canonical chat endpoint today: `POST /chat`
- Task inspection endpoint: `GET /tasks`
- Dashboard/operator console read-only endpoints:
  - `GET /agents/status`
  - `GET /runs/status`
  - `GET /integrations/status`
  - `GET /memory/summary`
  - `GET /life/reminders`
  - `GET /life/calendar`
  - `GET /life/tasks`
  - `GET /browser/status`
  - `GET /browser/artifacts`

## Transport Findings

- `POST /chat` calls `core.transport.handle_operator_message(...)`.
- Slack DMs go through `SlackOperatorBridge` and enter the same shared transport path with `transport="slack"`.
- The Vite dashboard includes a first-class chat surface and posts to `/chat` with `transport: "dashboard"`.
- `ChatRequest` and `OperatorMessage` accept `transport: "ios"` for the future mobile client contract.
- Slack, dashboard, and future iOS are thin clients over the same supervisor/operator path rather than separate assistant brains.

## Deployment Findings

- Frontend is already structurally suitable for Vercel as a Vite app in `frontend/`.
- Frontend env surface is minimal: `VITE_SOVEREIGN_API_URL`.
- Backend is suitable for VPS/systemd deployment, with a deploy script, health-check script, service docs, and GitHub Actions deploy workflow now scaffolded.
- `scripts/check_deployment_readiness.py` is a useful local preflight and does not print secret values.
- `.env.example` is present and secret values are blank.
- `.gitignore` ignores `.env`, `secrets/`, Python caches, frontend build output, local browser artifacts, OS/editor junk, and the unrelated `Chess Engine/` directory. Some ignored files are still tracked until removed from the Git index.

## What Is Already Good

- The operator brain is centralized in `core.supervisor.Supervisor`.
- FastAPI, Slack, and dashboard chat all point toward the same supervisor rather than separate business logic.
- The frontend is more than a status page: it has chat, workstream visibility, browser evidence, memory, life ops, and integration readiness.
- The backend exposes safe dashboard summaries that intentionally avoid raw secrets and unrestricted memory dumps.
- Tests already cover Slack behavior, operator console safety, operator loop behavior, memory, reminders, browser execution, and foundation layers.
- The architecture is aligned with AGENTS.md: LLM-led planning exists, deterministic behavior is mainly fallback/adapters, and execution states distinguish completed, blocked, planned, and simulated work.

## Risks and Mess

- `venv/` appears to be tracked in git despite `.gitignore`; this will make GitHub the source of truth noisy and fragile until removed from the index in a separate cleanup.
- There are many modified and untracked files, including generated `__pycache__` files and local workspace artifacts. This worktree needs a careful commit plan.
- `Chess Engine/` appears unrelated to Sovereign deployment and includes frontend dependency/build output. It should be moved out of this repo or explicitly quarantined before GitHub becomes canonical.
- The shared transport contract is now represented by `core.transport.handle_operator_message(...)`.
- Backend deployment automation is scaffolded but still requires GitHub secrets, a VPS clone, systemd services, and VPS `.env` configuration.
- There is no CORS/auth story documented for a Vercel-hosted dashboard calling a VPS backend. This is acceptable for scaffold work, but it is a product deployment blocker before public exposure.

## Recommended Target Structure

Keep the current root-level backend packages for now to avoid breaking imports:

```text
app/
api/
core/
agents/
tools/
integrations/
memory/
prompts/
workers/
tests/
frontend/
scripts/
docs/
.github/workflows/
```

Longer-term, if the repo gets reorganized, migrate gradually toward:

```text
backend/
  app/
  api/
  core/
  agents/
  tools/
  integrations/
  memory/
  prompts/
  workers/
  tests/
frontend/
scripts/
docs/
.github/workflows/
```

That migration should happen in a dedicated branch with import-path tests, not during this deployment foundation pass.

## Recommended Transport Target

Use one canonical operator entrypoint:

```text
transport message
-> shared request normalization/context binding
-> supervisor.handle_user_goal(...)
-> ChatResponse
-> transport-specific formatting
```

Immediate safe target:

- Add a small shared transport module in `core/`.
- Let `/chat` call it with transport type `dashboard` by default.
- Let Slack call it with transport type `slack`.
- Preserve Slack-specific response formatting in the Slack integration only.
- Keep planning/routing inside the existing supervisor and LLM-led orchestration.

## Workflow Documents Added

- `docs/PRODUCT_WORKFLOW.md`: desired local-to-GitHub-to-Vercel/VPS workflow and current gap analysis.
- `docs/GO_LIVE_CHECKLIST.md`: step-by-step first launch checklist.
- `docs/AI_HUB_DEV_WORKFLOW.md`: how to evolve Sovereign as a personal AI hub without fragmenting the CEO/operator path.

## Safe Cleanup Scope

Safe to untrack from Git without deleting local files:

- `venv/`
- tracked `__pycache__/` and `*.pyc`
- `.envz`
- tracked runtime memory/browser artifacts under `workspace/.sovereign/` and `workspace/created_items/browser/`

Avoid in this pass:

- Moving existing backend packages into `backend/`.
- Deleting local copies of generated files.
- Deleting or importing `Chess Engine/`.
- Large frontend redesign.
- Rewriting supervisor/planner/router behavior.

## Safe To Continue?

Yes, implementation is safe to continue if scoped to additive deployment scaffolding, a thin shared transport wrapper, documentation, and Git index cleanup. It is not safe to claim live deployment success until GitHub, Vercel, VPS services, DNS/CORS, and secrets are configured externally and manually tested.
