# Scheduling Agent Readiness

## Architecture

Project Sovereign keeps one user-facing CEO/operator. Scheduling work is delegated underneath that operator:

CEO / Supervisor -> Life Assistant / Personal Ops -> Scheduling Agent -> calendar, reminder, and task tools.

The CEO should recognize scheduling intent and delegate it. It should not become a giant date parser. The Scheduling Agent owns calendar/reminder/task interpretation, safety checks, short-term references, and tool evidence. Python code handles validation, persistence, provider adapters, confirmation state, and setup blockers.

## Scheduling Primitive Differences

- Calendar event: a scheduled time block, usually with a start and end time.
- Reminder: a notification to send later or on a recurring cadence.
- Task: a to-do item, optionally with a due date, that can be listed or marked complete.

## Supported Calendar Commands

Calendar reads:
- "what do I have today?"
- "what do I have tomorrow?"
- "what do I have this week?"
- "what's my next event?"
- "what about Friday?"
- "am I free after school tomorrow?"
- "do I have anything at 7?"

Calendar creates:
- "add basketball practice Friday at 6"
- "schedule study session tomorrow from 7 to 8"
- "put dentist appointment next Tuesday at 3"
- "add an event for SAT prep Saturday morning"

Calendar updates and deletes:
- "move that to 8"
- "reschedule my study session to tomorrow at 6"
- "delete that event"
- "cancel basketball practice"

Simple, clear personal events can be created directly when title, date, and time are present. Events with attendees, invites, external updates, updates to existing events, and deletes require confirmation.

## Supported Reminder Commands

Reminder scheduling remains part of the same Scheduling Agent surface:
- "remind me to study at 7"
- "remind me every weekday at 8 to check assignments"
- "what reminders do I have?"
- "cancel that reminder"

Recurring reminders are supported through the existing reminder scheduler when the scheduler and outbound delivery are configured. If a recurring request is missing a time, Sovereign asks one concise follow-up.

## Supported Task Commands

Google Tasks remains under the Scheduling Agent, not a separate user-facing task agent.

Task listing:
- "what tasks do I have?"
- "show my tasks"
- "what tasks are due today?"

Task creation:
- "add finish math homework to my tasks"
- "create a task finish math homework due today"
- "add task submit permission slip due tomorrow"

Task completion:
- "mark that done"
- "complete the second one"
- "mark finish math homework complete"

Task completion is direct when the task referent is clear. If "that" or "the second one" could mean more than one task, Sovereign asks one clarification. Bulk deletes and full task deletion are intentionally not implemented in this pass.

## Setup Requirements

Calendar reads and writes need Google Calendar connected:
- Google Calendar enabled
- Google Calendar credentials file present
- saved Google Calendar access token present
- Google Calendar Python dependencies installed

User-facing replies should explain this plainly, for example: "I need you to connect Google Calendar before I can read events." Technical details can stay in logs and structured blockers.

Reminders need:
- scheduler backend enabled
- reminders enabled
- outbound delivery target, currently Slack for the MVP

Google Tasks needs:
- Google Tasks enabled
- Google Tasks credentials file present
- saved Google Tasks access present
- Google Tasks Python dependencies installed

## Confirmation Policy

No confirmation needed:
- read calendar
- create a clear low-risk personal event with no attendees or invite/send-update request
- create ordinary reminders
- list tasks
- create ordinary tasks
- mark one clear task complete

Confirmation required:
- delete/cancel a calendar event
- update/reschedule an existing calendar event
- create a calendar event with attendees or invites
- send calendar updates to guests
- future destructive or bulk task changes

Normal replies should not expose raw calendar event IDs or task IDs. They can use natural references like "Study session at 7:00 PM" or "finish math homework."

## Short-Term References

Calendar events returned from reads become short-term referents. Reminder creations, reminder list results, and task list results also become referents. This supports:
- "delete it"
- "move that to 8"
- "actually cancel that"
- "the second one"
- "mark that done"
- "complete the second one"
- "what about Friday?"

Ambiguous referents ask for clarification. Destructive calendar actions still require confirmation after the referent is resolved. Completing a single clear task does not require extra confirmation.

## Remaining Gaps

- Calendar matching by title for old events is still limited; the strongest path is recent referents from reads or creations.
- "Move it to tomorrow instead" asks for a time unless a future pass preserves original event duration/time metadata in referents.
- Availability phrases use lightweight windows, such as "after school" as 3 PM through end of day.
- Task deletion and bulk task cleanup are intentionally out of scope.

## Recommended Next Build

Next pass: add stronger event search and disambiguation for title-based updates/deletes, then consider task deletion with confirmation once list/create/complete behavior is stable.
