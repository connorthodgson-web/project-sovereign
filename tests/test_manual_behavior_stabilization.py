"""Manual-behavior regressions for live-assistant stabilization."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.assistant import AssistantLayer
from core.fast_actions import FastActionHandler
from core.interaction_context import InteractionContext, bind_interaction_context
from core.model_routing import ModelRouter, ModelTier
from core.models import AssistantDecision, RequestMode, TaskStatus
from core.operator_context import OperatorContextService
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.calendar.google_provider import GoogleCalendarProvider
from integrations.calendar.service import CalendarService
from integrations.reminders.parsing import parse_one_time_reminder_request
from integrations.slack_client import SlackClient, SlackOperatorBridge
from memory.memory_store import MemoryStore


class FakeUnavailableCalendarProvider(GoogleCalendarProvider):
    def readiness_blockers(self) -> list[str]:
        return ["GOOGLE_CALENDAR_TOKEN_PATH does not exist yet; run the local OAuth flow once."]


class ManualBehaviorStabilizationTests(unittest.TestCase):
    def _operator_context(self, temp_dir: str) -> OperatorContextService:
        return OperatorContextService(
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
            task_store=TaskStateStore(),
        )

    def test_placeholder_tier_model_is_never_selected_for_runtime_calls(self) -> None:
        with (
            patch.object(settings, "model_tier_2", "your-balanced-model"),
            patch.object(settings, "openrouter_model_tier2", "openai/gpt-4o-mini"),
        ):
            router = ModelRouter()
            selection = router.select(
                label="planner_create_plan",
                prompt="Break the goal into concrete subtasks.",
            )

        self.assertEqual(selection.tier, ModelTier.TIER_2)
        self.assertEqual(selection.model, "openai/gpt-4o-mini")
        self.assertNotEqual(selection.model, "your-balanced-model")

    def test_email_unavailable_fails_fast_without_planner_or_progress_ack(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "gmail_enabled", False),
        ):
            operator_context = self._operator_context(temp_dir)
            supervisor = Supervisor(operator_context_service=operator_context)

            self.assertFalse(supervisor.should_send_progress("send an email to alex@example.com saying hi"))
            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal("send an email to alex@example.com saying hi")

        combined = "\n".join(logs.output)
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertEqual(response.planner_mode, "fast_action")
        self.assertIn("gmail setup is needed", response.response.lower())
        self.assertIn("oauth", response.response.lower())
        self.assertIn("ROUTE_EMAIL_UNAVAILABLE", combined)
        self.assertNotIn("PLANNER_AGENT_START", combined)

    def test_slack_does_not_send_on_it_for_known_unavailable_email(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=lambda _: self._blocked_email_response(),
            progress_decider=lambda message: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=lambda task: task())

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "text": "send an email to Alex"},
            sent_messages.append,
        )

        self.assertEqual(len(sent_messages), 1)
        self.assertNotIn("On it.", sent_messages)
        self.assertIn("Email sending is not live", sent_messages[0])

    def test_calendar_token_missing_returns_oauth_setup_before_confirmation(self) -> None:
        service = CalendarService(provider=FakeUnavailableCalendarProvider())
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FastActionHandler(
                operator_context_service=self._operator_context(temp_dir),
                calendar_service=service,
            )
            decision = AssistantDecision(mode=RequestMode.ACT, reasoning="calendar", should_use_tools=True)

            response = handler.handle(
                "Add an event for tomorrow at 4 PM called Review with bob@example.com invite",
                decision,
            )

        assert response is not None
        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("google calendar setup is needed", response.response.lower())
        self.assertIn("oauth", response.response.lower())
        self.assertNotIn("please confirm", response.response.lower())

    def test_calendar_readiness_fast_path_does_not_need_llm(self) -> None:
        service = CalendarService(provider=FakeUnavailableCalendarProvider())
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            operator_context = self._operator_context(temp_dir)
            supervisor = Supervisor(
                assistant_layer=AssistantLayer(openrouter_client=None, operator_context_service=operator_context),
                operator_context_service=operator_context,
                fast_action_handler=FastActionHandler(
                    operator_context_service=operator_context,
                    calendar_service=service,
                ),
            )

            response = supervisor.handle_user_goal("Add an event for tomorrow at 4 PM called Review")

        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertEqual(response.planner_mode, "fast_action")
        self.assertIn("google calendar setup is needed", response.response.lower())

    def test_reminder_parser_accepts_go_off_natural_phrasing(self) -> None:
        now = datetime(2026, 4, 26, 12, 0).astimezone()

        parsed = parse_one_time_reminder_request(
            "set a reminder to go off in 1 minute telling me to drink water",
            now=now,
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.summary, "drink water")
        self.assertEqual(int((parsed.deliver_at - now).total_seconds()), 60)

    def test_post_tool_thanks_stays_simple_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = Supervisor(operator_context_service=self._operator_context(temp_dir))
            with bind_interaction_context(InteractionContext(source="slack", channel_id="D123", user_id="U123")):
                supervisor.handle_user_goal("remind me in 1 minute to drink water")
            response = supervisor.handle_user_goal("thanks")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "conversation_fast_path")

    @staticmethod
    def _blocked_email_response():
        from core.models import ChatResponse, TaskOutcome

        return ChatResponse(
            task_id="email-blocked",
            status=TaskStatus.BLOCKED,
            planner_mode="fast_action",
            request_mode=RequestMode.ACT,
            response="Email sending is not live in this runtime yet.",
            outcome=TaskOutcome(blocked=1, total_subtasks=1),
            subtasks=[],
            results=[],
        )


if __name__ == "__main__":
    unittest.main()
