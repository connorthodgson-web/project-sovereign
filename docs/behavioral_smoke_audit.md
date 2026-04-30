# Behavioral Smoke Audit

Date: 2026-04-28

Scope: Manual `/chat` smoke pass through a local FastAPI server on `127.0.0.1:8012`, followed by focused pytest runs. I read `AGENTS.md` first and tested Sovereign as a user-facing CEO-style assistant across chat, memory, reminders, calendar, Google Tasks, coding, and browser behavior.

## Summary

Sovereign mostly feels like one assistant, not a pile of visible backend parts. Simple chat is natural, memory recall works, and the system automatically selected live tools for calendar, Google Tasks, reminders, and browser work. Calendar and Google Tasks were actually connected in this environment. Reminder delivery was honestly blocked because the local `/chat` session did not provide a live Slack delivery target.

The largest behavioral gaps were:

- Calendar update follow-up did not route to the scheduling action path. `move that to 8` and `yes, move basketball practice to 8 PM` stayed conversational instead of setting and consuming the confirmation.
- The coding task created a valid file but returned `blocked` because no run result was captured. That is honest, but the user asked for a tiny script, so the end-to-end coding path still feels incomplete.
- Before the fix in this pass, Google Tasks due dates from Google's date-only midnight UTC format displayed as `due today` in Eastern time when the user asked for tomorrow.

## Manual Probe Log

| Area | Prompt | Observed response | Tools called | Memory/context used | Natural? | AGENTS.md match |
| --- | --- | --- | --- | --- | --- | --- |
| Simple chat | `hi` | `Hi. What can I help with?` | No | Session turn only | Yes | Good. Short, assistant-like, no backend jargon. |
| Capabilities | `what can you do?` | Said it can chat, remember context, handle reminders/lists, work with files, run browser tasks, and route coding/research. | No | No material memory needed | Yes | Good. Honest enough, no internal route labels. |
| Memory write | `remember that my current Sovereign priority is email after tasks` | `Noted. I'll remember that.` | Memory write | Yes | Yes | Good. Low-friction memory capture. |
| Memory recall | `what do you remember about Sovereign?` | `I remember the current project priority is email after tasks.` | Memory read | Yes | Yes | Good. Recalled the specific fact cleanly. |
| Reminder create | `remind me to study at 7` | Blocked: needs a live Slack conversation to deliver the reminder. | `reminder_scheduler` via scheduling agent | Wrote reminder/open-loop context | Mostly | Good honesty. It did not pretend delivery was active. |
| Reminder list | `what reminders do I have?` | Listed open reminders including `Study at 7 PM`; said delivery needs live Slack. | Memory/reminder context, no live delivery | Yes | Mostly | Good honesty, slightly chatty. |
| Calendar read | `what do I have today?` | Returned `7:30 PM: Basketball Practice at Country Day`. | `google_calendar` | Yes, wrote session turn | Yes | Good. Correctly distinguished calendar from reminders/tasks. |
| Calendar create | `add basketball practice Friday at 6` | Added basketball practice for May 1 at 6:00 PM. | `google_calendar` | Registered recent calendar object | Yes | Good tool use. This did create a live calendar event. |
| Calendar update | `move that to 8` | Asked which item `that` referred to, listing basketball practice and reminder possibilities. | No | Used context but did not route action | Mostly | Partial. It avoided risky mutation, but should have recognized the recent calendar event or asked a tighter confirmation. |
| Calendar update follow-up | `yes, move basketball practice to 8 PM` | Asked for confirmation but did not actually set a pending update action. | No | Used calendar context | Mostly | Gap. It sounded natural but stayed in conversation mode instead of scheduling mode. |
| Calendar follow-up read | `what do I have Friday?` | Returned two `6:00 PM: basketball practice` entries. | `google_calendar` | Yes | Yes | Tool worked. Duplicate event exists after this and/or prior smoke runs. I did not delete it because one copy may have preexisted. |
| Task list | `what tasks do I have?` | `You don't have any tasks.` | `google_tasks` | Session turn | Yes | Good. Correctly used tasks, not reminders/calendar. |
| Task create | `add finish math homework to my tasks for tomorrow` | Before fix: `due today`; after fix with `verify due date smoke`: `due tomorrow`. | `google_tasks` | Registered recent task | Yes after fix | Fixed. Correct route, previously wrong date wording. |
| Task complete | `mark that done` / `mark verify due date smoke done` | Marked the recent task complete. | `google_tasks` | Used recent task referent | Yes | Good. Low-friction completion. |
| Next build advice | `what should I work on next for Sovereign?` | Recommended email after tasks and referenced current project memory. | LLM answer with memory/context | Yes | Mostly | Good context use, a little long and ended with a follow-up question. |
| Coding | `build a tiny Python script that prints hello` | Created `workspace/created_items/hello_world.py`, but response status was `blocked` because no run result was captured. | `file_tool` via coding agent | Session/project context | Mostly | Partial. Honest, but not fully end-to-end. I manually ran the file and it prints `Hello, World!`. |
| Browser | `open example.com and tell me the page title` | `Example Domain: Example Domain` with source URL. | `browser_tool`, Playwright backend | Session turn | Yes | Good. Used live browser path and returned evidence-like source. |

## What Worked

- Normal replies avoided obvious backend jargon such as planner, router, graph, subtasks, or internal status labels.
- The assistant voice was generally natural and direct.
- Memory write and recall worked immediately.
- Calendar reads and creates used live Google Calendar.
- Google Tasks list/create/complete used live Google Tasks.
- Browser execution used Playwright and returned the page title/source.
- Missing reminder delivery setup was explained in human terms: it needs a live Slack delivery target.
- Tool traces confirmed automatic routing:
  - Calendar read/create: `scheduling_agent` with `google_calendar`.
  - Tasks list/create/complete: `scheduling_agent` or assistant fast action with `google_tasks`.
  - Reminder create: `scheduling_agent` with `reminder_scheduler`, blocked for no delivery target.
  - Browser: `browser_agent` with `browser_tool`, Playwright backend.

## What Failed Or Felt Off

- Calendar update routing is not reliable from natural follow-up language. The system asks a sensible question, but it does not actually enter the confirmation/update path.
- Google Tasks due-date display was wrong for live Google date-only values before this pass. Fixed and retested.
- The coding flow created the file but did not execute it, then returned a blocked status. This is honest, but not yet the seamless Codex-style completion expected by `AGENTS.md`.
- The live calendar now shows duplicate `basketball practice` entries on Friday at 6 PM. I did not delete them during the audit because the environment already had prior smoke artifacts and deleting could remove a preexisting user event.

## Fix Made

Fixed Google Tasks date-only parsing in `integrations/tasks/google_client.py`. Google Tasks returns due dates as midnight UTC even though they are semantically date-only. The parser now preserves the Google due date in the configured scheduler timezone instead of converting midnight UTC to the previous local day.

Added a focused regression in `tests/test_scheduling_calendar.py` for `2026-04-29T00:00:00.000Z` staying on `2026-04-29`.

Live retest after restart:

- `add verify due date smoke to my tasks for tomorrow` -> `I added verify due date smoke to your tasks due tomorrow.`
- `what tasks do I have now?` -> `Your tasks: 1. verify due date smoke due tomorrow`
- `mark verify due date smoke done` -> completed successfully.

## Google Setup

Google Calendar and Google Tasks are live in this workspace: reads, creates, and task completion succeeded. Google setup is not currently blocking calendar/tasks live use.

Reminder delivery is blocked in local `/chat` because there is no live Slack delivery target attached to the request. The response was human-readable and did not expose raw setup variable names.

## Test Results

- `python -m pytest tests/test_assistant_feel_behavior.py tests/test_memory_recall.py tests/test_scheduling_calendar.py tests/test_reminders.py tests/test_short_term_continuity.py -q`
  - Result: `104 passed in 62.96s`
- `python -m pytest tests/test_memory_platform_v2.py tests/test_routing_stabilization.py -q`
  - Result: `25 passed in 5.06s`
- `python -m pytest -q`
  - Result: timed out after about 303 seconds. The final captured output showed `OSError: [Errno 22] Invalid argument` while flushing stdout after the timeout.

## Recommended Next Build

1. Fix natural calendar update continuity so `move that to 8` after a calendar event read/create enters the scheduling confirmation path, and `confirm` performs the update.
2. Tighten coding-agent completion for tiny script requests: create the file, run it, capture output, then return completed evidence.
3. Add a cleanup/test namespace strategy for live calendar smoke prompts so behavioral tests do not leave duplicate user-visible calendar events.
4. Keep expanding Personal Ops through the main operator voice, with email next after tasks per memory.

## Files Touched

- `docs/behavioral_smoke_audit.md`
- `integrations/tasks/google_client.py`
- `tests/test_scheduling_calendar.py`
- `workspace/created_items/hello_world.py`

