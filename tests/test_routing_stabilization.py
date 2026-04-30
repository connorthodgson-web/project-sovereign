"""Targeted routing, latency, and evidence regressions for the stabilization pass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.verifier_agent import VerifierAgent
from app.config import settings
from core.assistant import AssistantLayer
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ExecutionEscalation,
    FileEvidence,
    RequestMode,
    SubTask,
    Task,
    TaskStatus,
    ToolEvidence,
    ToolInvocation,
)
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from memory.memory_store import MemoryStore
from tools.base_tool import BaseTool
from tools.registry import ToolRegistry


class FakeOpenRouterClient:
    def __init__(self, *, configured: bool = True, response: str = "") -> None:
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
        return invocation.tool_name == self.name and invocation.action in {"open", "inspect", "summarize"}

    def execute(self, invocation: ToolInvocation) -> dict:
        url = invocation.parameters.get("url", "")
        dataset = {
            "https://example.com": {
                "title": "Example Domain",
                "final_url": "https://example.com",
                "headings": ["Example Domain"],
                "text_preview": "This domain is for use in illustrative examples in documents.",
                "summary_text": "Example Domain is a reserved page for documentation examples.",
            },
            "https://www.cnn.com": {
                "title": "CNN",
                "final_url": "https://www.cnn.com",
                "headings": ["Story One", "Story Two", "Story Three", "Story Four", "Story Five"],
                "text_preview": "Top stories from the CNN homepage.",
                "summary_text": "CNN homepage with major top stories.",
            },
        }
        payload = dataset[url].copy()
        payload.update(
            {
                "requested_url": url,
                "backend": "playwright",
                "screenshot_path": "C:/tmp/fake-browser.png",
                "user_action_required": [],
                "objective": invocation.parameters.get("objective", ""),
            }
        )
        return {
            "success": True,
            "summary": f"Opened {payload['final_url']} and captured browser evidence.",
            "payload": payload,
            "error": None,
        }


class RoutingStabilizationTests(unittest.TestCase):
    def assert_no_backend_jargon(self, text: str) -> None:
        lowered = text.lower()
        for marker in (
            "task_status",
            "request_mode",
            "planner_mode",
            "execution state",
            "route_",
            "langgraph",
            "subtask",
            "orchestration graph",
            "evaluator",
        ):
            self.assertNotIn(marker, lowered)

    def _memory_service(self, memory_path: Path) -> OperatorContextService:
        return OperatorContextService(
            openrouter_client=FakeOpenRouterClient(configured=False),
            memory_store_instance=MemoryStore(memory_path),
            task_store=TaskStateStore(),
        )

    def _browser_registry(self) -> ToolRegistry:
        from tools.file_tool import FileTool
        from tools.runtime_tool import RuntimeTool
        from tools.slack_messaging_tool import SlackMessagingTool

        registry = ToolRegistry()
        registry.register(FakeBrowserTool())
        registry.register(FileTool())
        registry.register(RuntimeTool())
        registry.register(SlackMessagingTool())
        return registry

    def _supervisor(
        self,
        *,
        memory_path: Path,
        tool_registry: ToolRegistry | None = None,
        openrouter_client: FakeOpenRouterClient | None = None,
    ) -> tuple[Supervisor, FakeOpenRouterClient]:
        fake_openrouter = openrouter_client or FakeOpenRouterClient(configured=False)
        operator_context = self._memory_service(memory_path)
        assistant = AssistantLayer(
            openrouter_client=fake_openrouter,
            operator_context_service=operator_context,
        )
        router = Router(
            openrouter_client=fake_openrouter,
            tool_registry=tool_registry or self._browser_registry(),
        )
        planner = Planner(
            openrouter_client=fake_openrouter,
            tool_registry=tool_registry or self._browser_registry(),
            agent_registry=router.agent_registry,
        )
        supervisor = Supervisor(
            assistant_layer=assistant,
            router=router,
            planner=planner,
            operator_context_service=operator_context,
        )
        return supervisor, fake_openrouter

    def test_assistant_fast_prompts_stay_local_and_instant_like(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, fake_openrouter = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            prompts = ["hi", "how are you?", "who are you?", "what can you do?"]
            for prompt in prompts:
                with self.assertLogs("core.supervisor", level="INFO") as logs:
                    response = supervisor.handle_user_goal(prompt)
                combined = "\n".join(logs.output)
                self.assertEqual(response.request_mode, RequestMode.ANSWER)
                self.assertIn("ROUTE_FAST_ASSISTANT", combined)
                self.assertIn("ROUTE_INTENT_CLASSIFICATION", combined)
                self.assertIn("OPENROUTER_CALLED=False", combined)
                self.assertNotIn("ROUTE_PLANNER", combined)
                self.assert_no_backend_jargon(response.response)

            self.assertEqual(fake_openrouter.prompt_calls, [])

    def test_llm_decides_nontrivial_lane_when_available(self) -> None:
        reminder_layer = AssistantLayer(
            openrouter_client=FakeOpenRouterClient(
                configured=True,
                response=(
                    '{"mode":"ACT","escalation_level":"single_action","reasoning":"The user wants a real reminder.",'
                    '"should_use_tools":true,"intent_label":"reminder_action"}'
                ),
            )
        )
        script_layer = AssistantLayer(
            openrouter_client=FakeOpenRouterClient(
                configured=True,
                response=(
                    '{"mode":"EXECUTE","escalation_level":"bounded_task_execution","reasoning":"The user wants a script built.",'
                    '"should_use_tools":true,"intent_label":"bounded_execution"}'
                ),
            )
        )

        reminder = reminder_layer.decide("Remind me to study at 7")
        script = script_layer.decide("Build me a script")

        self.assertEqual(reminder.mode, RequestMode.ACT)
        self.assertEqual(reminder.escalation_level, ExecutionEscalation.SINGLE_ACTION)
        self.assertEqual(script.mode, RequestMode.EXECUTE)
        self.assertEqual(script.escalation_level, ExecutionEscalation.BOUNDED_TASK_EXECUTION)

    def test_memory_fast_path_prompts_use_memory_without_openrouter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, fake_openrouter = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            remember = supervisor.handle_user_goal("my name is Connor Hodgson")
            recall = supervisor.handle_user_goal("what do you remember about me?")
            with self.assertLogs("core.supervisor", level="INFO") as logs:
                follow_up = supervisor.handle_user_goal("is that all you remember?")

            self.assertEqual(remember.request_mode, RequestMode.ANSWER)
            self.assertIn("Connor Hodgson", recall.response)
            self.assertTrue(
                "don't have anything beyond that" in follow_up.response.lower()
                or "local memory" in follow_up.response.lower()
            )
            self.assertEqual(fake_openrouter.prompt_calls, [])
            self.assertIn("ROUTE_MEMORY", "\n".join(logs.output))

    def test_reminder_fast_path_does_not_route_to_browser_or_codex(self) -> None:
        from core.interaction_context import InteractionContext, bind_interaction_context

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, _fake_openrouter = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            with bind_interaction_context(InteractionContext(source="slack", channel_id="D123", user_id="U123")):
                with self.assertLogs("core.supervisor", level="INFO") as logs:
                    response = supervisor.handle_user_goal("remind me in 30 seconds to drink water")

            combined = "\n".join(logs.output)
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertIn(response.status, {TaskStatus.COMPLETED, TaskStatus.BLOCKED})
            self.assertIn("ROUTE_REMINDER", combined)
            self.assertNotIn("ROUTE_BROWSER", combined)
            self.assertNotIn("ROUTE_CODEX", combined)

    def test_direct_browser_requests_use_fast_browser_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            registry = self._browser_registry()
            supervisor, fake_openrouter = self._supervisor(
                memory_path=Path(temp_dir) / "memory.json",
                tool_registry=registry,
            )

            with self.assertLogs("core.supervisor", level="INFO") as logs:
                example = supervisor.handle_user_goal("open https://example.com and summarize it")
            cnn = supervisor.handle_user_goal("open cnn and tell me the top 5 stories")

            self.assertEqual(example.planner_mode, "fast_action")
            self.assertTrue(any(result.tool_name == "browser_tool" for result in example.results))
            self.assertIn("Example Domain", example.response)
            self.assertEqual(cnn.planner_mode, "fast_action")
            self.assertIn("Story One", cnn.response)
            self.assertEqual(fake_openrouter.prompt_calls, [])
            combined = "\n".join(logs.output)
            self.assertIn("ROUTE_BROWSER", combined)
            self.assertIn("OPENROUTER_CALLED=False", combined)

    def test_local_file_fast_path_fixes_workspace_duplication_bug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, fake_openrouter = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            with self.assertLogs("core.supervisor", level="INFO") as logs:
                create = supervisor.handle_user_goal(
                    "Create a small README note in workspace/created_items/codex_test.md explaining that Codex CLI Agent is connected."
                )
            read = supervisor.handle_user_goal("read workspace/created_items/codex_test.md")
            listed = supervisor.handle_user_goal("list workspace/created_items")

            created_file = Path(temp_dir) / "created_items" / "codex_test.md"
            doubled_file = Path(temp_dir) / "workspace" / "created_items" / "codex_test.md"
            evidence = create.results[0].evidence[0]

            self.assertEqual(create.planner_mode, "fast_action")
            self.assertEqual(create.status, TaskStatus.COMPLETED)
            self.assertTrue(created_file.exists())
            self.assertFalse(doubled_file.exists())
            self.assertEqual(evidence.requested_path, "workspace/created_items/codex_test.md")
            self.assertEqual(evidence.normalized_path, "created_items/codex_test.md")
            self.assertEqual(Path(evidence.actual_path or ""), created_file)
            self.assertIn("Codex CLI Agent is connected", created_file.read_text(encoding="utf-8"))
            self.assertIn("Codex CLI Agent is connected", read.response)
            self.assertIn("codex_test.md", listed.response)
            self.assertEqual(fake_openrouter.prompt_calls, [])
            combined = "\n".join(logs.output)
            self.assertIn("ROUTE_LOCAL_FILE", combined)
            self.assertIn("OPENROUTER_CALLED=False", combined)

    def test_codex_route_stays_reserved_for_serious_coding_prompts(self) -> None:
        router = Router(openrouter_client=FakeOpenRouterClient(configured=False))
        planner = Planner(
            openrouter_client=FakeOpenRouterClient(configured=False),
            agent_registry=router.agent_registry,
        )

        refactor_subtasks, _ = planner.create_plan("Refactor a failing auth module and add tests.")
        healthcheck_subtasks, _ = planner.create_plan("Build a small healthcheck endpoint and add tests.")

        self.assertTrue(any(subtask.assigned_agent == "codex_cli_agent" for subtask in refactor_subtasks))
        self.assertTrue(any(subtask.assigned_agent == "codex_cli_agent" for subtask in healthcheck_subtasks))

    def test_stale_lane_context_does_not_steal_follow_up_requests(self) -> None:
        from core.interaction_context import InteractionContext, bind_interaction_context

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            registry = self._browser_registry()
            supervisor, _ = self._supervisor(
                memory_path=Path(temp_dir) / "memory.json",
                tool_registry=registry,
            )

            browser_response = supervisor.handle_user_goal("open https://example.com and summarize it")
            with bind_interaction_context(InteractionContext(source="slack", channel_id="D123", user_id="U123")):
                reminder_response = supervisor.handle_user_goal("remind me in 30 seconds to drink water")
            coding_response = supervisor.handle_user_goal("Refactor a failing auth module and add tests.")
            greeting_response = supervisor.handle_user_goal("hi")
            file_response = supervisor.handle_user_goal("write this text to workspace/created_items/example.md saying concise mode is on")
            memory_response = supervisor.handle_user_goal("what do you remember about me?")

            self.assertTrue(any(result.tool_name == "browser_tool" for result in browser_response.results))
            self.assertEqual(reminder_response.planner_mode, "fast_action")
            self.assertFalse(any(result.tool_name == "browser_tool" for result in reminder_response.results))
            self.assertEqual(coding_response.request_mode, RequestMode.EXECUTE)
            self.assertEqual(greeting_response.planner_mode, "conversation_fast_path")
            self.assertEqual(file_response.planner_mode, "fast_action")
            self.assertEqual(memory_response.request_mode, RequestMode.ANSWER)

    def test_capability_questions_do_not_execute_file_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, fake_openrouter = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            response = supervisor.handle_user_goal("can u make a file or nah")

            self.assertEqual(response.request_mode, RequestMode.ANSWER)
            self.assertEqual(response.planner_mode, "conversation_fast_path")
            self.assertFalse((Path(temp_dir) / "created_items").exists())
            self.assertEqual(fake_openrouter.prompt_calls, [])
            self.assertIn("can create local workspace files", response.response.lower())
            self.assert_no_backend_jargon(response.response)

    def test_ambiguous_inputs_ask_clarifying_question_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, fake_openrouter = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            short_response = supervisor.handle_user_goal("WYD")
            vague_response = supervisor.handle_user_goal("I might want a file")

            self.assertEqual(short_response.request_mode, RequestMode.ANSWER)
            self.assertEqual(short_response.planner_mode, "conversation_clarify")
            self.assertIn("something specific", short_response.response.lower())
            self.assertEqual(vague_response.request_mode, RequestMode.ANSWER)
            self.assertEqual(vague_response.planner_mode, "conversation_clarify")
            self.assertIn("create a file now", vague_response.response.lower())
            self.assertEqual(fake_openrouter.prompt_calls, [])
            self.assertFalse((Path(temp_dir) / "created_items").exists())

    def test_browser_then_casual_message_does_not_reuse_browser_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            registry = self._browser_registry()
            supervisor, fake_openrouter = self._supervisor(
                memory_path=Path(temp_dir) / "memory.json",
                tool_registry=registry,
            )

            browser_response = supervisor.handle_user_goal("open https://example.com and summarize it")
            casual_response = supervisor.handle_user_goal("how are you?")

            self.assertTrue(any(result.tool_name == "browser_tool" for result in browser_response.results))
            self.assertEqual(casual_response.request_mode, RequestMode.ANSWER)
            self.assertEqual(casual_response.planner_mode, "conversation_fast_path")
            self.assertEqual(fake_openrouter.prompt_calls, [])

    def test_codex_then_casual_message_does_not_reuse_codex_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor, _ = self._supervisor(memory_path=Path(temp_dir) / "memory.json")

            coding_response = supervisor.handle_user_goal("Refactor a failing auth module and add tests.")
            casual_response = supervisor.handle_user_goal("how are you?")

            self.assertEqual(coding_response.request_mode, RequestMode.EXECUTE)
            self.assertEqual(casual_response.request_mode, RequestMode.ANSWER)
            self.assertEqual(casual_response.planner_mode, "conversation_fast_path")

    def test_verifier_blocks_reviewed_file_path_mismatch(self) -> None:
        verifier = VerifierAgent()
        task = Task(
            goal="Create a file in workspace/created_items/codex_test.md",
            title="File task",
            description="File task",
            results=[
                AgentResult(
                    subtask_id="review-1",
                    agent="reviewer_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Reviewed workspace write result successfully.",
                    tool_name="file_tool",
                    evidence=[
                        FileEvidence(
                            tool_name="file_tool",
                            operation="write",
                            requested_path="workspace/created_items/codex_test.md",
                            normalized_path="created_items/codex_test.md",
                            workspace_root="C:/workspace",
                            actual_path="C:/workspace/workspace/created_items/codex_test.md",
                            file_path="C:/workspace/workspace/created_items/codex_test.md",
                            verification_notes=["Mismatched path was detected."],
                        )
                    ],
                )
            ],
        )

        result = verifier.run(
            task,
            SubTask(
                title="Verify outcome",
                description="Verify outcome",
                objective="Verify outcome",
                assigned_agent="verifier_agent",
            ),
        )

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("path", result.summary.lower())


if __name__ == "__main__":
    unittest.main()
