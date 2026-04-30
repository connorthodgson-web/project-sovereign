# AI Hub Development Workflow

Project Sovereign should evolve as a personal AI hub: one CEO/operator brain, many clients, and many tool integrations.

## Product Shape

- Slack is the first live interface.
- Dashboard is a first-class chat and control surface.
- Future iOS should be another client of the same backend.
- The backend owns planning, delegation, memory, review, verification, and tool orchestration.
- Clients should format messages and show state; they should not become separate assistant brains.

## Add A New Tool Or Integration

1. Add provider settings to `app/config.py` and `.env.example` with blank secret values.
2. Put provider-specific code under `integrations/` or `tools/`.
3. Keep credentials in VPS `.env` or `secrets/`, not Git.
4. Expose readiness through the existing integration readiness surface.
5. Add a tool/agent contract that returns structured evidence or an honest blocker.
6. Let the supervisor/planner decide when to use the tool through capability metadata and LLM-led orchestration.
7. Add tests for disabled, misconfigured, and successful paths.
8. Add manual testing steps to `docs/MANUAL_TESTING.md` if the integration changes product behavior.

Avoid hardcoding broad Python routing like "if user says X, always call tool Y" as the main intelligence layer. Deterministic code is appropriate for adapters, validation, parsing, retries, safety gates, and fallback behavior.

## Add A New Dashboard Panel

1. Add a backend summary endpoint if live data is needed.
2. Return safe summaries only; never expose raw secrets or unrestricted memory.
3. Add frontend types in `frontend/src/types.ts`.
4. Add an API client function in `frontend/src/lib/apiClient.ts`.
5. Add a panel in `frontend/src/App.tsx`.
6. Add mock fallback data only when clearly labeled as mock.
7. Confirm `npm run build` passes.
8. Manually test both live API and offline fallback.

The dashboard can show workstreams, evidence, memory summaries, reminders, integrations, and future browser views. It should not make its own separate decisions about user goals.

## Add A New Client Like iOS

Preferred first version:

```text
iOS app
-> authenticated HTTPS call
-> POST /chat
-> transport: "ios"
-> core.transport.handle_operator_message(...)
-> supervisor.handle_user_goal(...)
-> ChatResponse
```

If iOS needs its own endpoint for auth or push-notification metadata, keep it as a thin adapter that constructs the same `OperatorMessage`.

## Preserve One CEO Operator Brain

- Keep `core.transport.handle_operator_message` as the shared inbound contract.
- Keep `core.supervisor.Supervisor` as the owner of goal interpretation and execution.
- Keep Slack, dashboard, and iOS transport-specific code thin.
- Store transport metadata in interaction context, not separate planning logic.
- Add new agent capabilities as capabilities, not as competing assistant flows.
- Review meaningful outputs before claiming completion.

## Manual Deploy Testing For New Integrations

After pushing and deploying:

1. Check `/health`.
2. Check dashboard integration readiness.
3. Send a Slack message that should use the integration.
4. Send a dashboard message for the same behavior.
5. Confirm both clients reach the same operator path.
6. Confirm successful work includes evidence.
7. Confirm disabled/missing credentials produce an honest blocker.
8. Confirm logs do not print secret values.
9. Fix locally, run tests, commit, push, and retest live.

## Safe Git Discipline

Codex/Cursor/VS Code can edit files locally. The safe product workflow is reviewed commits and pushes:

```bash
git status --short
git diff
python -m pytest
cd frontend && npm run build
git add <reviewed-files>
git commit -m "Clear change description"
git push origin main
```

Do not add automation that silently commits or pushes generated changes without review.
