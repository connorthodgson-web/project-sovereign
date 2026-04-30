# Real User Agent Bug Hunt

Date: 2026-04-29

## Scope

This pass tested the real Supervisor path with fake providers where actions could otherwise touch browser, calendar, tasks, or email systems. The goal was to find user-facing logic bugs, backend-jargon leaks, weak follow-ups, fake completion risks, and routing mistakes without adding integrations or weakening confirmation/evidence gates.

## Smoke Runner

Used a temporary inline smoke runner built from existing test helpers:

- `Supervisor`
- `AssistantLayer`
- `FastActionHandler`
- fake OpenRouter client disabled
- fake Calendar provider
- fake Google Tasks provider
- fake Browser tool from existing tests

The system Python could not import the app because the user-site `typing_extensions` package is incompatible with installed `pydantic_core`, so probes and tests were run with `venv\Scripts\python.exe`.

## Prompts Tested

Assistant / conversation:

- `hi`
- `what's up`
- `what can you do?`
- `what are you working on?`
- `thanks`
- `remember that browser-use comes after search`
- `what do you remember about Sovereign?`
- `banana`

Scheduling / calendar:

- `what do I have today?`
- `add basketball practice tomorrow at 6`
- `add practice at 6`
- `to Mars`
- `Friday`
- `schedule study session Friday from 7 to 8`
- `move that to 8`
- `delete that event`
- ambiguous referents after calendar reads

Reminders:

- `remind me to study at 7`
- `remind me every weekday at 8`
- `what reminders do I have?`
- `cancel that`
- invalid continuation cases covered by pending-question handling

Google Tasks:

- `what tasks do I have?`
- `add finish math homework to my tasks tomorrow`
- `add complete English worksheet to my tasks`
- `complete the second one`
- `mark that done`

Communications / Gmail / contacts:

- `Mom's email is fake@example.com`
- `draft an email to Mom saying hi`
- `send Mom an email saying I arrived`
- `do I have emails from fake@example.com?`
- `reply to the latest email from fake@example.com saying thanks`
- unknown recipient and missing Gmail setup flows

Browser / Browser Use:

- `check https://example.com`
- `summarize https://example.com`
- invalid URL handling
- Browser Use disabled / missing SDK readiness
- login/CAPTCHA/payment-like blockers covered by browser execution tests

Search / research:

- source-backed research execution path
- source-less and unconfigured search provider blocked states
- specific URL routing to browser path

Coding / Codex:

- `build a tiny Python script that prints hello`
- `create a simple README`
- Codex disabled behavior
- failed test evidence blocking behavior

Complex objective loop:

- multi-step execution with planning, tool use, review, verifier, and final status handling

## Bugs Found

1. Generic assistant fallback leaked capability metadata.
   - Observed replies contained phrases like `live; cost=free; risk=low`.
   - This made ordinary replies feel like backend state, not an assistant.

2. Explicit memory involving `browser-use` was intercepted by capability wording.
   - Prompt: `remember that browser-use comes after search`
   - Old reply described Browser Use setup instead of acknowledging memory.

3. Vague calendar create fell into the planning/research path.
   - Prompt: `add practice at 6`
   - Old result blocked on missing search provider instead of asking for the missing day.

4. Invalid calendar follow-up was not handled as a human clarification.
   - Prompt sequence: `add practice at 6` then `to Mars`
   - Desired behavior is a simple clarification, not backend continuation language or unrelated fallback.

5. Pending date/time questions could swallow a new question containing a date word.
   - Example: `what do I have today?` while a pending calendar event question existed.
   - The date word `today` could be treated as a slot answer instead of a new request.

## Fixes Made

- Changed generic deterministic assistant fallback to ask naturally what the user wants next instead of dumping live capability state.
- Sanitized capability labels before user-facing capability summaries so `cost=` and `risk=` metadata cannot leak.
- Expanded backend-jargon detection to reject cost/risk and pending-state terms.
- Moved explicit memory/contact handling before Browser Use / Manus capability branches.
- Taught scheduling detection that `add practice at 6` is a calendar-create attempt with a missing day.
- Added pending-question invalid-answer handling in the Supervisor so short invalid answers like `to Mars`, `banana`, and `later idk` get a direct clarification.
- Prevented question-shaped messages like `what do I have today?` from being treated as answers to pending date/time questions.
- Added calendar-create merge candidates so `add practice at 6` + `Friday` becomes `add practice Friday at 6`.

## Tests Added Or Updated

- `tests/test_assistant_feel_behavior.py`
  - Added `what's up` naturalness regression.
  - Expanded backend-jargon guard with `cost=`, `risk=`, `resume_target`, and `pending_action`.
  - Added memory regression so `remember that browser-use comes after search` replies as memory, not Browser Use setup.

- `tests/test_scheduling_calendar.py`
  - Added missing-day calendar create flow:
    - `add practice at 6`
    - invalid follow-up `to Mars`
    - valid follow-up `Friday`
  - Verifies no pending/backend language leaks and that the event is actually created with title `practice`.

Existing suites already covered:

- browser evidence path with `https://example.com`
- valid calendar create/update/delete confirmation flow
- Google Tasks titles containing `finish` and `complete`
- Gmail guarded-send and setup blockers
- reminders and recurring reminders
- objective loop review/verifier behavior
- Codex disabled and failed-test blocking behavior

## Test Results

Targeted requested suite:

- Command: `venv\Scripts\python.exe -m pytest tests/test_assistant_feel_behavior.py tests/test_short_term_continuity.py tests/test_scheduling_calendar.py tests/test_reminders.py tests/test_gmail_communications.py tests/test_browser_execution.py tests/test_objective_loop.py tests/test_operator_loop.py tests/test_codex_cli_agent.py tests/test_routing_stabilization.py -q`
- Result: `272 passed in 139.38s`

Full backend suite:

- Command: `venv\Scripts\python.exe -m pytest -q > pytest_backend.log 2>&1`
- Result: `408 passed in 163.25s`

## Agent Status

- Assistant / conversation: real. Naturalness is materially better; still needs more varied casual replies later.
- Scheduling / calendar: real for fake-provider tested read/create/update/delete flows; partial for live Google setup dependency.
- Reminders: real for local scheduler-backed tests; partial where outbound delivery depends on runtime setup.
- Google Tasks: real for fake-provider list/create/complete; partial for live Google setup dependency.
- Communications / Gmail / contacts: partial. Contact resolution and guarded flows are tested; real sends remain intentionally gated by setup and confirmation.
- Browser / browser-use: partial. Local browser path is evidence-backed; Browser Use remains optional/disabled unless configured.
- Search / research: partial. Correctly blocks without source-backed search; needs live provider configuration for real current research.
- Coding / Codex: partial to real. Artifact/test evidence behavior is covered; disabled Codex states block instead of faking success.
- Objective loop: partial to real. Planning/review/verifier status flow works; richer live execution depends on enabled tools and LLM configuration.

## Remaining Weak Spots

- Casual assistant replies are safe but still repetitive for very small talk.
- Calendar natural language parsing handles common cases, but unusual phrase order still relies on merge candidates.
- Ambiguous calendar referents are conservative and can ask more than a user expects when several recent objects exist.
- Live Gmail, Google Calendar, Google Tasks, Browser Use, and source-backed search all remain setup-dependent.
- Full user-visible subagent activity is not yet surfaced as a polished live view.

## Next Recommended Build

Build a small persistent real-user smoke harness that can run the prompt matrix through Supervisor with fake providers and save a compact JSON/Markdown transcript. Keep it manual and observational first, then promote only proven regressions into tests.
