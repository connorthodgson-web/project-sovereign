"""Regression coverage for the bounded LLM-led objective loop."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.assistant import AssistantLayer
from core.models import ObjectiveState, RequestMode, TaskStatus, ToolInvocation
from core.objective_loop import ObjectiveLoopDecision
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore, task_state_store
from core.supervisor import Supervisor
from memory.memory_store import MemoryStore
from tools.base_tool import BaseTool
from tools.file_tool import FileTool
from tools.registry import ToolRegistry
from tools.runtime_tool import RuntimeTool
from tools.slack_messaging_tool import SlackMessagingTool


class NoLlmClient:
    def is_configured(self) -> bool:
        return False


class FakeBrowserTool(BaseTool):
    name = "browser_tool"

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name and invocation.action in {"open", "inspect", "summarize"}

    def execute(self, invocation: ToolInvocation) -> dict:
        return {
            "success": True,
            "summary": "Opened https://example.com and captured browser evidence.",
            "error": None,
            "payload": {
                "requested_url": invocation.parameters.get("url"),
                "final_url": "https://example.com",
                "title": "Example Domain",
                "headings": ["Example Domain"],
                "text_preview": "This domain is for use in illustrative examples in documents.",
                "summary_text": "Example Domain is a reserved page for documentation examples.",
                "screenshot_path": "C:/tmp/example.png",
                "backend": "playwright",
                "user_action_required": [],
            },
        }


class RecordingDecisionMaker:
    def __init__(self, mode: str = "browser_file") -> None:
        self.mode = mode
        self.contexts: list[dict[str, object]] = []

    def decide(self, task, decision, context):
        del decision
        self.contexts.append(context)
        results = context["evidence_results"]
        if self.mode == "manus":
            return ObjectiveLoopDecision(
                action="create_subtask",
                reasoning="Ask a disabled premium agent to prove the guardrail works.",
                title="Use disabled Manus",
                description="Attempt disabled premium execution",
                objective=task.goal,
                agent_name="manus_agent",
            )
        if self.mode == "repeat_failure":
            return ObjectiveLoopDecision(
                action="create_subtask",
                reasoning="Retry a file read that will fail until loop limits stop it.",
                title="Read missing file",
                description="Read a missing file",
                objective="Read definitely-missing.txt",
                agent_name="coding_agent",
                tool_invocation=ToolInvocation(
                    tool_name="file_tool",
                    action="read",
                    parameters={"path": "definitely-missing.txt"},
                ),
            )
        if self.mode == "ask_user":
            if not any(str(item).startswith("user_answer:") for item in context["recent_decisions"]):
                return ObjectiveLoopDecision(
                    action="ask_user",
                    reasoning="Need one missing user detail.",
                    final_response="Which source should I use?",
                    blockers=["source"],
                )
            return ObjectiveLoopDecision(
                action="block",
                reasoning="Resumed the same objective after user input.",
                final_response="I resumed the same objective with your answer.",
                blockers=["test stop"],
            )
        if self.mode == "research":
            if not results:
                return ObjectiveLoopDecision(
                    action="create_subtask",
                    reasoning="Gather available browser evidence before synthesizing.",
                    title="Open source page",
                    description="Open example.com as available browser evidence.",
                    objective="Open https://example.com and capture evidence for recommendation.",
                    agent_name="browser_agent",
                    tool_invocation=ToolInvocation(
                        tool_name="browser_tool",
                        action="summarize",
                        parameters={"url": "https://example.com", "objective": task.goal},
                    ),
                )
            if not any(item["agent"] == "reviewer_agent" for item in results):
                return ObjectiveLoopDecision(action="review", reasoning="Review gathered browser evidence.")
            return ObjectiveLoopDecision(action="finish", reasoning="Verifier can now check the recommendation evidence.")

        if not results:
            browser_subtask = next(
                (
                    item
                    for item in context["current_plan"]
                    if (item.get("tool_invocation") or {}).get("tool_name") == "browser_tool"
                ),
                None,
            )
            if browser_subtask is None:
                return ObjectiveLoopDecision(
                    action="create_subtask",
                    reasoning="The LLM mock chooses a browser step even though no phrase recipe exists.",
                    title="Open example source",
                    description="Open example.com for objective evidence.",
                    objective="Open https://example.com and capture browser evidence.",
                    agent_name="browser_agent",
                    tool_invocation=ToolInvocation(
                        tool_name="browser_tool",
                        action="summarize",
                        parameters={"url": "https://example.com", "objective": task.goal},
                    ),
                )
            return ObjectiveLoopDecision(
                action="execute_subtask",
                reasoning="Use the planned browser step first.",
                subtask_id=browser_subtask["id"],
            )
        if not any(item["tool_name"] == "file_tool" for item in results):
            browser_summary = next(item["summary"] for item in results if item["tool_name"] == "browser_tool")
            return ObjectiveLoopDecision(
                action="create_subtask",
                reasoning="Save the browser evidence summary after seeing the browser result.",
                title="Save browser summary",
                description="Write the browser summary to the requested file.",
                objective="Save browser evidence to summary.txt",
                agent_name="coding_agent",
                tool_invocation=ToolInvocation(
                    tool_name="file_tool",
                    action="write",
                    parameters={"path": "created_items/summary.txt", "content": browser_summary},
                ),
            )
        if not any(item["agent"] == "reviewer_agent" for item in results):
            return ObjectiveLoopDecision(action="review", reasoning="Review the saved file evidence.")
        return ObjectiveLoopDecision(action="finish", reasoning="Ask verifier to confirm completion.")


class ObjectiveLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        task_state_store._tasks.clear()

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(FakeBrowserTool())
        registry.register(FileTool())
        registry.register(RuntimeTool())
        registry.register(SlackMessagingTool())
        return registry

    def _supervisor(self, temp_dir: str, decider: RecordingDecisionMaker) -> Supervisor:
        llm = NoLlmClient()
        operator_context = OperatorContextService(
            openrouter_client=llm,
            task_store=TaskStateStore(),
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
        )
        registry = self._registry()
        router = Router(openrouter_client=llm, tool_registry=registry)
        planner = Planner(openrouter_client=llm, tool_registry=registry, agent_registry=router.agent_registry)
        assistant = AssistantLayer(openrouter_client=llm, operator_context_service=operator_context)
        return Supervisor(
            assistant_layer=assistant,
            planner=planner,
            router=router,
            operator_context_service=operator_context,
            objective_decision_maker=decider,
        )

    def test_simple_chat_does_not_enter_objective_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker()
            response = self._supervisor(temp_dir, decider).handle_user_goal("thanks")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(decider.contexts, [])

    def test_simple_reminder_does_not_enter_objective_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker()
            response = self._supervisor(temp_dir, decider).handle_user_goal("remind me tomorrow to drink water")

        self.assertEqual(response.planner_mode, "fast_action")
        self.assertEqual(decider.contexts, [])

    def test_simple_browser_summary_remains_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker()
            response = self._supervisor(temp_dir, decider).handle_user_goal("open https://example.com and summarize it")

        self.assertEqual(response.planner_mode, "fast_action")
        self.assertTrue(any(result.tool_name == "browser_tool" for result in response.results))
        self.assertEqual(decider.contexts, [])

    def test_multi_step_browser_file_request_enters_llm_led_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker()
            response = self._supervisor(temp_dir, decider).handle_user_goal(
                "open https://example.com, summarize it, and save the summary to summary.txt"
            )
            saved_file = Path(temp_dir) / "created_items" / "summary.txt"
            saved_file_exists = saved_file.exists()
            saved_content = saved_file.read_text(encoding="utf-8") if saved_file_exists else ""

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertGreaterEqual(len(decider.contexts), 3)
        self.assertTrue(saved_file_exists)
        self.assertIn("Example Domain", saved_content)
        self.assertTrue(any(result.tool_name == "browser_tool" for result in response.results))
        self.assertTrue(any(result.tool_name == "file_tool" for result in response.results))
        self.assertNotEqual(response.planner_mode, "fast_action")

    def test_loop_decisions_are_mocked_not_phrase_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker()
            response = self._supervisor(temp_dir, decider).handle_user_goal(
                "Please handle this multi-step objective for the demo."
            )

        self.assertGreaterEqual(len(decider.contexts), 1)
        stored = task_state_store.get_task(response.task_id)
        self.assertIsInstance(stored.objective_state, ObjectiveState)
        self.assertTrue(stored.objective_state.recent_decisions)

    def test_disabled_premium_tools_are_blocked_even_if_llm_asks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            response = self._supervisor(temp_dir, RecordingDecisionMaker("manus")).handle_user_goal(
                "Research this complex objective and produce a recommendation."
            )

        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertIn("Manus", response.response)
        self.assertFalse(any(result.agent == "manus_agent" for result in response.results))

    def test_repeated_failed_tool_call_stops_within_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            response = self._supervisor(temp_dir, RecordingDecisionMaker("repeat_failure")).handle_user_goal(
                "Execute this multi-step file objective until it is done or blocked."
            )

        self.assertEqual(response.status, TaskStatus.BLOCKED)
        self.assertLessEqual(len(response.results), 3)
        self.assertIn("blocked", response.response.lower())

    def test_reviewer_feedback_is_included_in_later_llm_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker("research")
            response = self._supervisor(temp_dir, decider).handle_user_goal(
                "research the example objective with available browser tools and give me a recommendation"
            )

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertTrue(any(context["reviewer_feedback"] for context in decider.contexts))

    def test_final_response_includes_evidence_summary_not_backend_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            response = self._supervisor(temp_dir, RecordingDecisionMaker()).handle_user_goal(
                "open https://example.com, summarize it, and save the summary to summary.txt"
            )

        self.assertIn("summary.txt", response.response)
        self.assertNotIn("OBJECTIVE_LOOP_DECISION", response.response)
        self.assertNotIn("ROUTER_EXECUTE", response.response)

    def test_thanks_after_objective_loop_stays_normal_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker()
            supervisor = self._supervisor(temp_dir, decider)
            supervisor.handle_user_goal("open https://example.com, summarize it, and save the summary to summary.txt")
            response = supervisor.handle_user_goal("thanks")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "conversation_fast_path")

    def test_ask_user_resumes_same_objective_on_next_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            decider = RecordingDecisionMaker("ask_user")
            supervisor = self._supervisor(temp_dir, decider)
            first = supervisor.handle_user_goal("Please handle this multi-step objective for the demo.")
            pending = supervisor.operator_context.get_short_term_state().pending_question
            second = supervisor.handle_user_goal("Use the project docs.")

        self.assertEqual(first.status, TaskStatus.BLOCKED)
        self.assertIsNotNone(pending)
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(len(task_state_store.list_tasks()), 1)
        self.assertTrue(any("user_answer: Use the project docs." in item for item in decider.contexts[-1]["recent_decisions"]))


if __name__ == "__main__":
    unittest.main()
