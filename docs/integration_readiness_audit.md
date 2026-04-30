# Integration Readiness Audit

Date: 2026-04-28

Source of truth: `AGENTS.md`

## Executive Summary

Sovereign is now a credible assistant front door for chat, memory, Slack, reminders, Google Calendar, Google Tasks, Gmail, simple browser checks, files, and runtime tools. The strongest live slice is Life Assistant Mode: Slack DM transport, reminders, calendar/tasks, and memory all have real paths with honest blockers and confirmation gates.

The biggest product gap against `AGENTS.md` is still Goal Execution Mode for coding/build tasks. A prompt like "build a tiny Python script that prints hello" enters the execution loop, but the deterministic plan routes to simulated research/review instead of producing a verified file through the coding/Codex path. That is not ready enough for "finished output > chat response."

## What Got Fixed

- Bounded coding/build requests now have a real local artifact path when Codex CLI is not enabled: the planner creates a workspace file, runs generated Python scripts when safe, captures stdout/stderr/exit code, and sends the combined evidence through reviewer/verifier gates.
- `CodexCliAgentAdapter` now treats `incomplete` and `needs_user_review` reports as not complete instead of letting a successful process exit imply task completion.
- The reviewer/evaluator now understand local `coding_artifact` evidence so completion requires a created file plus a successful run for generated scripts.
- Slack final responses now prefer `client.chat_postMessage(channel=..., text=...)` when Slack Bolt provides a WebClient, with the old `say(...)` path retained as fallback.
- Slack delivery now has focused tests for simple answer, fast action, blocked action, and execution/progress responses.
- LangGraph per-turn transient state is cleared at the start of each invocation, preventing stale `response_payload` reuse across the same Slack thread.
- Direct browser phrasing now treats "check https://..." as an obvious browser request.
- Referent scheduling updates like "move that to 8" now route through the Scheduling Agent after a calendar read and remain confirmation-gated.
- Calendar referents are preferred over older reminder referents for update/move phrases when a calendar event was the most recent actionable object.

## Integration Inventory

| Integration | Status | Env configured? | Tests? | Live usage path? | Safe blocker wording? | Confirmation policy? | Biggest gap | Recommended next step |
|---|---:|---:|---:|---:|---:|---:|---|---|
| Slack transport | Real/live locally | Yes | Yes | Yes, Socket Mode DM -> Supervisor -> WebClient post | Yes | N/A for replies | Needs live Slack workspace verification after patch | Send live DM for answer/action/blocked/execution and confirm timestamps |
| Slack outbound | Real/live locally | Yes | Yes | Yes, reminders and Slack messaging tool | Yes | Required by caller/tool policy for risky outbound | Target resolution is limited | Keep as reminder delivery backbone; add live delivery monitor later |
| Google Calendar | Real/live locally | Yes | Yes | Yes, Scheduling Agent read/create/update/delete | Yes | Update/delete/invite actions require confirmation | Natural update parsing is limited | Harden Scheduling Agent edge cases after coding path |
| Google Tasks | Real/live locally | Yes | Yes | Yes, list/create/complete | Yes | Completion uses referent clarification; destructive bulk not present | No delete/bulk management yet | Keep focused; do not expand until core execution improves |
| Gmail | Real/live locally | Yes | Yes | Yes, Communications Agent read/search/draft/send guarded | Setup wording exists | Send/delete/archive/forward/bulk require confirmation | "email to myself" resolution is not robust; safe send UX needs more live testing | Later Email/Communications Agent pass, after coding/Codex |
| Browser / Playwright | Real/live locally | Yes | Yes | Yes, simple URL inspection through browser tool | Yes | N/A unless authenticated/risky | Broad browser autonomy is intentionally not implemented | Browser hardening later, after coding/Codex |
| browser-use | Partial/configured but disabled | API key + SDK present, disabled | Readiness tests | No default live path | Yes | N/A | Not enabled, not integrated as primary executor | Do not add yet; keep as future escalation backend |
| File tool | Real/live | Workspace root | Yes | Yes, file read/write/list | Yes | N/A | Mostly bounded simple operations | Keep as coding/Codex evidence surface |
| Runtime tool | Real/live | Workspace root | Yes | Yes, bounded shell command execution | Yes | N/A | Needs stronger integration into coding results | Use in Codex/coding verifier loop |
| Codex CLI | Partial/planned | Command configured and enabled | Yes | Adapter exists; bounded coding requests prefer local artifact execution unless Codex is enabled and safe | Yes | Destructive requests are guarded | Live Codex CLI execution still needs environment verification | Configure Codex CLI only when desired, then live-test managed coding |
| Chroma memory | Partial/live-ish | Chroma path/settings present | Yes | Memory provider/adapters exist; local memory remains main | Yes | Secrets excluded | Semantic retrieval not clearly default-live | Clarify local vs Chroma provider mode in memory docs |
| Zep | Scaffold | Placeholder env only | Some adapter coverage | No live path | Yes | N/A | Placeholder only | Leave parked |
| Supabase | Missing/scaffold | Not configured in readiness table | Limited/no live path | No | N/A | N/A | Needed for durable production memory later | Later persistence phase, not next |
| OpenClaw / Manus / external agents | Planned/scaffold | Not configured | Readiness metadata only | No | Yes | N/A | No runtime bridge | Do not add yet |
| Telegram/texting/SMS/Discord | Planned/missing | Not configured | Placeholder tests/metadata | No | Yes | Confirmation needed for outbound | No adapters | Leave future-facing |
| Voice/calls | Planned/missing | Not configured | Metadata only | No | Yes | Confirmation/high-risk needed | No provider path | Leave future-facing |

## Tool Auto-Call Smoke

Non-destructive smoke used fake providers for Google Tasks, Calendar, reminders, browser, and disabled Gmail to avoid modifying live accounts.

| Prompt | Expected lane/tool | Actual lane/tool | Reply feel | Bug found |
|---|---|---|---|---|
| hi | answer | `conversation_fast_path` | Natural | None |
| what can you do? | answer/capability | `conversation_fast_path` | Natural, one assistant voice | None |
| remember that email comes after tasks | memory | `conversation_fast_path`, memory write | Natural | None |
| what do you remember about Sovereign? | memory recall | `conversation_memory_fast_path` | Honest sparse-memory answer | None |
| what tasks do I have? | Google Tasks list | `fast_action`, `google_tasks`, `scheduling_agent` | Natural | Lane label says assistant in one graph log, result/tool correct |
| add finish math homework to my tasks for tomorrow | Google Tasks create | `fast_action`, `google_tasks`, `scheduling_agent` | Natural | None |
| remind me to study at 7 | reminder scheduler | `fast_action`, `reminder_scheduler`, `scheduling_agent` | Natural; scheduled time explicit | None |
| what do I have today? | Calendar read | `fast_action`, `google_calendar`, `scheduling_agent` | Natural | None |
| move that to 8 | Calendar update confirmation | `fast_action`, `google_calendar`, blocked pending confirmation | Natural enough; asks confirmation | Fixed referent routing |
| draft an email to myself saying hello | Gmail draft or setup blocker | `fast_action`, `gmail`, blocked in disabled smoke | Safe wording | "myself" recipient resolution still needs product decision/live test |
| check https://example.com | Browser tool | `fast_action`, `browser_tool`, `browser_agent` | Natural | Fixed "check" routing |
| build a tiny Python script that prints hello | Coding/Codex execution | Execution loop, `coding_agent` file write + runtime run + reviewer/verifier evidence | Natural, evidence-backed | Fixed in this pass |

## Slack Delivery Diagnosis

Before this pass, inbound Slack DM handling used Bolt's `say(...)` callable from a background thread for final replies. Logs could show `SLACK_BRIDGE_COMPLETED` before the final Slack send, and the code had no explicit `chat_postMessage` delivery evidence for normal responses.

After this pass:

- `SlackClient._handle_message_event` accepts the injected Slack `client`.
- Progress and final messages use `_deliver_message`.
- `_deliver_message` calls `client.chat_postMessage` when `client` and `channel_id` exist.
- It logs `SLACK_REPLY_DELIVERED method=chat_postMessage ...`.
- Empty backend replies get a safe fallback message rather than attempting an empty Slack post.
- `say(...)` remains as fallback for tests or runtimes without a WebClient.

Live Slack still needs a real workspace verification pass because unit tests can only prove the WebClient method is called.

## Files Involved

- `integrations/slack_client.py`
- `core/orchestration_graph.py`
- `core/browser_requests.py`
- `core/assistant.py`
- `core/fast_actions.py`
- `tests/test_slack_interface.py`
- `tests/test_langgraph_orchestration.py`
- `tests/test_assistant_feel_behavior.py`
- `tests/test_scheduling_calendar.py`
- `docs/integration_readiness_audit.md`

## Intentionally Not Changed

- No Manus/OpenClaw integration was added.
- No broad browser autonomy was added.
- No email sending behavior was loosened.
- No credentials or tokens were stored.
- No large Slack architecture rewrite was attempted.
- `fast_actions.py` was only touched for a narrow referent-ordering bug, not expanded into a new planning brain.
- Reviewer/verifier gates were not weakened.

## Recommended Next Build

Pick the next narrow Goal Execution hardening pass.

Why: bounded coding artifacts now produce real files and runtime evidence, so the next value is hardening adjacent execution paths without broad rewrites: live Codex CLI environment verification, richer coding artifact naming/content generation, or objective-loop mixed browser-to-file behavior.

## Risks

- Live Slack may still fail due to Slack app scopes, token validity, app subscriptions, or Socket Mode runtime state even though the code now calls `chat_postMessage`.
- The runtime has live Gmail, Calendar, and Tasks credentials; manual smoke tests can mutate real data if not isolated.
- LangGraph checkpointing now resets transient fields per turn; this should preserve thread identity while avoiding stale results, but live objective-resume behavior should be watched.
- Gmail is live according to readiness, but live draft/send flows need careful manual testing with confirmation gates.
- Browser Use is configured but disabled; users may assume it is live unless capability wording stays precise.

## Manual Setup Still Needed

- Restart the Slack Socket Mode process so the `chat_postMessage` patch is active.
- In Slack, verify the bot has the required scopes for DM posting, especially `chat:write` and DM event subscriptions.
- Confirm Google Calendar, Google Tasks, and Gmail OAuth token files are valid without printing or moving credentials.
- Decide whether Chroma should become the default memory provider or stay behind local memory.
- Configure Codex CLI execution policy if the next build uses `CodexCliAgentAdapter`.

## What To Test Live Next

1. Slack DM: `hi` should produce exactly one final reply.
2. Slack DM: `what tasks do I have?` should produce a Google Tasks response with no progress spam.
3. Slack DM: `remind me to study at 7` should schedule a reminder and later deliver through Slack outbound.
4. Slack DM: `what do I have today?` then `move that to 8` should ask for confirmation and not update until confirmed.
5. Slack DM: `check https://example.com` should return browser evidence.
6. Slack DM: `build a tiny Python script that prints hello` should remain honestly incomplete today; after the next build it should create an artifact, run verification, and report evidence.

## Verification

- `python -m pytest tests/test_operator_loop.py tests/test_codex_cli_agent.py tests/test_assistant_feel_behavior.py tests/test_langgraph_orchestration.py -q` passed: 97 tests in 11.84s.
- `python -m pytest tests/test_memory_recall.py::MemoryRecallTests::test_memory_follow_up_includes_all_relevant_durable_facts -q` passed: 1 test in 1.27s.
- `python -m pytest -q` completed in 261.74s with 371 passing and 9 failures outside this coding/Codex pass, concentrated in browser clarification, communications/email behavior, calendar OAuth wording, model-routing memory fast path, and objective-loop mixed browser/file expectations.
- `python -m pytest tests/test_slack_interface.py tests/test_assistant_feel_behavior.py tests/test_scheduling_calendar.py tests/test_reminders.py tests/test_short_term_continuity.py tests/test_memory_recall.py -q` passed: 126 tests in 80.52s.
- `python -m pytest tests/test_langgraph_orchestration.py tests/test_slack_interface.py tests/test_assistant_feel_behavior.py tests/test_scheduling_calendar.py tests/test_short_term_continuity.py -q` passed: 82 tests in 49.60s.
- `python -m pytest tests/test_scheduling_calendar.py tests/test_short_term_continuity.py -q` passed: 49 tests in 65.48s.
- Previous audit note: an earlier `python -m pytest -q` attempt timed out after about 304 seconds with no visible pytest progress output before timeout.
