"""Behavioral assistant-feel coverage for the Sovereign front door."""

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
from core.models import AgentExecutionStatus, RequestMode, TaskStatus, ToolInvocation
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore, task_state_store
from core.supervisor import Supervisor
from integrations.calendar.google_provider import GoogleCalendarProvider
from integrations.calendar.service import CalendarService
from memory.memory_store import MemoryStore
from tools.base_tool import BaseTool
from tools.file_tool import FileTool
from tools.registry import ToolRegistry
from tools.runtime_tool import RuntimeTool
from tools.slack_messaging_tool import SlackMessagingTool


BACKEND_JARGON = (
    "langgraph",
    "planner",
    "router",
    "evaluator",
    "agentresult",
    "task_status",
    "tool invocation",
    "orchestration graph",
    "cost=",
    "risk=",
    "resume_target",
    "pending_action",
)


class FakeOpenRouterClient:
    def __init__(self, *, configured: bool = False, response: str = "") -> None:
        self.configured = configured
        self.response = response
        self.prompt_calls: list[str | None] = []

    def is_configured(self) -> bool:
        return self.configured

    def prompt(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        label: str | None = None,
        **kwargs,
    ) -> str:
        del prompt, system_prompt, kwargs
        self.prompt_calls.append(label)
        return self.response


class FakeBrowserTool(BaseTool):
    name = "browser_tool"

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name

    def execute(self, invocation: ToolInvocation) -> dict:
        url = invocation.parameters.get("url", "https://example.com")
        return {
            "success": True,
            "summary": "Opened Example Domain and captured browser evidence.",
            "payload": {
                "requested_url": url,
                "final_url": "https://example.com",
                "title": "Example Domain",
                "headings": ["Example Domain"],
                "text_preview": "This domain is for use in illustrative examples in documents.",
                "summary_text": "Example Domain is a reserved page for documentation examples.",
                "screenshot_path": "C:/tmp/example.png",
                "user_action_required": [],
            },
            "error": None,
        }


class FakeUnavailableCalendarProvider(GoogleCalendarProvider):
    def readiness_blockers(self) -> list[str]:
        return ["GOOGLE_CALENDAR_TOKEN_PATH does not exist yet; run the local OAuth flow once."]


class FakeReminderService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def schedule_one_time_reminder(
        self,
        *,
        summary: str,
        deliver_at: datetime,
        channel_id: str,
        user_id: str | None = None,
        source: str = "fast_action",
        metadata: dict[str, str] | None = None,
    ):
        self.calls.append(
            {
                "summary": summary,
                "deliver_at": deliver_at,
                "channel_id": channel_id,
                "user_id": user_id,
                "source": source,
                "metadata": metadata or {},
            }
        )

        class Record:
            reminder_id = "reminder-feel-1"

        return True, "scheduled", Record(), []


class AssistantFeelBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        task_state_store._tasks.clear()

    def tearDown(self) -> None:
        task_state_store._tasks.clear()

    def _operator_context(self, memory_path: Path) -> OperatorContextService:
        return OperatorContextService(
            openrouter_client=FakeOpenRouterClient(configured=False),
            memory_store_instance=MemoryStore(memory_path),
            task_store=TaskStateStore(),
        )

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(FakeBrowserTool())
        registry.register(FileTool())
        registry.register(RuntimeTool())
        registry.register(SlackMessagingTool())
        return registry

    def _supervisor(
        self,
        temp_dir: str,
        *,
        reminder_service: FakeReminderService | None = None,
        calendar_service: CalendarService | None = None,
    ) -> Supervisor:
        fake_openrouter = FakeOpenRouterClient(configured=False)
        operator_context = self._operator_context(Path(temp_dir) / "memory.json")
        registry = self._registry()
        assistant = AssistantLayer(
            openrouter_client=fake_openrouter,
            operator_context_service=operator_context,
        )
        router = Router(openrouter_client=fake_openrouter, tool_registry=registry)
        planner = Planner(
            openrouter_client=fake_openrouter,
            tool_registry=registry,
            agent_registry=router.agent_registry,
        )
        fast_actions = FastActionHandler(
            operator_context_service=operator_context,
            reminder_service=reminder_service,
            calendar_service=calendar_service,
            openrouter_client=fake_openrouter,
            tool_registry=registry,
        )
        return Supervisor(
            assistant_layer=assistant,
            router=router,
            planner=planner,
            operator_context_service=operator_context,
            fast_action_handler=fast_actions,
        )

    def assert_no_backend_jargon(self, text: str) -> None:
        lowered = text.lower()
        for marker in BACKEND_JARGON:
            self.assertNotIn(marker, lowered)

    def test_simple_chat_and_context_answers_feel_like_one_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            for prompt in ("hi", "what's up", "what can you do?", "what are you working on?"):
                response = supervisor.handle_user_goal(prompt)

                self.assertEqual(response.request_mode, RequestMode.ANSWER)
                self.assertFalse(supervisor.should_send_progress(prompt))
                self.assert_no_backend_jargon(response.response)
                self.assertNotEqual(response.response.strip(), "")
                if prompt == "what's up":
                    self.assertNotIn("right now i can", response.response.lower())

    def test_memory_statement_stays_natural_and_does_not_trigger_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)
            prompt = "remember that my project priority is memory then calendar"

            response = supervisor.handle_user_goal(prompt)

            self.assertEqual(response.request_mode, RequestMode.ANSWER)
            self.assertFalse(supervisor.should_send_progress(prompt))
            self.assertIn("noted", response.response.lower())
            self.assertNotIn("browser backend", response.response.lower())
            self.assert_no_backend_jargon(response.response)

    def test_reminder_routes_as_small_action_without_slack_progress_ack(self) -> None:
        reminder_service = FakeReminderService()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir, reminder_service=reminder_service)
            prompt = "remind me to study at 7"

            with bind_interaction_context(InteractionContext(source="slack", channel_id="D123", user_id="U123")):
                with self.assertLogs("core.supervisor", level="INFO") as logs:
                    response = supervisor.handle_user_goal(prompt)

            self.assertFalse(supervisor.should_send_progress(prompt))
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertIn("study", response.response.lower())
            self.assertIn("ROUTE_REMINDER", "\n".join(logs.output))
            self.assert_no_backend_jargon(response.response)
            self.assertEqual(reminder_service.calls[0]["channel_id"], "D123")

    def test_calendar_today_is_honest_when_calendar_access_is_missing(self) -> None:
        calendar_service = CalendarService(provider=FakeUnavailableCalendarProvider())
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir, calendar_service=calendar_service)
            prompt = "what do I have today?"

            response = supervisor.handle_user_goal(prompt)

            self.assertFalse(supervisor.should_send_progress(prompt))
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertEqual(response.status, TaskStatus.BLOCKED)
            self.assertIn("calendar", response.response.lower())
            self.assertIn("google", response.response.lower())
            self.assertNotIn("adapter", response.response.lower())
            self.assertNotIn("runtime", response.response.lower())
            self.assert_no_backend_jargon(response.response)

    def test_coding_and_research_requests_enter_planning_execution(self) -> None:
        cases = (
            "build a small Python script that solves a quadratic",
            "research this topic and give me a plan",
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            for prompt in cases:
                with self.assertLogs(level="INFO") as logs:
                    response = supervisor.handle_user_goal(prompt)

                combined = "\n".join(logs.output)
                self.assertTrue(supervisor.should_send_progress(prompt))
                self.assertEqual(response.request_mode, RequestMode.EXECUTE)
                self.assertIn("LANGGRAPH_START", combined)
                self.assertIn("PLANNER_AGENT_START", combined)
                self.assertGreater(response.outcome.total_subtasks, 0)
                self.assert_no_backend_jargon(response.response)

    def test_email_draft_blocked_state_is_specific_without_backend_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir), patch.object(settings, "gmail_enabled", False):
            supervisor = self._supervisor(temp_dir)
            prompt = "send an email draft to alex@example.com saying hi"

            response = supervisor.handle_user_goal(prompt)

            self.assertFalse(supervisor.should_send_progress(prompt))
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertEqual(response.status, TaskStatus.BLOCKED)
            self.assertIn("gmail", response.response.lower())
            self.assertIn("credentials", response.response.lower())
            self.assertNotIn("adapter", response.response.lower())
            self.assertNotIn("runtime", response.response.lower())
            self.assert_no_backend_jargon(response.response)

    def test_browser_request_uses_browser_lane_and_returns_page_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)
            prompt = "check https://example.com"

            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal(prompt)

            self.assertFalse(supervisor.should_send_progress(prompt))
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertTrue(any(result.tool_name == "browser_tool" for result in response.results))
            self.assertIn("Example Domain", response.response)
            self.assertIn("ROUTE_BROWSER", "\n".join(logs.output))
            self.assert_no_backend_jargon(response.response)

    def test_llm_lane_contract_for_representative_prompts(self) -> None:
        decisions = {
            "remind me to study at 7": '{"mode":"ACT","escalation_level":"single_action","reasoning":"A reminder is one concrete action.","should_use_tools":true}',
            "build a small Python script that solves a quadratic": '{"mode":"EXECUTE","escalation_level":"bounded_task_execution","reasoning":"Building a script needs execution.","should_use_tools":true}',
            "research this topic and give me a plan": '{"mode":"EXECUTE","escalation_level":"bounded_task_execution","reasoning":"Research plus planning needs execution.","should_use_tools":true}',
            "what can you do?": '{"mode":"ANSWER","escalation_level":"conversational_advice","reasoning":"The user is asking a capability question.","should_use_tools":false}',
        }

        for prompt, payload in decisions.items():
            layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=True, response=payload))
            decision = layer.decide(prompt)

            if prompt.startswith("what"):
                self.assertEqual(decision.mode, RequestMode.ANSWER)
                self.assertFalse(decision.should_use_tools)
            elif prompt.startswith("remind"):
                self.assertEqual(decision.mode, RequestMode.ACT)
                self.assertTrue(decision.should_use_tools)
            else:
                self.assertEqual(decision.mode, RequestMode.EXECUTE)
                self.assertTrue(decision.should_use_tools)


if __name__ == "__main__":
    unittest.main()
