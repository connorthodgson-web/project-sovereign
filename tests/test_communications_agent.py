"""Coverage for the live Slack outbound communications path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.communications_agent import CommunicationsAgent
from app.config import settings
from core.assistant import AssistantLayer
from core.models import AgentExecutionStatus, ExecutionEscalation, SubTask, Task, ToolInvocation
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.openrouter_client import OpenRouterClient
from integrations.slack_outbound import SlackOutboundAdapter
from memory.memory_store import MemoryStore
from tools.registry import build_default_tool_registry
from tools.slack_messaging_tool import SlackMessagingTool


class FakeSlackWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []
        self.opened_dms: list[str] = []

    def chat_postMessage(self, *, channel: str, text: str) -> dict[str, str | bool]:
        self.posts.append({"channel": channel, "text": text})
        return {"ok": True, "channel": channel, "ts": "1710000000.000100"}

    def conversations_open(self, *, users: str) -> dict[str, object]:
        self.opened_dms.append(users)
        return {"ok": True, "channel": {"id": "D999"}}


class CommunicationsAgentTests(unittest.TestCase):
    class _NoLlmClient:
        def is_configured(self) -> bool:
            return False

    def _assistant_layer(self, *, memory_path: Path | None = None) -> AssistantLayer:
        if memory_path is None:
            return AssistantLayer(openrouter_client=OpenRouterClient(api_key=None))
        return AssistantLayer(
            openrouter_client=OpenRouterClient(api_key=None),
            operator_context_service=OperatorContextService(
                memory_store_instance=MemoryStore(memory_path),
                task_store=TaskStateStore(),
            ),
        )

    def test_slack_send_request_routes_to_communications_agent(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            supervisor = Supervisor(assistant_layer=self._assistant_layer())
            planner = Planner(
                openrouter_client=self._NoLlmClient(),
                agent_registry=supervisor.router.agent_registry,
            )

            decision = supervisor.assistant_layer.decide(
                'send a Slack message to #ops saying "Deployment is live."'
            )
            lane = supervisor._select_lane(
                'send a Slack message to #ops saying "Deployment is live."',
                decision,
            )
            subtasks, planner_mode = planner.create_plan(
                'send a Slack message to #ops saying "Deployment is live."',
                escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
            )

        self.assertEqual(lane.agent_id, "planner_agent")
        self.assertEqual(planner_mode, "deterministic")
        execute_subtask = subtasks[1]
        self.assertEqual(execute_subtask.assigned_agent, "communications_agent")
        self.assertIsNotNone(execute_subtask.tool_invocation)
        assert execute_subtask.tool_invocation is not None
        self.assertEqual(execute_subtask.tool_invocation.tool_name, "slack_messaging_tool")
        self.assertEqual(execute_subtask.tool_invocation.action, "send_channel_message")

    def test_slack_tool_succeeds_with_mocked_client(self) -> None:
        fake_client = FakeSlackWebClient()
        tool = SlackMessagingTool(
            outbound_adapter=SlackOutboundAdapter(runtime_settings=settings, client=fake_client)
        )
        invocation = ToolInvocation(
            tool_name="slack_messaging_tool",
            action="send_channel_message",
            parameters={
                "channel": "#ops",
                "target": "#ops",
                "message_text": "Deployment is live.",
            },
        )

        with (
            patch.object(settings, "slack_bot_token", "xoxb-live"),
            patch.object(settings, "slack_enabled", True),
        ):
            result = tool.execute(invocation)

        self.assertTrue(result["success"])
        self.assertEqual(result["payload"]["target"], "#ops")
        self.assertEqual(result["payload"]["message_text"], "Deployment is live.")
        self.assertEqual(result["payload"]["timestamp"], "1710000000.000100")
        self.assertEqual(fake_client.posts[0]["channel"], "#ops")

    def test_missing_target_returns_blocked_honestly(self) -> None:
        fake_client = FakeSlackWebClient()
        registry = build_default_tool_registry(
            slack_messaging_tool=SlackMessagingTool(
                outbound_adapter=SlackOutboundAdapter(runtime_settings=settings, client=fake_client)
            )
        )
        agent = CommunicationsAgent(tool_registry=registry)
        task = Task(goal='send a Slack message saying "Hello."', title="Slack send", description="Slack send")
        subtask = SubTask(
            title="Send outbound Slack message",
            description="Send the Slack message",
            objective='Send a Slack message saying "Hello."',
            assigned_agent="communications_agent",
            tool_invocation=ToolInvocation(
                tool_name="slack_messaging_tool",
                action="send_channel_message",
                parameters={"message_text": "Hello."},
            ),
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("channel", " ".join(result.blockers).lower())
        self.assertEqual(fake_client.posts, [])

    def test_email_request_still_returns_blocked(self) -> None:
        agent = CommunicationsAgent()
        task = Task(goal="Send an email to the team about the release.", title="Email", description="Email")
        subtask = SubTask(
            title="Send email update",
            description="Send the release email",
            objective="Send an email to the team about the release.",
            assigned_agent="communications_agent",
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("email", result.summary.lower())

    def test_existing_assistant_browser_and_reminder_routes_still_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(settings, "openrouter_api_key", None):
                supervisor = Supervisor(
                    assistant_layer=self._assistant_layer(memory_path=Path(temp_dir) / "memory.json")
                )

                greeting_lane = supervisor._select_lane("hi", supervisor.assistant_layer.decide("hi"))
                browser_lane = supervisor._select_lane(
                    "open https://example.com",
                    supervisor.assistant_layer.decide("open https://example.com"),
                )
                reminder_lane = supervisor._select_lane(
                    "remind me in 5 minutes to stretch",
                    supervisor.assistant_layer.decide("remind me in 5 minutes to stretch"),
                )

        self.assertEqual(greeting_lane.agent_id, "assistant_agent")
        self.assertEqual(browser_lane.agent_id, "browser_agent")
        self.assertEqual(reminder_lane.agent_id, "scheduling_agent")


if __name__ == "__main__":
    unittest.main()
