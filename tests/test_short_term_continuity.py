"""Short-term assistant continuity regressions."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.assistant import AssistantLayer
from core.conversation import ConversationalHandler
from core.fast_actions import FastActionHandler
from core.interaction_context import InteractionContext, bind_interaction_context
from core.models import AssistantDecision, RequestMode, ReminderStatus, TaskStatus
from core.operator_context import OperatorContextService
from core.orchestration_graph import SovereignOrchestrationGraph
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.calendar.google_provider import GoogleCalendarEvent, GoogleCalendarProvider
from integrations.calendar.service import CalendarService
from integrations.reminders.service import ReminderSchedulerService
from integrations.browser.runtime import BrowserRuntimeSupport
from integrations.gmail_client import GmailClient
from integrations.google_calendar_client import GoogleCalendarClient
from integrations.readiness import build_integration_readiness
from memory.memory_store import MemoryStore
from memory.types import ReminderRecord
from tests.test_reminders import FakeBackgroundScheduler, FakeOutboundAdapter
from tools.capability_manifest import build_capability_catalog


class _NoLlmClient:
    def is_configured(self) -> bool:
        return False


class _FakeCalendarProvider(GoogleCalendarProvider):
    def __init__(self, events: list[GoogleCalendarEvent] | None = None) -> None:
        self.events = events or []
        self.calls: list[tuple[datetime, datetime]] = []
        self.deleted: list[str] = []
        self.updated: list[dict[str, object]] = []

    def readiness_blockers(self) -> list[str]:
        return []

    def list_events(self, *, start: datetime, end: datetime, max_results: int = 20) -> list[GoogleCalendarEvent]:
        del max_results
        self.calls.append((start, end))
        return list(self.events)

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

    def update_event(
        self,
        *,
        event_id: str,
        updates: dict,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> GoogleCalendarEvent:
        del calendar_id, send_updates
        self.updated.append({"event_id": event_id, "updates": updates})
        return GoogleCalendarEvent(
            event_id=event_id,
            calendar_id="primary",
            title=str(updates.get("summary") or "Updated"),
            start=datetime(2026, 4, 27, 9, 0).astimezone(),
            end=datetime(2026, 4, 27, 10, 0).astimezone(),
        )


class ShortTermContinuityTests(unittest.TestCase):
    def _context(self, temp_dir: str) -> OperatorContextService:
        return OperatorContextService(
            openrouter_client=_NoLlmClient(),
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
            task_store=TaskStateStore(),
        )

    def _pending_reminder(self, store: MemoryStore, *, summary: str = "drink water") -> ReminderRecord:
        return store.upsert_reminder(
            reminder_id="rem-water",
            summary=summary,
            deliver_at=(datetime.now().astimezone() + timedelta(hours=1)).isoformat(),
            channel="D123",
            recipient="U123",
            delivery_channel="slack",
            status="pending",
            schedule_kind="one_time",
            timezone_name="America/New_York",
            source="test",
            metadata={},
        )

    def test_wait_what_reminder_then_cancel_it_resolves_last_mentioned_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            self._pending_reminder(store)
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            reminder_service = __import__(
                "integrations.reminders.service",
                fromlist=["ReminderSchedulerService"],
            ).ReminderSchedulerService(memory_store_instance=store)
            conversation = ConversationalHandler(
                openrouter_client=_NoLlmClient(),
                operator_context_service=context,
                reminder_service=reminder_service,
            )
            fast_actions = FastActionHandler(
                operator_context_service=context,
                reminder_service=reminder_service,
                openrouter_client=_NoLlmClient(),
            )

            reminder_reply = conversation.handle(
                "wait what reminder?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="test", should_use_tools=False),
            )
            canceled = fast_actions.handle(
                "cancel it please",
                AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
            )
            confirmed = fast_actions.handle(
                "confirm",
                AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
            )

        self.assertIn("drink water", reminder_reply.response)
        assert canceled is not None and confirmed is not None
        self.assertEqual(canceled.status, TaskStatus.BLOCKED)
        self.assertIn("confirm", canceled.response.lower())
        self.assertEqual(confirmed.status, TaskStatus.COMPLETED)
        self.assertIn("drink water", confirmed.response)
        self.assertEqual(store.list_reminders(statuses=("pending",)), [])

    def test_calendar_week_query_uses_runtime_date_without_asking_today(self) -> None:
        event = GoogleCalendarEvent(
            event_id="evt-1",
            calendar_id="primary",
            title="Planning",
            start=datetime(2026, 4, 27, 9, 0).astimezone(),
            end=datetime(2026, 4, 27, 10, 0).astimezone(),
        )
        provider = _FakeCalendarProvider([event])
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            assistant = AssistantLayer(openrouter_client=_NoLlmClient(), operator_context_service=context)
            handler = FastActionHandler(
                operator_context_service=context,
                calendar_service=CalendarService(provider=provider),
                openrouter_client=_NoLlmClient(),
            )
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=context,
                fast_action_handler=handler,
            )

            response = supervisor.handle_user_goal("what events do I have this week?")

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertNotIn("today's date", response.response.lower())
        self.assertNotIn("what is today's date", response.response.lower())
        self.assertIn("Planning", response.response)

    def test_user_answer_to_pending_followup_resumes_original_task(self) -> None:
        provider = _FakeCalendarProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            context.set_pending_question(
                original_user_intent="what events do I have this week?",
                missing_field="current_date",
                expected_answer_type="date",
                resume_target="calendar_read",
                tool_or_agent="scheduling_agent",
                question="What is today's date?",
            )
            assistant = AssistantLayer(openrouter_client=_NoLlmClient(), operator_context_service=context)
            handler = FastActionHandler(
                operator_context_service=context,
                calendar_service=CalendarService(provider=provider),
                openrouter_client=_NoLlmClient(),
            )
            supervisor = Supervisor(
                assistant_layer=assistant,
                operator_context_service=context,
                fast_action_handler=handler,
            )

            response = supervisor.handle_user_goal("Today is April 26, 2026.")

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertGreaterEqual(len(provider.calls), 1)
        self.assertIsNone(context.get_short_term_state().pending_question)

    def test_runtime_snapshot_includes_current_datetime_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "scheduler_timezone", "America/New_York"):
            context = self._context(temp_dir)
            snapshot = context.build_runtime_snapshot()

        self.assertIn("T", snapshot.current_datetime)
        self.assertEqual(snapshot.timezone, "America/New_York")
        self.assertRegex(snapshot.timezone_offset, r"^[+-]\d{2}:\d{2}$")
        self.assertIn("current_datetime:", snapshot.to_prompt_block())
        self.assertIn("timezone_offset:", snapshot.to_prompt_block())
        self.assertIn("short_term_interaction_state:", snapshot.to_prompt_block())

    def test_recurring_reminder_missing_time_resumes_and_schedules(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )
            handler = FastActionHandler(
                operator_context_service=context,
                reminder_service=service,
                openrouter_client=_NoLlmClient(),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True)

            with bind_interaction_context(InteractionContext(source="slack", channel_id="D123", user_id="U123")):
                first = handler.handle("remind me every morning", decision)
                second = handler.handle("8 AM", decision)

        assert first is not None and second is not None
        self.assertEqual(first.status, TaskStatus.BLOCKED)
        self.assertEqual(second.status, TaskStatus.COMPLETED)
        reminders = store.list_reminders(statuses=(ReminderStatus.PENDING,))
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].schedule_kind, "recurring")

    def test_calendar_delete_missing_id_resumes_to_session_scoped_confirmation(self) -> None:
        provider = _FakeCalendarProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            handler = FastActionHandler(
                operator_context_service=context,
                calendar_service=CalendarService(provider=provider),
                openrouter_client=_NoLlmClient(),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U1")):
                first = handler.handle("delete event", decision)
                second = handler.handle("evt-123", decision)
                pending = context.get_pending_confirmation("calendar_action")

        assert first is not None and second is not None and pending is not None
        self.assertEqual(first.status, TaskStatus.BLOCKED)
        self.assertEqual(second.status, TaskStatus.BLOCKED)
        self.assertEqual(pending["event_id"], "evt-123")
        self.assertIn("confirm", second.response.lower())

    def test_pending_confirmations_are_scoped_per_slack_user(self) -> None:
        provider = _FakeCalendarProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            handler = FastActionHandler(
                operator_context_service=context,
                calendar_service=CalendarService(provider=provider),
                openrouter_client=_NoLlmClient(),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U1")):
                handler.handle("delete calendar event evt-user-1", decision)
            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U2")):
                user_two = handler.handle("yes", decision)
            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U1")):
                user_one = handler.handle("yes", decision)

        self.assertIsNone(user_two)
        assert user_one is not None
        self.assertEqual(user_one.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.deleted, ["evt-user-1"])

    def test_slack_graph_thread_identity_is_stable_across_turns(self) -> None:
        graph = Supervisor().orchestration_graph
        context = InteractionContext(source="slack", channel_id="D123", user_id="U123")
        with bind_interaction_context(context):
            first = graph._invoke_config()["configurable"]["thread_id"]
            second = graph._invoke_config()["configurable"]["thread_id"]

        self.assertEqual(first, second)
        self.assertEqual(first, "slack:D123:U123")

    def test_fast_path_uses_short_term_referent_before_acting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            context.register_actionable_object(
                object_type="reminder",
                object_id="rem-1",
                summary="drink water",
                source="test",
            )
            handler = FastActionHandler(operator_context_service=context, openrouter_client=_NoLlmClient())

            self.assertTrue(handler._looks_like_cancel_reminder_request("cancel it please"))

    def test_open_loops_do_not_pollute_referent_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            context.remember_open_loop("Pending reminder: drink water", source="test")

            self.assertIsNone(context.resolve_recent_referent(object_type="reminder", pronoun_text="it"))

    def test_ordinal_and_ambiguous_referents_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            context.register_actionable_object(object_type="reminder", object_id="rem-1", summary="first reminder")
            context.register_actionable_object(object_type="reminder", object_id="rem-2", summary="second reminder")

            first = context.resolve_recent_referent(object_type="reminder", pronoun_text="the first one")
            second = context.resolve_recent_referent(object_type="reminder", pronoun_text="the second one")
            ambiguous = context.resolve_recent_referents(object_type="reminder", pronoun_text="it")

        assert first is not None and second is not None
        self.assertEqual(first.object_id, "rem-2")
        self.assertEqual(second.object_id, "rem-1")
        self.assertEqual(len(ambiguous), 2)

    def test_delete_that_after_calendar_read_uses_calendar_referent(self) -> None:
        event = GoogleCalendarEvent(
            event_id="evt-read",
            calendar_id="primary",
            title="Planning",
            start=datetime(2026, 4, 27, 9, 0).astimezone(),
            end=datetime(2026, 4, 27, 10, 0).astimezone(),
        )
        provider = _FakeCalendarProvider([event])
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            handler = FastActionHandler(
                operator_context_service=context,
                calendar_service=CalendarService(provider=provider),
                openrouter_client=_NoLlmClient(),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            read = handler.handle("what do I have today?", decision)
            delete = handler.handle("delete that", decision)

        assert read is not None and delete is not None
        self.assertEqual(delete.status, TaskStatus.BLOCKED)
        self.assertIn("Planning", delete.response)
        self.assertNotIn("evt-read", delete.response)

    def test_move_that_after_calendar_read_requires_human_confirmation(self) -> None:
        event = GoogleCalendarEvent(
            event_id="evt-move",
            calendar_id="primary",
            title="Study session",
            start=datetime(2026, 4, 27, 19, 0).astimezone(),
            end=datetime(2026, 4, 27, 20, 0).astimezone(),
        )
        provider = _FakeCalendarProvider([event])
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            handler = FastActionHandler(
                operator_context_service=context,
                calendar_service=CalendarService(provider=provider),
                openrouter_client=_NoLlmClient(),
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            read = handler.handle("what do I have today?", decision)
            move = handler.handle("move that to 8", decision)
            confirmed = handler.handle("confirm", decision)

        assert read is not None and move is not None and confirmed is not None
        self.assertEqual(move.status, TaskStatus.BLOCKED)
        self.assertIn("confirm", move.response.lower())
        self.assertIn("Study session", move.response)
        self.assertNotIn("evt-move", move.response)
        self.assertEqual(confirmed.status, TaskStatus.COMPLETED)
        self.assertEqual(provider.updated[0]["event_id"], "evt-move")
        updates = provider.updated[0]["updates"]
        moved_start = datetime.fromisoformat(updates["start"]["dateTime"])
        moved_end = datetime.fromisoformat(updates["end"]["dateTime"])
        self.assertEqual(moved_start.date(), event.start.date())
        self.assertEqual(moved_start.hour, 20)
        self.assertEqual(moved_end - moved_start, event.end - event.start)

    def test_stale_short_term_state_expires_without_durable_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)
            state = context.get_short_term_state()
            context.set_pending_question(
                original_user_intent="remind me every morning",
                missing_field="recurring_reminder_time",
                expected_answer_type="time",
                resume_target="reminder",
                question="What time?",
            )
            state.updated_at = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()

            context.cleanup_short_term_state()

        self.assertEqual(state.lifecycle_state, "expired")
        self.assertIsNone(state.pending_question)

    def test_browser_use_readiness_distinguishes_disabled_missing_sdk_and_live(self) -> None:
        with (
            patch.object(settings, "browser_use_enabled", False),
            patch.object(settings, "browser_use_api_key", None),
            patch(
                "integrations.readiness.detect_browser_runtime_support",
                return_value=BrowserRuntimeSupport(playwright_available=False, browser_use_sdk_available=False),
            ),
        ):
            disabled = build_integration_readiness()["integration:browser_use"]

        with (
            patch.object(settings, "browser_use_enabled", True),
            patch.object(settings, "browser_use_api_key", "key"),
            patch(
                "integrations.readiness.detect_browser_runtime_support",
                return_value=BrowserRuntimeSupport(playwright_available=False, browser_use_sdk_available=False),
            ),
        ):
            missing_sdk = build_integration_readiness()["integration:browser_use"]

        with (
            patch.object(settings, "browser_use_enabled", True),
            patch.object(settings, "browser_use_api_key", "key"),
            patch(
                "integrations.readiness.detect_browser_runtime_support",
                return_value=BrowserRuntimeSupport(playwright_available=False, browser_use_sdk_available=True),
            ),
        ):
            live = build_integration_readiness()["integration:browser_use"]

        self.assertEqual(disabled.status, "planned")
        self.assertEqual(missing_sdk.status, "unavailable")
        self.assertIn("BROWSER_USE_SDK", missing_sdk.missing_fields)
        self.assertEqual(live.status, "live")

    def test_gmail_readiness_distinguishes_credentials_token_and_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "gmail_credentials.json"
            token = Path(temp_dir) / "gmail_token.json"
            credentials.write_text("{}", encoding="utf-8")
            with (
                patch.object(settings, "gmail_enabled", True),
                patch.object(settings, "gmail_credentials_path", str(credentials)),
                patch.object(settings, "gmail_token_path", str(token)),
                patch("integrations.gmail_client._google_deps_available", return_value=True),
            ):
                credentials_only = GmailClient(runtime_settings=settings).readiness()
                token.write_text("{}", encoding="utf-8")
                token_ready = GmailClient(runtime_settings=settings).readiness()

        self.assertTrue(credentials_only.can_run_local_auth)
        self.assertFalse(credentials_only.live)
        self.assertTrue(any("TOKEN_PATH" in blocker for blocker in credentials_only.blockers))
        self.assertTrue(token_ready.configured)
        self.assertTrue(token_ready.live)

    def test_calendar_capability_wording_matches_read_write_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "credentials.json"
            token = Path(temp_dir) / "token.json"
            credentials.write_text("{}", encoding="utf-8")
            token.write_text("{}", encoding="utf-8")
            with (
                patch.object(settings, "google_calendar_enabled", True),
                patch.object(settings, "google_calendar_credentials_path", str(credentials)),
                patch.object(settings, "google_calendar_token_path", str(token)),
                patch.object(settings, "google_calendar_scopes", "https://www.googleapis.com/auth/calendar"),
                patch("integrations.google_calendar_client._google_deps_available", return_value=True),
            ):
                readiness = GoogleCalendarClient(runtime_settings=settings).readiness()
                snapshot = build_capability_catalog().snapshot_for("google_calendar")

        assert snapshot is not None
        self.assertTrue(readiness.live)
        self.assertEqual(snapshot.status, "live")
        self.assertIn("read/write", " ".join(snapshot.honesty_notes).lower())


if __name__ == "__main__":
    unittest.main()
