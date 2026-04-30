# Project Sovereign

Project Sovereign is a backend-first operator scaffold built around a clean orchestration chain:

`Supervisor -> Planner -> Router -> Agent`

This current implementation now supports a real first-pass operator loop while staying explicit about what is still only planned, simulated, or blocked.

## What Works Now

- `GET /health` returns a basic health response.
- `POST /chat` accepts a high-level goal and runs it through:
  - supervisor task creation
  - planner subtask decomposition
  - router-based agent assignment
  - structured per-agent results
  - supervisor aggregation into a clear response
- `GET /tasks` returns the current in-memory task list, including subtasks and routed results.
- Planning supports:
  - deterministic fallback planning with no external dependency
  - optional OpenRouter-assisted planning when `OPENROUTER_API_KEY` is configured and reachable
- Agents return honest execution states:
  - `completed`
  - `planned`
  - `simulated`
  - `blocked`

## Current Architecture

- `app/`: FastAPI bootstrap and configuration
- `api/`: HTTP routes
- `core/`: supervisor, planner, router, state, and shared models
- `agents/`: specialized worker agents
- `integrations/`: external service adapters
- `memory/`: early memory modules
- `workers/`: future background execution hooks

## Integrations Status

Implemented now:

- OpenRouter client for optional planning assistance
- Slack DM interface via Socket Mode as a thin transport to the supervisor

Placeholder-only:

- Browser Use live browser execution
- Telegram delivery
- Supabase persistence
- Voice STT/TTS

Important honesty rules in the current backend:

- Browser work is recognized and planned, but not claimed as executed.
- Research and review can synthesize structured guidance without pretending they performed live retrieval or verification.
- Communications work can be routed and outlined without sending a real message.
- Task state is in-memory only for now.

## Environment Variables

Used by the current operator loop:

- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`
- `BROWSER_USE_API_KEY`
- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`
- `REMINDERS_ENABLED`
- `SCHEDULER_BACKEND`
- `SCHEDULER_TIMEZONE`

Present in config but not used by the current loop:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACK_CLIENT_ID`
- `SLACK_CLIENT_SECRET`
- `TELEGRAM_BOT_TOKEN`
- `VOICE_API_KEY`

## Local Run

1. Activate the virtual environment:

```bash
source venv/bin/activate
```

2. Install dependencies if needed:

```bash
pip install -r requirements.txt
```

3. Copy environment variables if needed:

```bash
cp .env.example .env
```

4. Run the backend:

```bash
./venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

5. Run the Slack DM interface:

```bash
./venv/bin/python -m app.slack_main
```

Or, after installation:

```bash
sovereign-slack
```

## Example Request

```bash
curl -sS -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Research what is needed to add browser-driven QA for our app and prepare the implementation plan."}'
```

Example response shape:

```json
{
  "task_id": "d4d3f8e9-2788-46ef-b917-f4f66fe697f2",
  "status": "blocked",
  "planner_mode": "deterministic",
  "response": "Sovereign created 4 subtasks for 'Research what is needed to add browser-driven QA for our app and pr...' using deterministic planning. Completed: 1, blocked: 1, simulated: 2, planned: 0. Routed agents: browser_agent, memory_agent, research_agent, reviewer_agent. Results distinguish actual completion from preparation and blockers.",
  "outcome": {
    "completed": 1,
    "blocked": 1,
    "simulated": 2,
    "planned": 0,
    "total_subtasks": 4
  },
  "subtasks": [
    {
      "title": "Capture goal context",
      "assigned_agent": "memory_agent",
      "status": "completed"
    },
    {
      "title": "Plan constraints and dependencies",
      "assigned_agent": "research_agent",
      "status": "running"
    },
    {
      "title": "Prepare browser execution path",
      "assigned_agent": "browser_agent",
      "status": "blocked"
    },
    {
      "title": "Review plan integrity",
      "assigned_agent": "reviewer_agent",
      "status": "running"
    }
  ],
  "results": [
    {
      "agent": "browser_agent",
      "status": "blocked",
      "summary": "Browser-oriented work was recognized, but live browser execution is blocked until a Browser Use adapter is wired into the runtime."
    }
  ]
}
```

## Notes

- `POST /chat` is synchronous and runs in-process.
- The Slack process is also synchronous and forwards DM text directly into `supervisor.handle_user_goal(...)`.
- One-time reminders are live only when Slack outbound is configured and the scheduler runtime is enabled with `REMINDERS_ENABLED=true` and `SCHEDULER_BACKEND=apscheduler`.
- Slack formatting is intentionally minimal: one top-level result, a compact status line, and a short preview of the first few results/evidence items.
- The first operator loop is intentionally minimal and extensible rather than fully autonomous.
- Browser Use, Supabase, Telegram, and voice should be added behind the existing layers instead of bypassing them.
