"""Coverage for outbound Slack delivery and live reminder scheduling."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agents.reminder_agent import ReminderSchedulerAgent
from app.config import settings
from core.assistant import AssistantLayer
from core.conversation import ConversationalHandler
from core.fast_actions import FastActionHandler
from core.interaction_context import InteractionContext, bind_interaction_context
from core.models import AssistantDecision, RequestMode, ReminderStatus, SubTask
from core.operator_context import OperatorContextService
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.calendar.google_provider import GoogleCalendarEvent, GoogleCalendarProvider
from integrations.calendar.parsing import parse_calendar_event_request, parse_calendar_query
from integrations.calendar.service import CalendarService
from integrations.messaging.contracts import MessagingRequest, MessagingResult
from integrations.readiness import build_integration_readiness
from integrations.reminders.adapter import APSchedulerReminderAdapter
from integrations.reminders.parsing import (
    normalize_reminder_summary_text,
    parse_one_time_reminder_request,
    parse_one_time_reminder_request_with_fallback,
)
from integrations.reminders.recurring import parse_recurring_reminder_request
from integrations.reminders.service import ReminderSchedulerService
from integrations.slack_outbound import SlackOutboundAdapter
from memory.memory_store import MemoryStore


class FakeReminderOpenRouterClient:
    def __init__(self, response: str, *, configured: bool = True) -> None:
        self.response = response
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured

    def prompt(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        label: str | None = None,
        context=None,
    ) -> str:
        del prompt, system_prompt, label, context
        return self.response


class FakeBackgroundScheduler:
    def __init__(self, timezone=None) -> None:
        self.timezone = timezone
        self.jobs: dict[str, dict[str, object]] = {}
        self.started = False

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = False) -> None:
        del wait
        self.started = False

    def add_job(
        self,
        func,
        *,
        trigger: str,
        id: str,
        replace_existing: bool,
        args: list[str],
        misfire_grace_time: int,
        run_date=None,
        **kwargs,
    ) -> None:
        del func, replace_existing, misfire_grace_time
        self.jobs[id] = {"run_date": run_date, "args": args, "kwargs": kwargs, "trigger": trigger}

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


class FakeOutboundAdapter:
    def __init__(self, *, blockers: list[str] | None = None) -> None:
        self.blockers = blockers or []
        self.sent_messages: list[MessagingRequest] = []

    def readiness_blockers(self) -> list[str]:
        return list(self.blockers)

    def send(self, request: MessagingRequest) -> MessagingResult:
        self.sent_messages.append(request)
        if self.blockers:
            return MessagingResult(
                success=False,
                summary="Fake outbound delivery is blocked.",
                blockers=self.blockers,
            )
        return MessagingResult(
            success=True,
            summary="Delivered.",
            delivery_id="slack-ts-123",
            metadata={"target_channel": request.recipient},
        )


class FakeSlackWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []

    def chat_postMessage(self, *, channel: str, text: str) -> dict[str, str | bool]:
        self.posts.append({"channel": channel, "text": text})
        return {"ok": True, "channel": channel, "ts": "1710000000.000100"}


class FakeCalendarProvider(GoogleCalendarProvider):
    def __init__(
        self,
        *,
        events: list[GoogleCalendarEvent] | None = None,
        blockers: list[str] | None = None,
        created_event: GoogleCalendarEvent | None = None,
    ) -> None:
        self._events = events or []
        self._blockers = blockers or []
        self._created_event = created_event or GoogleCalendarEvent(
            event_id="evt-123",
            title="Math Review",
            start=datetime(2026, 4, 24, 16, 0).astimezone(),
            end=datetime(2026, 4, 24, 17, 0).astimezone(),
            html_link=None,
        )

    def readiness_blockers(self) -> list[str]:
        return list(self._blockers)

    def list_events(self, *, start: datetime, end: datetime, max_results: int = 20) -> list[GoogleCalendarEvent]:
        del start, end, max_results
        return list(self._events)

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
    ) -> GoogleCalendarEvent:
        del description
        return GoogleCalendarEvent(
            event_id=self._created_event.event_id,
            title=title,
            start=start,
            end=end,
            html_link=self._created_event.html_link,
        )


class ReminderReadinessTests(unittest.TestCase):
    def test_slack_outbound_readiness_reports_live_when_bot_token_exists(self) -> None:
        with (
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch.object(settings, "slack_enabled", True),
        ):
            readiness = build_integration_readiness()

        self.assertEqual(readiness["integration:slack_outbound"].status, "live")

    def test_slack_outbound_adapter_returns_structured_delivery_metadata(self) -> None:
        adapter = SlackOutboundAdapter(
            runtime_settings=settings,
            client=FakeSlackWebClient(),
        )
        with (
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch.object(settings, "slack_enabled", True),
        ):
            result = adapter.send(
                MessagingRequest(
                    channel="slack",
                    recipient="D123",
                    message="Reminder: check the deployment",
                    metadata={"channel_id": "D123"},
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.delivery_id, "1710000000.000100")
        self.assertEqual(result.metadata["target_channel"], "D123")


class ReminderFlowTests(unittest.TestCase):
    def test_reminder_summary_cleanup_strips_edge_filler_words(self) -> None:
        self.assertEqual(
            normalize_reminder_summary_text("drink water, please"),
            "drink water",
        )
        self.assertEqual(
            normalize_reminder_summary_text("please drink water plz"),
            "drink water",
        )
        self.assertEqual(
            normalize_reminder_summary_text("drink water for me real quick"),
            "drink water",
        )

    def test_explicit_scheduled_reminder_stays_on_action_path_even_with_llm_available(self) -> None:
        assistant = AssistantLayer(
            openrouter_client=FakeReminderOpenRouterClient(
                response='{"mode":"ANSWER","reasoning":"Treat it like casual conversation.","should_use_tools":false}'
            )
        )

        decision = assistant.decide("remind me yesterday to drink water")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)

    def test_parser_accepts_minute_abbreviation_and_that_connector(self) -> None:
        now = datetime(2026, 4, 21, 12, 0).astimezone()

        parsed = parse_one_time_reminder_request(
            "remind me in 1 min that I'm cool",
            now=now,
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.summary, "I'm cool")
        self.assertEqual(int((parsed.deliver_at - now).total_seconds()), 60)

    def test_parser_accepts_common_abbreviation_variants(self) -> None:
        now = datetime(2026, 4, 21, 12, 0).astimezone()

        hours = parse_one_time_reminder_request("remind me in 2 hrs to call mom", now=now)
        seconds = parse_one_time_reminder_request("remind me after 30 secs to stand up", now=now)

        self.assertIsNotNone(hours)
        self.assertIsNotNone(seconds)
        assert hours is not None
        assert seconds is not None
        self.assertEqual(int((hours.deliver_at - now).total_seconds()), 7200)
        self.assertEqual(int((seconds.deliver_at - now).total_seconds()), 30)

    def test_parser_trims_polite_filler_from_reminder_summary(self) -> None:
        now = datetime(2026, 4, 21, 12, 0).astimezone()

        parsed_please = parse_one_time_reminder_request(
            "remind me in 2 minutes to drink water, please",
            now=now,
        )
        parsed_plz = parse_one_time_reminder_request(
            "remind me in 2 minutes to drink water plz",
            now=now,
        )

        assert parsed_please is not None
        assert parsed_plz is not None
        self.assertEqual(parsed_please.summary, "drink water")
        self.assertEqual(parsed_plz.summary, "drink water")
        self.assertEqual(int((parsed_please.deliver_at - now).total_seconds()), 120)
        self.assertEqual(int((parsed_plz.deliver_at - now).total_seconds()), 120)

    def test_operator_context_open_loop_uses_cleaned_reminder_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Remind me later to check the deployment, please")

            open_loops = [item.summary for item in store.snapshot().open_loops]

        self.assertIn("check the deployment", open_loops)
        self.assertNotIn("check the deployment, please", open_loops)

    def test_operator_context_open_loop_drops_schedule_phrase_for_relative_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Remind me in two minutes to drink water, please")

            open_loops = [item.summary for item in store.snapshot().open_loops]

        self.assertIn("drink water", open_loops)
        self.assertNotIn("in two minutes to drink water", open_loops)

    def test_invalid_past_time_stays_on_fast_path_with_honest_blocker(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            operator_context = OperatorContextService(
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            reminder_service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )
            supervisor = Supervisor(
                operator_context_service=operator_context,
                fast_action_handler=FastActionHandler(
                    operator_context_service=operator_context,
                    reminder_service=reminder_service,
                    openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
                ),
            )

            with bind_interaction_context(
                InteractionContext(source="slack", channel_id="D123", user_id="U123")
            ):
                response = supervisor.handle_user_goal("remind me yesterday to drink water")

        self.assertEqual(response.status.value, "blocked")
        self.assertEqual(response.planner_mode, "fast_action")
        self.assertIn("couldn't schedule that reminder yet", response.response.lower())

    def test_recurring_reminder_request_in_conversation_path_asks_for_missing_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            operator_context = OperatorContextService(
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            handler = ConversationalHandler(
                openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=operator_context,
            )

            response = handler.handle(
                "Remind me every day to play basketball",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="status", should_use_tools=False),
            )

        self.assertIn("what time", response.response.lower())

    def test_parser_uses_llm_fallback_when_deterministic_path_fails(self) -> None:
        now = datetime(2026, 4, 21, 12, 0).astimezone()
        llm = FakeReminderOpenRouterClient(
            response=(
                '{"summary":"submit the form","deliver_at":"2026-04-21T12:45:00-04:00",'
                '"schedule_phrase":"in 45 minutes","confidence":0.93}'
            )
        )

        outcome = parse_one_time_reminder_request_with_fallback(
            "Can you ping me 45 from now about submitting the form?",
            now=now,
            openrouter_client=llm,
        )

        self.assertIsNotNone(outcome.parsed)
        self.assertTrue(outcome.attempted_llm_fallback)
        assert outcome.parsed is not None
        self.assertEqual(outcome.parsed.parser, "llm")
        self.assertEqual(outcome.parsed.summary, "submit the form")

    def test_parser_returns_specific_failure_when_both_paths_fail(self) -> None:
        llm = FakeReminderOpenRouterClient(
            response='{"summary":"","deliver_at":"","schedule_phrase":"","confidence":0.1}'
        )

        outcome = parse_one_time_reminder_request_with_fallback(
            "remind me sometime maybe",
            openrouter_client=llm,
        )

        self.assertIsNone(outcome.parsed)
        self.assertIn("couldn't", (outcome.failure_reason or "").lower())

    def test_router_sends_reminder_subtasks_to_reminder_agent(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            router = Router(openrouter_client=None)
            subtask = SubTask(
                title="Schedule reminder",
                description="Schedule a reminder for later",
                objective="Remind the user in 10 minutes to check the deployment.",
            )

            decision = router.assign_agent(subtask)

            self.assertEqual(decision.agent_name, "scheduling_agent")

    def test_supervisor_schedules_and_delivers_reminder_with_operator_visibility(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            outbound = FakeOutboundAdapter()
            reminder_service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=outbound,
            )
            reminder_agent = ReminderSchedulerAgent(
                reminder_adapter=APSchedulerReminderAdapter(service=reminder_service)
            )
            operator_context = OperatorContextService(
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            router = Router(openrouter_client=None, reminder_agent=reminder_agent)
            supervisor = Supervisor(
                router=router,
                operator_context_service=operator_context,
            )

            with bind_interaction_context(
                InteractionContext(source="slack", channel_id="D123", user_id="U123")
            ):
                response = supervisor.handle_user_goal(
                    "Remind me in 10 minutes to check the deployment."
                )

            reminders = store.list_reminders(statuses=(ReminderStatus.PENDING,))
            self.assertEqual(response.status.value, "completed")
            self.assertIn("i'll remind you", response.response.lower())
            self.assertEqual(len(reminders), 1)

            reminder_service._deliver_reminder_job(reminders[0].reminder_id)
            delivered = store.get_reminder(reminders[0].reminder_id)
            self.assertIsNotNone(delivered)
            self.assertEqual(delivered.status, ReminderStatus.DELIVERED.value)
            self.assertEqual(outbound.sent_messages[0].recipient, "D123")

            handler = ConversationalHandler(
                operator_context_service=operator_context,
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
            )
            pending_response = handler.handle(
                "What reminders do I have?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="status", should_use_tools=False),
            )
            delivered_response = handler.handle(
                "Did you remind me already?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="status", should_use_tools=False),
            )

            self.assertIn("don't have any pending reminders", pending_response.response.lower())
            self.assertIn("latest delivered reminder", delivered_response.response.lower())
            reminder_service.shutdown()

    def test_reminder_request_stays_honest_when_delivery_is_not_configured(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", False),
            patch.object(settings, "slack_bot_token", None),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            reminder_service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(
                    blockers=["Slack outbound delivery is disabled in this runtime."]
                ),
            )
            reminder_agent = ReminderSchedulerAgent(
                reminder_adapter=APSchedulerReminderAdapter(service=reminder_service)
            )
            operator_context = OperatorContextService(
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            router = Router(openrouter_client=None, reminder_agent=reminder_agent)
            supervisor = Supervisor(
                router=router,
                operator_context_service=operator_context,
            )

            with bind_interaction_context(
                InteractionContext(source="slack", channel_id="D123", user_id="U123")
            ):
                response = supervisor.handle_user_goal(
                    "Remind me in 10 minutes to check the deployment."
                )

            self.assertEqual(response.status.value, "blocked")
            self.assertIn("slack outbound delivery is disabled", response.response.lower())
            self.assertEqual(store.list_reminders(), [])

    def test_scheduler_backend_unavailable_is_reported_honestly(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "scheduler_backend", "custom-backend"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )

            success, _summary, record, blockers = service.schedule_one_time_reminder(
                summary="check the deployment",
                deliver_at=datetime.now().astimezone() + timedelta(minutes=10),
                channel_id="D123",
                user_id="U123",
            )

        self.assertFalse(success)
        self.assertIsNone(record)
        self.assertTrue(any("unsupported scheduler backend" in blocker.lower() for blocker in blockers))

    def test_recurring_parser_extracts_weekday_schedule_with_time(self) -> None:
        parsed = parse_recurring_reminder_request(
            "Remind me every weekday at 3 PM to stretch",
            timezone_name="America/New_York",
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        assert parsed.schedule is not None
        self.assertEqual(parsed.summary, "stretch")
        self.assertEqual(parsed.schedule.frequency, "weekdays")
        self.assertEqual(parsed.schedule.hour, 15)

    def test_recurring_parser_requests_follow_up_when_time_missing(self) -> None:
        parsed = parse_recurring_reminder_request(
            "Remind me every day to play basketball",
            timezone_name="America/New_York",
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.summary, "play basketball")
        self.assertIn("what time", parsed.follow_up_question or "")

    def test_fast_action_schedules_recurring_reminder(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=store,
                    task_store=TaskStateStore(),
                ),
                reminder_service=service,
                openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
            )

            with bind_interaction_context(
                InteractionContext(source="slack", channel_id="D123", user_id="U123")
            ):
                response = handler.handle(
                    "Remind me every weekday at 3 PM to stretch",
                    AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
                )

        assert response is not None
        self.assertEqual(response.status.value, "completed")
        self.assertIn("every weekday at 3:00 pm", response.response.lower())
        reminders = store.list_reminders(statuses=(ReminderStatus.PENDING,))
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].schedule_kind, "recurring")

    def test_fast_action_cancel_reminder(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )
            recurring = parse_recurring_reminder_request(
                "Remind me every day at 7 PM to play basketball",
                timezone_name="America/New_York",
            )
            assert recurring is not None and recurring.schedule is not None
            service.schedule_recurring_reminder(
                summary="play basketball",
                schedule=recurring.schedule,
                channel_id="D123",
                user_id="U123",
                metadata={"delivery_text": "Reminder: play basketball"},
            )
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=store,
                    task_store=TaskStateStore(),
                ),
                reminder_service=service,
                openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
            )

            response = handler.handle(
                "Cancel my basketball reminder",
                AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
            )
            confirmed = handler.handle(
                "confirm",
                AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
            )

        assert response is not None and confirmed is not None
        self.assertEqual(response.status.value, "blocked")
        self.assertIn("confirm", response.response.lower())
        self.assertEqual(confirmed.status.value, "completed")
        self.assertIn("i canceled", confirmed.response.lower())
        active = store.list_reminders(statuses=(ReminderStatus.PENDING,))
        self.assertEqual(active, [])

    def test_conversation_lists_recurring_reminders_naturally(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )
            recurring = parse_recurring_reminder_request(
                "Remind me every weekday at 3 PM to stretch",
                timezone_name="America/New_York",
            )
            assert recurring is not None and recurring.schedule is not None
            service.schedule_recurring_reminder(
                summary="stretch",
                schedule=recurring.schedule,
                channel_id="D123",
                user_id="U123",
                metadata={"delivery_text": "Reminder: stretch"},
            )
            handler = ConversationalHandler(
                openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=OperatorContextService(
                    memory_store_instance=store,
                    task_store=TaskStateStore(),
                ),
                reminder_service=service,
            )

            response = handler.handle(
                "What recurring reminders are active?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="status", should_use_tools=False),
            )

        self.assertIn("every weekday", response.response.lower())
        self.assertIn("stretch", response.response.lower())

    def test_fast_action_updates_recurring_reminder_time(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "scheduler_backend", "apscheduler"),
            patch.object(settings, "reminders_enabled", True),
            patch.object(settings, "scheduler_timezone", "America/New_York"),
            patch.object(settings, "slack_enabled", True),
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch("integrations.reminders.service.BackgroundScheduler", FakeBackgroundScheduler),
            patch("integrations.reminders.service._APSCHEDULER_IMPORT_ERROR", None),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = ReminderSchedulerService(
                runtime_settings=settings,
                memory_store_instance=store,
                outbound_adapter=FakeOutboundAdapter(),
            )
            recurring = parse_recurring_reminder_request(
                "Remind me every weekday at 3 PM to stretch",
                timezone_name="America/New_York",
            )
            assert recurring is not None and recurring.schedule is not None
            service.schedule_recurring_reminder(
                summary="stretch",
                schedule=recurring.schedule,
                channel_id="D123",
                user_id="U123",
                metadata={"delivery_text": "Reminder: stretch"},
            )
            handler = FastActionHandler(
                operator_context_service=OperatorContextService(
                    memory_store_instance=store,
                    task_store=TaskStateStore(),
                ),
                reminder_service=service,
                openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
            )

            response = handler.handle(
                "Change my stretch reminder to every weekday at 4 PM",
                AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
            )
            confirmed = handler.handle(
                "confirm",
                AssistantDecision(mode=RequestMode.ACT, reasoning="test", should_use_tools=True),
            )

        assert response is not None and confirmed is not None
        self.assertEqual(response.status.value, "blocked")
        self.assertIn("confirm", response.response.lower())
        self.assertEqual(confirmed.status.value, "completed")
        self.assertIn("4:00 pm", confirmed.response.lower())


class CalendarFoundationTests(unittest.TestCase):
    def test_calendar_readiness_is_honest_when_google_auth_is_missing(self) -> None:
        with (
            patch.object(settings, "calendar_enabled", True),
            patch.object(settings, "calendar_provider", "google"),
            patch.object(settings, "calendar_client_id", None),
            patch.object(settings, "calendar_client_secret", None),
            patch.object(settings, "calendar_refresh_token", None),
        ):
            readiness = build_integration_readiness()

        self.assertIn(readiness["integration:calendar"].status, {"scaffolded", "unavailable"})
        self.assertTrue(readiness["integration:calendar"].missing_fields)

    def test_parse_calendar_query_understands_today_and_tomorrow(self) -> None:
        today = parse_calendar_query("What do I have today?", timezone_name="America/New_York")
        tomorrow = parse_calendar_query("What's on my calendar tomorrow?", timezone_name="America/New_York")

        self.assertIsNotNone(today)
        self.assertIsNotNone(tomorrow)
        assert today is not None
        assert tomorrow is not None
        self.assertEqual(today.label, "today")
        self.assertEqual(tomorrow.label, "tomorrow")

    def test_parse_calendar_event_request_builds_basic_event(self) -> None:
        draft = parse_calendar_event_request(
            "Add an event for tomorrow at 4 PM called Math Review",
            now=datetime(2026, 4, 23, 12, 0).astimezone(),
            timezone_name="America/New_York",
        )

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.title, "Math Review")
        self.assertEqual(draft.start.hour, 16)

    def test_calendar_service_lists_events_when_provider_is_ready(self) -> None:
        event = GoogleCalendarEvent(
            event_id="evt-1",
            title="Math Review",
            start=datetime(2026, 4, 23, 16, 0).astimezone(),
            end=datetime(2026, 4, 23, 17, 0).astimezone(),
        )
        service = CalendarService(provider=FakeCalendarProvider(events=[event]))

        result = service.events_for_day(target_day=datetime(2026, 4, 23, 12, 0).astimezone())

        self.assertTrue(result.success)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].title, "Math Review")

    def test_calendar_conversation_reports_honest_blocked_state(self) -> None:
        handler = ConversationalHandler(
            openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
            task_store=TaskStateStore(),
            operator_context_service=OperatorContextService(
                memory_store_instance=MemoryStore(Path(tempfile.gettempdir()) / "calendar-memory.json"),
                task_store=TaskStateStore(),
            ),
            calendar_service=CalendarService(provider=FakeCalendarProvider(blockers=["CALENDAR_REFRESH_TOKEN is missing."])),
        )

        response = handler.handle(
            "What do I have today?",
            AssistantDecision(mode=RequestMode.ANSWER, reasoning="calendar", should_use_tools=False),
        )

        self.assertIn("blocked", response.response.lower())
        self.assertIn("saved google calendar access", response.response.lower())
        self.assertNotIn("calendar_refresh_token", response.response.lower())

    def test_fast_action_creates_calendar_event(self) -> None:
        service = CalendarService(provider=FakeCalendarProvider())
        handler = FastActionHandler(
            operator_context_service=OperatorContextService(
                memory_store_instance=MemoryStore(Path(tempfile.gettempdir()) / "calendar-create-memory.json"),
                task_store=TaskStateStore(),
            ),
            calendar_service=service,
            openrouter_client=FakeReminderOpenRouterClient(configured=False, response=""),
        )

        response = handler.handle(
            "Add an event for tomorrow at 4 PM called Math Review",
            AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True),
        )

        assert response is not None
        self.assertEqual(response.status.value, "completed")
        self.assertIn("math review", response.response.lower())


if __name__ == "__main__":
    unittest.main()
