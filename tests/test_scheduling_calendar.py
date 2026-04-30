"""Scheduling / Personal Ops Google Calendar coverage."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agents.catalog import build_agent_catalog
from app.config import settings
from core.assistant import AssistantLayer
from core.context_assembly import ContextAssembler
from core.fast_actions import FastActionHandler
from core.models import AssistantDecision, RequestMode, SubTask, TaskStatus
from core.operator_context import OperatorContextService
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.calendar.google_provider import GoogleCalendarEvent, GoogleCalendarProvider
from integrations.calendar.parsing import parse_calendar_event_request, parse_calendar_query
from integrations.calendar.service import CalendarService
from integrations.google_calendar_client import GoogleCalendarClient
from integrations.tasks.google_client import NormalizedGoogleTask
from integrations.tasks.google_provider import GoogleTaskItem, GoogleTasksProvider
from integrations.tasks.service import GoogleTasksService
from memory.memory_store import MemoryStore
from tools.capability_manifest import build_capability_catalog


class FakeCalendarProvider(GoogleCalendarProvider):
    def __init__(self, *, blockers: list[str] | None = None, events: list[GoogleCalendarEvent] | None = None) -> None:
        self.blockers = blockers or []
        self.events = events or []
        self.created: list[dict[str, object]] = []
        self.updated: list[dict[str, object]] = []
        self.deleted: list[str] = []

    def readiness_blockers(self) -> list[str]:
        return list(self.blockers)

    def list_events(self, *, start: datetime, end: datetime, max_results: int = 20) -> list[GoogleCalendarEvent]:
        del start, end, max_results
        return list(self.events)

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        attendees: list[str] | None = None,
        send_updates: bool = False,
    ) -> GoogleCalendarEvent:
        del description
        self.created.append({"title": title, "attendees": attendees or [], "send_updates": send_updates})
        return GoogleCalendarEvent(
            event_id="evt-created",
            calendar_id="primary",
            title=title,
            start=start,
            end=end,
            attendees_count=len(attendees or []),
            attendees=tuple(attendees or []),
            html_link="https://calendar.google.com/event?eid=evt-created",
        )

    def update_event(
        self,
        *,
        event_id: str,
        updates: dict,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> GoogleCalendarEvent:
        del calendar_id
        if not updates:
            raise AssertionError("Calendar updates must not be empty.")
        self.updated.append({"event_id": event_id, "updates": updates, "send_updates": send_updates})
        return GoogleCalendarEvent(
            event_id=event_id,
            calendar_id="primary",
            title=str(updates.get("summary") or "Updated Event"),
            start=datetime(2026, 4, 27, 16, 0).astimezone(),
            end=datetime(2026, 4, 27, 17, 0).astimezone(),
        )

    def delete_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> dict[str, str]:
        del calendar_id, send_updates
        self.deleted.append(event_id)
        return {"event_id": event_id, "calendar_id": "primary", "source": "google_calendar", "status": "deleted"}


class FakeTasksProvider(GoogleTasksProvider):
    def __init__(self, *, blockers: list[str] | None = None, tasks: list[GoogleTaskItem] | None = None) -> None:
        self.blockers = blockers or []
        self.tasks = tasks or []
        self.created: list[dict[str, object]] = []
        self.completed: list[str] = []

    def readiness_blockers(self) -> list[str]:
        return list(self.blockers)

    def list_tasks(self, *, show_completed: bool = False, max_results: int = 20) -> list[GoogleTaskItem]:
        del show_completed, max_results
        return list(self.tasks)

    def create_task(self, *, title: str, due: datetime | None = None, notes: str | None = None) -> GoogleTaskItem:
        del notes
        self.created.append({"title": title, "due": due})
        task = GoogleTaskItem(
            task_id=f"task-{len(self.created)}",
            title=title,
            status="needsAction",
            due=due,
        )
        self.tasks.append(task)
        return task

    def complete_task(self, *, task_id: str, task_list_id: str | None = None) -> GoogleTaskItem:
        del task_list_id
        self.completed.append(task_id)
        existing = next((task for task in self.tasks if task.task_id == task_id), None)
        if existing is None:
            existing = GoogleTaskItem(task_id=task_id, title="Resolved task", status="needsAction")
        return GoogleTaskItem(
            task_id=existing.task_id,
            title=existing.title,
            status="completed",
            task_list_id=existing.task_list_id,
            due=existing.due,
            notes=existing.notes,
            completed=datetime.now().astimezone().isoformat(),
        )


class SchedulingCalendarTests(unittest.TestCase):
    def test_calendar_disabled_returns_setup_needed_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "google_calendar_enabled", False):
            with patch.object(settings, "google_calendar_credentials_path", str(Path(temp_dir) / "credentials.json")):
                with patch.object(settings, "google_calendar_token_path", str(Path(temp_dir) / "token.json")):
                    client = GoogleCalendarClient(runtime_settings=settings)
                    message = client.setup_needed_message()

        self.assertIn("Google Calendar setup is needed", message)
        self.assertNotIn("client_secret", message.lower())
        self.assertNotIn("refresh_token", message.lower())

    def test_credentials_and_token_paths_are_checked_without_exposing_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "credentials.json"
            token = Path(temp_dir) / "token.json"
            credentials.write_text('{"installed":{"client_secret":"do-not-print"}}', encoding="utf-8")
            with (
                patch.object(settings, "google_calendar_enabled", True),
                patch.object(settings, "google_calendar_credentials_path", str(credentials)),
                patch.object(settings, "google_calendar_token_path", str(token)),
            ):
                readiness = GoogleCalendarClient(runtime_settings=settings).readiness()

        self.assertFalse(readiness.live)
        self.assertTrue(any("TOKEN_PATH" in blocker for blocker in readiness.blockers))
        self.assertFalse(any("do-not-print" in blocker for blocker in readiness.blockers))

    def test_read_only_calendar_question_routes_to_scheduling_path(self) -> None:
        router = Router(openrouter_client=None)
        decision = router.assign_agent(
            SubTask(
                title="Check calendar",
                description="Read today's calendar",
                objective="What do I have today?",
            )
        )

        self.assertEqual(decision.agent_name, "scheduling_agent")

    def test_mixed_reminder_calendar_phrase_reads_calendar_not_reminder(self) -> None:
        event = GoogleCalendarEvent(
            event_id="evt-tomorrow",
            calendar_id="primary",
            title="Math review",
            start=datetime(2026, 4, 27, 9, 0).astimezone(),
            end=datetime(2026, 4, 27, 10, 0).astimezone(),
        )
        provider = FakeCalendarProvider(events=[event])
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(
                operator_context_service=operator_context,
                calendar_service=service,
            )
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            response = supervisor.handle_user_goal("remind me what's on my calendar tomorrow")

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertEqual(response.results[0].agent, "scheduling_agent")
        self.assertEqual(response.results[0].tool_name, "google_calendar")
        self.assertIn("Math review", response.response)
        self.assertEqual(provider.created, [])

    def test_read_today_and_tomorrow_schedule_are_human_readable(self) -> None:
        today_event = GoogleCalendarEvent(
            event_id="evt-today",
            calendar_id="primary",
            title="Standup",
            start=datetime(2026, 4, 27, 9, 0).astimezone(),
            end=datetime(2026, 4, 27, 9, 30).astimezone(),
        )
        tomorrow_event = GoogleCalendarEvent(
            event_id="evt-tomorrow",
            calendar_id="primary",
            title="Study group",
            start=datetime(2026, 4, 28, 19, 0).astimezone(),
            end=datetime(2026, 4, 28, 20, 0).astimezone(),
        )
        provider = FakeCalendarProvider(events=[today_event])
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(operator_context_service=operator_context, calendar_service=service)
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            today = supervisor.handle_user_goal("what do I have today?")
            provider.events = [tomorrow_event]
            tomorrow = supervisor.handle_user_goal("what do I have tomorrow?")

        self.assertEqual(today.status, TaskStatus.COMPLETED)
        self.assertIn("Standup", today.response)
        self.assertIn("today", today.response.lower())
        self.assertEqual(tomorrow.status, TaskStatus.COMPLETED)
        self.assertIn("Study group", tomorrow.response)
        self.assertIn("tomorrow", tomorrow.response.lower())

    def test_scheduling_instructions_are_always_in_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assembler = ContextAssembler(operator_context_service=service)
            operator_bundle = assembler.build("operator", user_message="hi")
            reviewer_bundle = assembler.build("reviewer", goal="check output")

        self.assertIn("Scheduling / Personal Ops Agent", operator_bundle.to_prompt_block())
        self.assertIn("modifying existing events requires confirmation", reviewer_bundle.to_prompt_block())

    def test_today_tomorrow_week_and_next_event_ranges_parse(self) -> None:
        now = datetime(2026, 4, 26, 12, 0).astimezone()
        today = parse_calendar_query("what do I have today?", now=now)
        tomorrow = parse_calendar_query("what's on my calendar tomorrow?", now=now)
        week = parse_calendar_query("what do I have this week?", now=now)
        next_event = parse_calendar_query("when is my next event?", now=now)
        availability = parse_calendar_query("am I free after school tomorrow?", now=now)
        at_seven = parse_calendar_query("do I have anything at 7?", now=now)
        friday_followup = parse_calendar_query("what about Friday?", now=now)

        assert today is not None and tomorrow is not None and week is not None and next_event is not None
        assert availability is not None and at_seven is not None and friday_followup is not None
        self.assertEqual(today.label, "today")
        self.assertEqual(tomorrow.day.date(), (now + timedelta(days=1)).date())
        self.assertEqual((week.end_day - week.day).days, 7)
        self.assertEqual(next_event.mode, "next")
        self.assertEqual(availability.mode, "availability")
        self.assertIsNotNone(availability.window_start)
        self.assertEqual(at_seven.mode, "availability")
        self.assertEqual(friday_followup.day.weekday(), 4)

    def test_natural_calendar_event_requests_parse_weekday_and_time_range(self) -> None:
        now = datetime(2026, 4, 27, 12, 0).astimezone()
        basketball = parse_calendar_event_request("add basketball practice Friday at 6", now=now)
        study = parse_calendar_event_request("schedule study session tomorrow from 7 to 8", now=now)
        dentist = parse_calendar_event_request("put dentist appointment next Tuesday at 3", now=now)
        sat_prep = parse_calendar_event_request("add an event for SAT prep Saturday morning", now=now)

        assert basketball is not None and study is not None and dentist is not None and sat_prep is not None
        self.assertEqual(basketball.title, "basketball practice")
        self.assertEqual(basketball.start.hour, 18)
        self.assertEqual(basketball.start.weekday(), 4)
        self.assertEqual(study.title, "study session")
        self.assertEqual(study.start.hour, 19)
        self.assertEqual(study.end.hour, 20)
        self.assertEqual(dentist.title, "dentist appointment")
        self.assertEqual(dentist.start.hour, 15)
        self.assertEqual(sat_prep.title, "SAT prep")
        self.assertEqual(sat_prep.start.hour, 9)

    def test_calendar_availability_and_next_event_wording(self) -> None:
        afternoon_event = GoogleCalendarEvent(
            event_id="evt-after-school",
            calendar_id="primary",
            title="Basketball practice",
            start=datetime(2026, 4, 28, 16, 0).astimezone(),
            end=datetime(2026, 4, 28, 17, 0).astimezone(),
        )
        provider = FakeCalendarProvider(events=[afternoon_event])
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            handler = FastActionHandler(operator_context_service=operator_context, calendar_service=service)
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            availability = handler.handle("am I free after school tomorrow?", decision)
            next_event = handler.handle("what's my next event?", decision)

        assert availability is not None and next_event is not None
        self.assertIn("Basketball practice", availability.response)
        self.assertIn("next calendar event", next_event.response.lower())

    def test_calendar_create_missing_time_asks_one_follow_up(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            response = handler.handle("add basketball practice Friday", decision)

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("what time", response.response.lower())
        self.assertEqual(provider.created, [])

    def test_calendar_create_missing_day_invalid_then_valid_follow_up(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(operator_context_service=operator_context, calendar_service=service)
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            missing_day = supervisor.handle_user_goal("add practice at 6")
            invalid = supervisor.handle_user_goal("to Mars")
            valid = supervisor.handle_user_goal("Friday")

        self.assertEqual(missing_day.status, TaskStatus.BLOCKED)
        self.assertIn("what day", missing_day.response.lower())
        self.assertEqual(invalid.status, TaskStatus.BLOCKED)
        self.assertIn("real day", invalid.response.lower())
        self.assertNotIn("resume_target", invalid.response.lower())
        self.assertNotIn("pending", invalid.response.lower())
        self.assertEqual(valid.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.created[0]["title"], "practice")

    def test_normalized_event_output_is_clean(self) -> None:
        raw = {
            "id": "evt-1",
            "summary": "Dentist",
            "start": {"dateTime": "2026-04-27T15:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-04-27T16:00:00-04:00", "timeZone": "America/New_York"},
            "description": "Bring forms and insurance card.",
            "attendees": [{"email": "safe@example.com"}],
            "htmlLink": "https://calendar.google.com/event?eid=evt-1",
        }

        normalized = GoogleCalendarClient(runtime_settings=settings).normalize_event(raw, calendar_id="primary")

        self.assertEqual(normalized.event_id, "evt-1")
        self.assertEqual(normalized.source, "google_calendar")
        self.assertEqual(normalized.attendees_count, 1)
        self.assertEqual(normalized.attendees, ("safe@example.com",))

    def test_simple_reminder_still_uses_reminder_scheduler_capability(self) -> None:
        policy = __import__("tools.tool_policy", fromlist=["build_tool_cost_policy"]).build_tool_cost_policy()
        decision = policy.assess("remind me in 10 minutes to stretch")

        self.assertIn("reminder_scheduler", decision.preferred_capability_ids)
        self.assertNotIn("google_calendar", decision.preferred_capability_ids)

    def test_calendar_create_with_attendee_requires_confirmation_then_executes(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            handler = FastActionHandler(
                operator_context_service=operator_context,
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            first = handler.handle("Add an event for tomorrow at 4 PM called Review with bob@example.com invite", decision)
            second = handler.handle("confirm", decision)

        assert first is not None and second is not None
        self.assertEqual(first.status, TaskStatus.BLOCKED)
        self.assertIn("confirm", first.response.lower())
        self.assertEqual(second.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.created[0]["attendees"], ["bob@example.com"])

    def test_simple_natural_calendar_create_executes_without_extra_confirmation(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(operator_context_service=operator_context, calendar_service=service)
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            response = supervisor.handle_user_goal("add basketball practice Friday at 6")

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertIn("basketball practice", response.response.lower())
        self.assertEqual(provider.created[0]["title"], "basketball practice")
        self.assertEqual(provider.created[0]["attendees"], [])

    def test_delete_and_update_require_confirmation(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            delete_prompt = handler.handle("delete calendar event evt-1", decision)
            delete_confirmed = handler.handle("yes", decision)
            update_prompt = handler.handle("change event evt-2 title to Updated Review", decision)

        assert delete_prompt is not None and delete_confirmed is not None and update_prompt is not None
        self.assertEqual(delete_prompt.status, TaskStatus.BLOCKED)
        self.assertEqual(delete_confirmed.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.deleted, ["evt-1"])
        self.assertEqual(update_prompt.status, TaskStatus.BLOCKED)
        self.assertIn("confirm", update_prompt.response.lower())
        pending = handler.operator_context.get_pending_confirmation("calendar_action")
        assert pending is not None
        self.assertEqual(pending["updates"], {"summary": "Updated Review"})

    def test_calendar_update_valid_fields_confirm_then_executes_non_empty_payload(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            prompt = handler.handle("change event evt-2 title to Updated Review", decision)
            confirmed = handler.handle("confirm", decision)

        assert prompt is not None and confirmed is not None
        self.assertEqual(prompt.status, TaskStatus.BLOCKED)
        self.assertEqual(confirmed.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.updated[0]["event_id"], "evt-2")
        self.assertEqual(provider.updated[0]["updates"], {"summary": "Updated Review"})
        self.assertEqual(confirmed.results[0].agent, "scheduling_agent")

    def test_calendar_update_ambiguous_fields_asks_clarification(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            response = handler.handle("update event evt-2", decision)

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("what should i update", response.response.lower())
        self.assertEqual(provider.updated, [])

    def test_title_based_calendar_match_ambiguity_asks_clarification(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            operator_context.register_actionable_object(
                object_type="calendar_event",
                object_id="evt-1",
                summary="Review at 3:00 PM",
                source="google_calendar",
            )
            operator_context.register_actionable_object(
                object_type="calendar_event",
                object_id="evt-2",
                summary="Review at 5:00 PM",
                source="google_calendar",
            )
            handler = FastActionHandler(
                operator_context_service=operator_context,
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            response = handler.handle("move review to 8", decision)

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("which one", response.response.lower())
        self.assertEqual(provider.updated, [])

    def test_supervisor_routes_referent_calendar_update_after_calendar_read(self) -> None:
        event = GoogleCalendarEvent(
            event_id="evt-move",
            calendar_id="primary",
            title="Study session",
            start=datetime(2026, 4, 28, 19, 0).astimezone(),
            end=datetime(2026, 4, 28, 20, 0).astimezone(),
        )
        provider = FakeCalendarProvider(events=[event])
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(operator_context_service=operator_context, calendar_service=service)
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            read = supervisor.handle_user_goal("what do I have today?")
            move = supervisor.handle_user_goal("move that to 8")

        self.assertEqual(read.status, TaskStatus.COMPLETED)
        self.assertEqual(move.status, TaskStatus.BLOCKED)
        self.assertEqual(move.planner_mode, "fast_action")
        self.assertIn("confirm", move.response.lower())
        self.assertIn("Study session", move.response)
        self.assertEqual(provider.updated, [])

    def test_scheduling_agent_owns_calendar_read_interpretation(self) -> None:
        handler = FastActionHandler(calendar_service=CalendarService(provider=FakeCalendarProvider()))

        self.assertTrue(handler.scheduling_agent.is_calendar_read_request("what do I have today?"))
        self.assertEqual(handler._looks_like_calendar_read_request("what do I have today?"), True)

    def test_calendar_delete_without_clear_target_asks_for_identifier(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            response = handler.handle("cancel calendar event", decision)

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("which", response.response.lower())
        self.assertEqual(provider.deleted, [])

    def test_missing_calendar_setup_is_human_readable_without_backend_jargon(self) -> None:
        provider = FakeCalendarProvider(
            blockers=["GOOGLE_CALENDAR_TOKEN_PATH does not exist yet; run the local OAuth flow once."]
        )
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(operator_context_service=operator_context, calendar_service=service)
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            response = supervisor.handle_user_goal("what do I have today?")

        self.assertEqual(response.status, TaskStatus.BLOCKED)
        lowered = response.response.lower()
        self.assertIn("google calendar setup is needed", lowered)
        self.assertIn("saved google calendar access", lowered)
        for marker in ("google_calendar_token_path", "oauth", "runtime", "adapter"):
            self.assertNotIn(marker, lowered)

    def test_calendar_service_rejects_empty_update_payload(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)

        result = service.update_event(event_id="evt-2", updates={})

        self.assertFalse(result.success)
        self.assertIn("No calendar update fields", result.blockers[0])
        self.assertEqual(provider.updated, [])

    def test_empty_pending_calendar_update_does_not_claim_success(self) -> None:
        provider = FakeCalendarProvider()
        service = CalendarService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            handler = FastActionHandler(
                operator_context_service=operator_context,
                calendar_service=service,
            )
            operator_context.set_pending_confirmation(
                "calendar_action",
                {"action": "update", "event_id": "evt-2", "updates": {}},
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            response = handler.handle("yes", decision)

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("what should i update", response.response.lower())
        self.assertEqual(provider.updated, [])

    def test_google_tasks_capability_is_owned_by_scheduling_agent(self) -> None:
        catalog = build_capability_catalog()
        tasks = catalog.snapshot_for("google_tasks")

        self.assertIsNotNone(tasks)
        assert tasks is not None
        self.assertIn(tasks.status, {"scaffolded", "unavailable", "configured_but_disabled", "live"})
        self.assertEqual(tasks.owner_agent, "scheduling_agent")

    def test_google_tasks_list_create_due_today_and_complete_referent(self) -> None:
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        provider = FakeTasksProvider(
            tasks=[
                GoogleTaskItem(task_id="task-1", title="Math homework", status="needsAction", due=today),
                GoogleTaskItem(task_id="task-2", title="Pack gym bag", status="needsAction"),
            ]
        )
        service = GoogleTasksService(provider=provider)
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(
                operator_context_service=operator_context,
                tasks_service=service,
            )
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            listed = supervisor.handle_user_goal("what tasks do I have?")
            done = supervisor.handle_user_goal("complete the second one")
            due_today = supervisor.handle_user_goal("what tasks are due today?")
            created = supervisor.handle_user_goal("add finish math homework to my tasks for tomorrow")
            created_complete_title = supervisor.handle_user_goal("add complete English worksheet to my tasks")

        self.assertEqual(listed.status, TaskStatus.COMPLETED)
        self.assertIn("1. Math homework", listed.response)
        self.assertIn("2. Pack gym bag", listed.response)
        self.assertEqual(due_today.status, TaskStatus.COMPLETED)
        self.assertIn("Math homework", due_today.response)
        self.assertNotIn("Pack gym bag", due_today.response)
        self.assertEqual(created.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.created[0]["title"], "finish math homework")
        created_due = provider.created[0]["due"]
        assert isinstance(created_due, datetime)
        self.assertEqual(created_due.astimezone().date(), (datetime.now().astimezone() + timedelta(days=1)).date())
        self.assertEqual(created_complete_title.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.created[1]["title"], "complete English worksheet")
        self.assertEqual(done.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.completed[-1], "task-2")
        self.assertIn("Pack gym bag", done.response)

    def test_google_tasks_date_only_due_preserves_google_due_date(self) -> None:
        task = NormalizedGoogleTask(
            task_id="task-1",
            title="Read chapter 4",
            status="needsAction",
            due="2026-04-29T00:00:00.000Z",
        )

        assert task.due_datetime is not None
        self.assertEqual(task.due_datetime.date().isoformat(), "2026-04-29")

    def test_google_tasks_completion_without_referent_asks_which_task(self) -> None:
        provider = FakeTasksProvider(
            tasks=[
                GoogleTaskItem(task_id="task-1", title="Math homework", status="needsAction"),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            handler = FastActionHandler(
                operator_context_service=operator_context,
                tasks_service=GoogleTasksService(provider=provider),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="tasks", should_use_tools=True)

            response = handler.handle("mark that done", decision)

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("which task", response.response.lower())
        self.assertEqual(provider.completed, [])

    def test_google_tasks_ambiguous_referent_requires_clarification(self) -> None:
        provider = FakeTasksProvider(
            tasks=[
                GoogleTaskItem(task_id="task-1", title="Math homework", status="needsAction"),
                GoogleTaskItem(task_id="task-2", title="English homework", status="needsAction"),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            handler = FastActionHandler(
                operator_context_service=operator_context,
                tasks_service=GoogleTasksService(provider=provider),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="tasks", should_use_tools=True)

            listed = handler.handle("what tasks do I have?", decision)
            response = handler.handle("mark that done", decision)

        assert listed is not None and response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("which", response.response.lower())
        self.assertEqual(provider.completed, [])

    def test_google_tasks_missing_setup_is_human_readable_without_backend_jargon(self) -> None:
        provider = FakeTasksProvider(
            blockers=["GOOGLE_TASKS_TOKEN_PATH does not exist yet; run the local OAuth flow once."]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            handler = FastActionHandler(
                operator_context_service=operator_context,
                tasks_service=GoogleTasksService(provider=provider),
            )
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
                fast_action_handler=handler,
            )

            response = supervisor.handle_user_goal("what tasks do I have?")

        self.assertEqual(response.status, TaskStatus.BLOCKED)
        lowered = response.response.lower()
        self.assertIn("google tasks setup is needed", lowered)
        self.assertIn("saved google tasks access", lowered)
        for marker in ("google_tasks_token_path", "oauth", "runtime", "adapter"):
            self.assertNotIn(marker, lowered)

    def test_thanks_after_calendar_action_stays_assistant_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            operator_context = OperatorContextService(
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )
            assistant = AssistantLayer(openrouter_client=None, operator_context_service=operator_context)
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=operator_context,
            )

            response = supervisor.handle_user_goal("thanks")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "conversation_fast_path")

    def test_agent_catalog_uses_scheduling_agent_not_calendar_only_agent(self) -> None:
        catalog = build_agent_catalog()

        self.assertIsNotNone(catalog.by_name("scheduling_agent"))
        self.assertIsNone(catalog.by_name("calendar_agent"))


if __name__ == "__main__":
    unittest.main()
