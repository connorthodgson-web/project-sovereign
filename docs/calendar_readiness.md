# Google Calendar Assistant Readiness

## Current Architecture

Calendar work is owned by the Scheduling / Personal Ops path.

- `core.assistant.AssistantLayer` classifies clear calendar reads and small calendar actions as `ACT`.
- `core.fast_actions.FastActionHandler` handles lightweight calendar reads, creates, guarded updates, and guarded deletes without the heavier planning loop.
- `agents.scheduling_agent.SchedulingPersonalOpsAgent` delegates calendar operations to `CalendarService`.
- `integrations.calendar.service.CalendarService` provides assistant-safe success/blocker results.
- `integrations.calendar.google_provider.GoogleCalendarProvider` adapts the Google client into normalized events.
- `integrations.google_calendar_client.GoogleCalendarClient` owns Google Calendar API access, readiness checks, token loading, event normalization, and local sign-in setup.

The user-facing path should stay natural: answer schedule questions in plain language, create obvious low-risk events directly, and ask for confirmation only when modifying/deleting events or when guests/updates are involved.

## Setup Requirements

To use live Google Calendar:

- Set `GOOGLE_CALENDAR_ENABLED=true`.
- Install the Google Calendar Python packages from `requirements.txt`.
- Put the Google Calendar credentials JSON at `GOOGLE_CALENDAR_CREDENTIALS_PATH` (default: `secrets/credentials.json`).
- Run the local Google sign-in flow once so `GOOGLE_CALENDAR_TOKEN_PATH` exists (default: `secrets/token.json`).
- Set `CALENDAR_ID` if the target calendar is not `primary`.
- Keep credentials and saved access files in `secrets/`; do not store raw credential contents in memory or chat logs.

When setup is missing, Sovereign should say what is missing in human terms, for example that Google Calendar is not enabled, the credentials file is missing, or saved Google Calendar access has not been created yet.

## Supported Commands

Calendar reads:

- `what do I have today?`
- `what do I have tomorrow?`
- `what do I have this week?`
- `when is my next event?`
- `what's on my calendar Friday?`

Calendar creates:

- `add basketball practice Friday at 6`
- `schedule study session tomorrow from 7 to 8`
- `add an event for tomorrow at 4 PM called Review`

Creates with attendees or guest notifications ask for confirmation first. Simple personal events without attendees are created directly when Google Calendar is configured.

Calendar updates/deletes:

- `change event evt-123 title to Updated Review`
- `move event evt-123 to tomorrow at 4`
- `delete calendar event evt-123`

Updates and deletes require a clear event id or a recent unambiguous calendar referent, then confirmation before execution.

## Remaining Gaps

- Natural update/delete by event title needs safer search-and-disambiguation before it should execute.
- Recurring calendar events are not parsed from natural language yet.
- All-day events are normalized but not first-class in creation parsing.
- Timezone selection is global through `SCHEDULER_TIMEZONE`; per-user timezone memory can be added later.
- Calendar availability/free-busy queries are not implemented.
