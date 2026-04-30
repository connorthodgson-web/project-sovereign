"""Regression coverage for the structured execution slice."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

from agents.coding_agent import CodingAgent
from agents.reviewer_agent import ReviewerAgent
from app.config import settings
from core.evaluator import GoalEvaluator
from core.fast_actions import FastActionHandler
from core.interaction_context import InteractionContext, bind_interaction_context
from core.operator_context import OperatorContextService, operator_context
from core.assistant import AssistantLayer
from core.conversation import ConversationalHandler
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ExecutionEscalation,
    FileEvidence,
    ToolEvidence,
    GoalEvaluation,
    ChatResponse,
    ObjectiveStage,
    RequestMode,
    RoutingDecision,
    SubTask,
    Task,
    TaskOutcome,
    TaskStatus,
    ToolInvocation,
)
from core.invocation_builders import BuiltInvocation
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore, task_state_store
from core.supervisor import Supervisor
from integrations.openrouter_client import OpenRouterClient
from integrations.search.contracts import SearchRequest, SearchResult, SearchSource
from tools.base_tool import BaseTool
from tools.browser_tool import BrowserTool
from tools.file_tool import FileTool
from tools.registry import ToolRegistry, build_default_tool_registry
from tools.runtime_tool import RuntimeTool
from memory.memory_store import MemoryStore


_PROVIDER_PATCHERS: list[object] = []


def setUpModule() -> None:
    for attr, value in (
        ("openrouter_api_key", None),
        ("openai_enabled", False),
        ("openai_api_key", None),
    ):
        patcher = patch.object(settings, attr, value)
        patcher.start()
        _PROVIDER_PATCHERS.append(patcher)


def tearDownModule() -> None:
    while _PROVIDER_PATCHERS:
        patcher = _PROVIDER_PATCHERS.pop()
        patcher.stop()


class FakeOpenRouterClient:
    """Simple fake for bounded LLM-assisted tests."""

    def __init__(self, response: str | None = None, *, configured: bool = True) -> None:
        self.response = response or ""
        self.configured = configured
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
        del kwargs
        self.prompt_calls.append(label)
        return self.response


class FakeSearchProvider:
    provider_name = "fake_search"

    def is_configured(self) -> bool:
        return True

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult(
            query=request.query,
            provider=self.provider_name,
            answer="Fake source-backed implementation-plan research.",
            sources=[SearchSource(title="Fake Source", url="https://example.com/research")],
        )


class FileToolTests(unittest.TestCase):
    """Cover safe workspace-scoped file operations."""

    def test_prevents_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = FileTool(temp_dir)

            result = tool.read_file("..\\outside.txt")

        self.assertFalse(result.success)
        self.assertIn("escapes the configured workspace", result.error or "")

    def test_create_read_and_list_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = FileTool(temp_dir)

            write_result = tool.write_file("hello.txt", "Hello from tests!")
            read_result = tool.read_file("hello.txt")
            list_result = tool.list_directory("created_items")

            self.assertTrue(write_result.success)
            self.assertEqual(Path(write_result.file_path or "").name, "hello.txt")
            self.assertEqual(Path(write_result.file_path or "").parent.name, "created_items")
            self.assertTrue(read_result.success)
            self.assertEqual(read_result.content, "Hello from tests!")
            self.assertTrue(list_result.success)
            self.assertIn("hello.txt", list_result.listed_entries)

    def test_write_defaults_bare_filename_into_created_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = FileTool(temp_dir)

            write_result = tool.write_file("notes.txt", "Generated note")

            created_file = Path(temp_dir) / "created_items" / "notes.txt"
            self.assertTrue(write_result.success)
            self.assertEqual(Path(write_result.file_path or ""), created_file)
            self.assertTrue(created_file.exists())
            self.assertEqual(created_file.read_text(encoding="utf-8"), "Generated note")

    def test_write_respects_explicit_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = FileTool(temp_dir)

            write_result = tool.write_file("app/something.py", "print('ok')")

            explicit_file = Path(temp_dir) / "app" / "something.py"
            self.assertTrue(write_result.success)
            self.assertEqual(Path(write_result.file_path or ""), explicit_file)
            self.assertTrue(explicit_file.exists())
            self.assertFalse((Path(temp_dir) / "created_items" / "app" / "something.py").exists())


class RuntimeToolTests(unittest.TestCase):
    """Cover narrow local runtime behavior."""

    def test_executes_simple_command_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = RuntimeTool(temp_dir)

            result = tool.execute(
                ToolInvocation(
                    tool_name="runtime_tool",
                    action="run",
                    parameters={"command": f'"{sys.executable}" -c "print(\'runtime ok\')"'},
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["payload"]["exit_code"], 0)
        self.assertIn("runtime ok", result["payload"]["stdout_preview"])

    def test_times_out_long_running_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = RuntimeTool(temp_dir, timeout_seconds=0.1)

            result = tool.execute(
                ToolInvocation(
                    tool_name="runtime_tool",
                    action="run",
                    parameters={"command": f'"{sys.executable}" -c "import time; time.sleep(1)"'},
                )
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["payload"]["timed_out"])
        self.assertIn("timed out", result["error"])


class PlannerTests(unittest.TestCase):
    """Cover planning behavior for structured tool execution."""

    class FakeInvocationBuilder:
        def can_build(self, goal: str) -> bool:
            return "dashboard" in goal.lower()

        def build(self, goal: str) -> BuiltInvocation:
            return BuiltInvocation(
                invocation=ToolInvocation(
                    tool_name="file_tool",
                    action="list",
                    parameters={"path": "."},
                ),
                execution_title="Execute alternate invocation",
                execution_description="Run the alternate deterministic invocation.",
                execution_objective=f"Execute alternate invocation for: {goal}",
                review_objective=f"Review alternate invocation for: {goal}",
            )

    def test_fallback_plan_routes_browser_goals_to_browser_agent(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan(
                "Use the browser to inspect the QA flow for our app."
            )

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[0].assigned_agent, "browser_agent")
        self.assertEqual(subtasks[1].depends_on, [subtasks[0].id])

    def test_file_plan_attaches_structured_tool_invocation(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Create a file called hello.txt with a short greeting")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(len(subtasks), 3)
        self.assertEqual(subtasks[1].assigned_agent, "coding_agent")
        self.assertIsNotNone(subtasks[1].tool_invocation)
        self.assertEqual(subtasks[1].tool_invocation.tool_name, "file_tool")
        self.assertEqual(subtasks[1].tool_invocation.action, "write")
        self.assertEqual(subtasks[1].tool_invocation.parameters["path"], "hello.txt")

    def test_python_file_goal_defaults_to_python_extension(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Create a python file")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(subtasks[1].tool_invocation.parameters["path"], "script.py")
        self.assertIn("print(", subtasks[1].tool_invocation.parameters["content"])

    def test_python_file_goal_adds_missing_python_extension_to_name(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Create a python file called hello")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(subtasks[1].tool_invocation.parameters["path"], "hello.py")

    def test_bounded_python_script_plan_creates_and_runs_artifact(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Build a tiny Python script that prints hello")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(subtasks[1].assigned_agent, "coding_agent")
        self.assertEqual(subtasks[1].tool_invocation.tool_name, "file_tool")
        self.assertEqual(subtasks[1].tool_invocation.parameters["path"], "hello.py")
        self.assertEqual(subtasks[2].assigned_agent, "coding_agent")
        self.assertEqual(subtasks[2].tool_invocation.tool_name, "runtime_tool")
        self.assertIn("created_items/hello.py", subtasks[2].tool_invocation.parameters["command"])

    def test_simple_readme_plan_creates_real_file_artifact(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Make a simple README file")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(subtasks[1].assigned_agent, "coding_agent")
        self.assertEqual(subtasks[1].tool_invocation.tool_name, "file_tool")
        self.assertEqual(subtasks[1].tool_invocation.parameters["path"], "README.md")

    def test_file_plan_respects_explicit_nested_path(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan(
                "Create a file at app/something.py with a short greeting"
            )

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(subtasks[1].tool_invocation.parameters["path"], "app/something.py")

    def test_runtime_plan_attaches_structured_tool_invocation(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Run python --version")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(len(subtasks), 3)
        self.assertEqual(subtasks[1].assigned_agent, "coding_agent")
        self.assertIsNotNone(subtasks[1].tool_invocation)
        self.assertEqual(subtasks[1].tool_invocation.tool_name, "runtime_tool")
        self.assertEqual(subtasks[1].tool_invocation.action, "run")
        self.assertEqual(subtasks[1].tool_invocation.parameters["command"], "python --version")

    def test_builder_registration_selects_matching_invocation_builder(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(
                openrouter_client=OpenRouterClient(api_key=None),
                invocation_builders=[self.FakeInvocationBuilder()],
            )

            subtasks, planner_mode = planner.create_plan("Inspect the dashboard task wiring")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(len(subtasks), 3)
        self.assertEqual(subtasks[1].title, "Execute alternate invocation")
        self.assertIsNotNone(subtasks[1].tool_invocation)
        self.assertEqual(subtasks[1].tool_invocation.action, "list")

    def test_fallback_plan_keeps_browser_research_with_research_agent(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            planner = Planner(openrouter_client=OpenRouterClient(api_key=None))

            subtasks, planner_mode = planner.create_plan("Research browser automation options for this site.")

        self.assertEqual(planner_mode, "deterministic")
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[0].assigned_agent, "research_agent")

    def test_planner_validation_drops_incompatible_llm_tool_invocation(self) -> None:
        registry = ToolRegistry()
        registry.register(FileTool())
        registry.register(BrowserTool())
        planner = Planner(
            tool_registry=registry,
            openrouter_client=FakeOpenRouterClient(
                response=(
                    '{"subtasks":['
                    '{"title":"Capture","description":"Capture context","objective":"Capture context","agent_hint":"memory_agent","tool_invocation":null},'
                    '{"title":"Execute","description":"Run browser action","objective":"Run browser action","agent_hint":"coding_agent",'
                    '"tool_invocation":{"tool_name":"browser_tool","action":"open","parameters":{"url":"https://example.com"}}},'
                    '{"title":"Review","description":"Review result","objective":"Review result","agent_hint":"reviewer_agent","tool_invocation":null}'
                    "]}"
                )
            ),
        )

        subtasks, planner_mode = planner.create_plan(
            "Use a browser to inspect the requested site once the target page is identified."
        )

        self.assertEqual(planner_mode, "openrouter")
        self.assertIsNone(subtasks[1].tool_invocation)
        self.assertIn("Planner validation rejected tool invocation", subtasks[1].notes[0])


class AssistantLayerTests(unittest.TestCase):
    """Cover request-mode decisions and reply composition."""

    def test_llm_mode_decision_is_used_when_available(self) -> None:
        layer = AssistantLayer(
            openrouter_client=FakeOpenRouterClient(
                response='{"mode":"ACT","reasoning":"The user wants one small file action.","should_use_tools":true}'
            )
        )

        decision = layer.decide("Compare two possible implementation approaches")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertEqual(decision.escalation_level, ExecutionEscalation.SINGLE_ACTION)
        self.assertTrue(decision.should_use_tools)
        self.assertIn("small file action", decision.reasoning)

    def test_deterministic_decision_escalates_bounded_research_request(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("Research browser automation options and summarize the tradeoffs.")

        self.assertEqual(decision.mode, RequestMode.EXECUTE)
        self.assertEqual(decision.escalation_level, ExecutionEscalation.BOUNDED_TASK_EXECUTION)

    def test_greeting_skips_llm_even_when_available(self) -> None:
        fake_openrouter = FakeOpenRouterClient(
            response='{"mode":"ACT","reasoning":"Use the LLM-selected path.","should_use_tools":true}'
        )
        layer = AssistantLayer(openrouter_client=fake_openrouter)

        decision = layer.decide("hi")

        self.assertEqual(decision.mode, RequestMode.ANSWER)
        self.assertIn("immediate conversational reply", decision.reasoning.lower())
        self.assertEqual(fake_openrouter.prompt_calls, [])

    def test_deterministic_decision_escalates_objective_completion_request(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("Build the reminder system and keep going until it works or you're blocked.")

        self.assertEqual(decision.mode, RequestMode.EXECUTE)
        self.assertEqual(decision.escalation_level, ExecutionEscalation.OBJECTIVE_COMPLETION)

    def test_deterministic_mode_treats_recent_history_questions_as_answer(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("Show me the files you created.")

        self.assertEqual(decision.mode, RequestMode.ANSWER)
        self.assertFalse(decision.should_use_tools)

    def test_preference_statement_is_answer_mode(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("Please keep answers concise.")

        self.assertEqual(decision.mode, RequestMode.ANSWER)
        self.assertFalse(decision.should_use_tools)

    def test_greeting_stays_answer_mode(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("hi")

        self.assertEqual(decision.mode, RequestMode.ANSWER)
        self.assertFalse(decision.should_use_tools)

    def test_planning_discussion_stays_answer_mode(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("Help me plan the next step for Project Sovereign.")

        self.assertEqual(decision.mode, RequestMode.ANSWER)
        self.assertFalse(decision.should_use_tools)

    def test_question_shaped_action_request_is_not_forced_into_answer_mode(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))

        decision = layer.decide("Can you write a quicksort function?")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)

    def test_deterministic_composer_translates_blocked_state_for_user(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))
        task = Task(
            goal="Open the requested browser page",
            title="Open browser page",
            description="Open browser page",
            status=TaskStatus.BLOCKED,
            request_mode=RequestMode.EXECUTE,
            results=[
                AgentResult(
                    subtask_id="1",
                    agent="memory_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Task context was normalized for short-term in-memory tracking.",
                ),
                AgentResult(
                    subtask_id="2",
                    agent="browser_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Browser execution is blocked.",
                    blockers=["live browser execution is not wired into the runtime"],
                    next_actions=["Enable the browser adapter for this environment"],
                ),
            ],
        )

        reply = layer.compose_task_response(
            task,
            AssistantDecision(
                mode=RequestMode.EXECUTE,
                reasoning="This needs multi-step execution.",
                should_use_tools=True,
            ),
            TaskOutcome(completed=1, blocked=1, total_subtasks=2),
            GoalEvaluation(
                satisfied=False,
                reasoning="Execution is blocked.",
                missing=["Enable browser execution"],
            ),
            "deterministic",
        )

        self.assertIn("blocked", reply.lower())
        self.assertIn("browser access", reply.lower())
        self.assertNotIn("captured the request context", reply.lower())

    def test_deterministic_action_reply_stays_honest_when_only_planned_work_exists(self) -> None:
        layer = AssistantLayer(openrouter_client=FakeOpenRouterClient(configured=False))
        task = Task(
            goal="Write a quicksort function",
            title="Write a quicksort function",
            description="Write a quicksort function",
            status=TaskStatus.RUNNING,
            request_mode=RequestMode.ACT,
            results=[
                AgentResult(
                    subtask_id="1",
                    agent="coding_agent",
                    status=AgentExecutionStatus.PLANNED,
                    summary="Implementation work was mapped into concrete next steps, but no executable tool invocation was attached to this subtask.",
                    next_actions=["Attach a supported tool invocation during planning before executing this subtask."],
                )
            ],
        )

        reply = layer.compose_task_response(
            task,
            AssistantDecision(
                mode=RequestMode.ACT,
                reasoning="Single concrete action.",
                should_use_tools=True,
            ),
            TaskOutcome(planned=1, total_subtasks=1),
            GoalEvaluation(
                satisfied=False,
                reasoning="The work was only planned.",
                missing=["Attach a tool invocation"],
            ),
            "deterministic",
        )

        self.assertIn("not done", reply.lower())
        self.assertIn("attach a supported tool invocation", reply.lower())


class ConversationalHandlerTests(unittest.TestCase):
    """Cover lightweight ANSWER-mode behavior."""

    def setUp(self) -> None:
        task_state_store._tasks.clear()
        operator_context.memory_store.reset()

    def test_uses_recent_task_context_for_last_task_question(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))
        task_state_store.add_task(
            Task(
                goal="Create hello.txt",
                title="Create hello.txt",
                description="Create hello.txt",
                status=TaskStatus.COMPLETED,
                request_mode=RequestMode.ACT,
                summary="Done. I created `created_items/hello.txt` and verified it.",
                results=[
                    AgentResult(
                        subtask_id="write-1",
                        agent="coding_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary="Created workspace file at hello.txt.",
                        tool_name="file_tool",
                        evidence=[
                            FileEvidence(
                                file_path=str(Path(settings.workspace_root) / "created_items" / "hello.txt"),
                                operation="write",
                                content_preview="Hello from tests!",
                            )
                        ],
                    )
                ],
            )
        )

        response = handler.handle(
            "What did you just do?",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Direct question about recent activity.",
                should_use_tools=False,
            ),
        )

        self.assertEqual(response.planner_mode, "conversation")
        self.assertIn("create hello.txt", response.response.lower())
        self.assertIn("created", response.response.lower())

    def test_reports_recent_created_files_without_execution_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            created_file = Path(temp_dir) / "created_items" / "note.txt"
            created_file.parent.mkdir(parents=True, exist_ok=True)
            created_file.write_text("hello", encoding="utf-8")
            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                workspace_root=temp_dir,
            )

            response = handler.handle(
                "Show me the files you created.",
                AssistantDecision(
                    mode=RequestMode.ANSWER,
                    reasoning="Direct question about recent files.",
                    should_use_tools=False,
                ),
            )

        self.assertIn("note.txt", response.response)
        self.assertEqual(response.outcome.total_subtasks, 0)

    def test_greeting_does_not_leak_active_task_state(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))
        task_state_store.add_task(
            Task(
                goal="Build the reminder system",
                title="Build the reminder system",
                description="Build the reminder system",
                status=TaskStatus.RUNNING,
                request_mode=RequestMode.EXECUTE,
            )
        )

        response = handler.handle(
            "hi",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Greeting.",
                should_use_tools=False,
            ),
        )

        self.assertIn("hi", response.response.lower())
        self.assertNotIn("build the reminder system", response.response.lower())
        self.assertNotIn("ready to help", response.response.lower())

    def test_describes_identity_from_centralized_context(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))

        response = handler.handle(
            "Who are you?",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Direct identity question.",
                should_use_tools=False,
            ),
        )

        self.assertIn("Sovereign", response.response)
        self.assertIn("operator", response.response.lower())

    def test_simple_math_uses_lightweight_answer_path(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))

        response = handler.handle(
            "2 + 2",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Direct math prompt.",
                should_use_tools=False,
            ),
        )

        self.assertEqual(response.response, "4")

    def test_acknowledges_preference_statement_naturally(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))

        response = handler.handle(
            "Please keep answers concise.",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Preference update.",
                should_use_tools=False,
            ),
        )

        self.assertIn("concise", response.response.lower())

    def test_planning_discussion_reply_stays_conversational(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))

        response = handler.handle(
            "Help me plan the next step.",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Planning discussion should stay conversational.",
                should_use_tools=False,
            ),
        )

        self.assertIn("conversational", response.response.lower())
        self.assertIn("next step", response.response.lower())

    def test_capabilities_reply_stays_natural_about_non_live_features(self) -> None:
        handler = ConversationalHandler(openrouter_client=FakeOpenRouterClient(configured=False))

        response = handler.handle(
            "What can you do?",
            AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="Capabilities question.",
                should_use_tools=False,
            ),
        )

        self.assertIn("right now i can", response.response.lower())
        self.assertIn("run bounded browser tasks", response.response.lower())
        self.assertNotIn("tracking unfinished areas honestly", response.response.lower())


class CodingAgentTests(unittest.TestCase):
    """Cover execution from structured tool invocation."""

    class MockRuntimeTool(BaseTool):
        name = "runtime_tool"

        def supports(self, invocation: ToolInvocation) -> bool:
            return invocation.tool_name == self.name and invocation.action == "run"

        def execute(self, invocation: ToolInvocation) -> dict:
            command = invocation.parameters.get("command", "")
            return {
                "success": True,
                "summary": f"Executed runtime command: {command}",
                "payload": {"command": command, "exit_code": 0},
                "stdout_preview": "Runtime OK",
            }

    def test_executes_structured_tool_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = CodingAgent(file_tool=FileTool(temp_dir))
            task = Task(goal="Create hello.txt", title="Create hello.txt", description="Create hello.txt")
            subtask = SubTask(
                title="Execute workspace file task",
                description="Write a file",
                objective="Write a file in the workspace",
                assigned_agent="coding_agent",
                tool_invocation=ToolInvocation(
                    tool_name="file_tool",
                    action="write",
                    parameters={"path": "hello.txt", "content": "Hello from invocation!"},
                ),
            )

            result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.evidence[0].operation, "write")
        self.assertEqual(Path(result.evidence[0].file_path or "").name, "hello.txt")

    def test_executes_non_file_tool_via_generic_executor_path(self) -> None:
        registry = ToolRegistry()
        registry.register(self.MockRuntimeTool())
        agent = CodingAgent(
            tool_registry=registry,
            supported_tool_names=frozenset({"runtime_tool"}),
        )
        task = Task(goal="Run the runtime tool", title="Run runtime tool", description="Run runtime tool")
        subtask = SubTask(
            title="Execute runtime task",
            description="Run a mocked runtime command",
            objective="Execute a generic non-file tool",
            assigned_agent="coding_agent",
            tool_invocation=ToolInvocation(
                tool_name="runtime_tool",
                action="run",
                parameters={"command": "pytest -q"},
            ),
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertIsInstance(result.evidence[0], ToolEvidence)
        self.assertEqual(result.evidence[0].tool_name, "runtime_tool")
        self.assertEqual(result.evidence[0].payload["command"], "pytest -q")
        self.assertEqual(result.evidence[0].payload["stdout_preview"], "Runtime OK")
        self.assertEqual(result.summary, "Executed runtime command: pytest -q")

    def test_blocks_incompatible_tool_invocation(self) -> None:
        agent = CodingAgent()
        task = Task(goal="Browse the app", title="Browse the app", description="Browse the app")
        subtask = SubTask(
            title="Execute browser task",
            description="Attempt browser execution",
            objective="Use a browser tool",
            assigned_agent="coding_agent",
            tool_invocation=ToolInvocation(
                tool_name="browser_tool",
                action="open",
                parameters={"url": "https://example.com"},
            ),
        )

        result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("does not support tool 'browser_tool'", result.blockers[0])


class ReviewerAgentTests(unittest.TestCase):
    """Cover reviewer behavior over executable evidence, not agent identity."""

    def test_reviewer_finds_latest_executable_result_without_coding_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = FileTool(temp_dir)
            tool.write_file("hello.txt", "Hello from tests!")
            reviewer = ReviewerAgent(file_tool=tool)

            execution_subtask = SubTask(
                id="execute-1",
                title="Execute workspace file task",
                description="Write a file",
                objective="Write a file in the workspace",
                assigned_agent="custom_executor",
                tool_invocation=ToolInvocation(
                    tool_name="file_tool",
                    action="write",
                    parameters={"path": "hello.txt", "content": "Hello from tests!"},
                ),
            )
            review_subtask = SubTask(
                id="review-1",
                title="Review workspace file result",
                description="Verify the file operation",
                objective="Review the workspace file execution result",
                assigned_agent="reviewer_agent",
            )
            task = Task(
                goal="Create hello.txt",
                title="Create hello.txt",
                description="Create hello.txt",
                subtasks=[execution_subtask, review_subtask],
                results=[
                    AgentResult(
                        subtask_id="execute-1",
                        agent="custom_executor",
                        status=AgentExecutionStatus.COMPLETED,
                        summary="Created workspace file at hello.txt.",
                        tool_name="file_tool",
                        evidence=[
                            FileEvidence(
                                file_path="hello.txt",
                                operation="write",
                                content_preview="Hello from tests!",
                            )
                        ],
                    )
                ],
            )

            result = reviewer.run(task, review_subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.tool_name, "file_tool")
        self.assertTrue(any("Verified created file exists" in note for note in result.evidence[0].verification_notes))

    def test_reviewer_adds_generic_review_for_non_file_tool_results(self) -> None:
        reviewer = ReviewerAgent()
        review_subtask = SubTask(
            id="review-2",
            title="Review current result",
            description="Verify the latest output",
            objective="Review the latest result",
            assigned_agent="reviewer_agent",
        )
        task = Task(
            goal="Check a future browser result",
            title="Check browser result",
            description="Check browser result",
            results=[
                AgentResult(
                    subtask_id="execute-browser",
                    agent="custom_executor",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Opened a browser page.",
                    tool_name="browser_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="browser_tool",
                            summary="Opened the requested page.",
                            payload={
                                "requested_url": "https://example.com",
                                "final_url": "https://example.com",
                                "title": "Example Domain",
                                "headings": ["Example Domain"],
                                "text_preview": "Example Domain page text.",
                                "screenshot_path": "C:/tmp/fake-browser.png",
                                "browser_task": {"synthesis_result": "Example Domain page text."},
                            },
                        )
                    ],
                )
            ],
        )

        result = reviewer.run(task, review_subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.tool_name, "browser_tool")
        self.assertIn("not semantic perfection", result.details[-1].lower())
        self.assertIn("Verified browser execution completed", result.evidence[0].verification_notes[0])

    def test_reviewer_verifies_runtime_result_fields(self) -> None:
        reviewer = ReviewerAgent()
        review_subtask = SubTask(
            id="review-runtime",
            title="Review runtime result",
            description="Verify the latest runtime output",
            objective="Review the runtime command result",
            assigned_agent="reviewer_agent",
        )
        task = Task(
            goal="Run python --version",
            title="Run python --version",
            description="Run python --version",
            results=[
                AgentResult(
                    subtask_id="execute-runtime",
                    agent="coding_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Executed runtime command successfully.",
                    tool_name="runtime_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="runtime_tool",
                            summary="Executed runtime command successfully.",
                            payload={
                                "command": "python --version",
                                "exit_code": 0,
                                "stdout_preview": "Python 3.11.0",
                                "stderr_preview": None,
                                "timed_out": False,
                            },
                        )
                    ],
                )
            ],
        )

        result = reviewer.run(task, review_subtask)

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.tool_name, "runtime_tool")
        self.assertIn("exit code was captured", " ".join(result.evidence[0].verification_notes))


class RouterTests(unittest.TestCase):
    """Cover bounded routing with deterministic and LLM-assisted paths."""

    def test_routing_fallback_behavior_without_llm(self) -> None:
        router = Router(openrouter_client=FakeOpenRouterClient(configured=False))
        subtask = SubTask(
            title="Verify execution honesty",
            description="Review the routed outputs for correctness",
            objective="Critique the result and verify the file task",
        )

        decision = router.assign_agent(subtask)

        self.assertIsInstance(decision, RoutingDecision)
        self.assertEqual(decision.agent_name, "reviewer_agent")
        self.assertEqual(decision.strategy, "deterministic")

    def test_routing_uses_llm_when_available(self) -> None:
        router = Router(
            openrouter_client=FakeOpenRouterClient(
                response='{"agent_name":"research_agent","reasoning":"The subtask asks for dependency investigation."}'
            )
        )
        subtask = SubTask(
            title="Investigate dependencies",
            description="Map the integration prerequisites",
            objective="Research what the current backend depends on",
        )

        decision = router.assign_agent(subtask)

        self.assertEqual(decision.agent_name, "research_agent")
        self.assertEqual(decision.strategy, "openrouter")
        self.assertIn("dependency investigation", decision.reasoning)

    def test_router_conservative_fallback_prefers_research_for_ambiguous_browser_topic(self) -> None:
        router = Router(openrouter_client=FakeOpenRouterClient(configured=False))
        subtask = SubTask(
            title="Interpret browser automation options",
            description="Research possible browser automation approaches",
            objective="Compare browser automation options for this site",
        )

        decision = router.assign_agent(subtask)

        self.assertEqual(decision.agent_name, "research_agent")

    def test_router_prefers_memory_agent_for_context_recall_work(self) -> None:
        router = Router(openrouter_client=FakeOpenRouterClient(configured=False))
        subtask = SubTask(
            title="Recall user context",
            description="Retrieve the saved preference and prior context",
            objective="Remember the user's stored preferences for concise answers",
        )

        decision = router.assign_agent(subtask)

        self.assertEqual(decision.agent_name, "memory_agent")
        self.assertEqual(decision.strategy, "deterministic")


class ToolRegistryTests(unittest.TestCase):
    """Cover bounded tool lookup for the file tool path."""

    def test_tool_registry_executes_via_tool_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = build_default_tool_registry(file_tool=FileTool(temp_dir))

            tool = registry.get("file_tool")
            payload = registry.execute(
                ToolInvocation(tool_name="file_tool", action="list", parameters={"path": "."})
            )

        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "file_tool")
        self.assertEqual(payload["operation"], "list")
        self.assertTrue(payload["success"])


class GoalEvaluatorTests(unittest.TestCase):
    """Cover deterministic fallback evaluation behavior."""

    def test_deterministic_evaluation_uses_reviewer_evidence(self) -> None:
        task = Task(goal="Read the file hello.txt", title="Read hello.txt", description="Read hello.txt")
        task.results = [
            AgentResult(
                subtask_id="1",
                agent="coding_agent",
                status=AgentExecutionStatus.COMPLETED,
                summary="Read workspace file",
                tool_name="file_tool",
                evidence=[
                    FileEvidence(
                        file_path="C:\\workspace\\hello.txt",
                        operation="read",
                        content_preview="Hello from Project Sovereign!",
                    )
                ],
            ),
            AgentResult(
                subtask_id="2",
                agent="reviewer_agent",
                status=AgentExecutionStatus.COMPLETED,
                summary="Reviewed workspace read result successfully.",
                tool_name="file_tool",
                evidence=[
                    FileEvidence(
                        file_path="C:\\workspace\\hello.txt",
                        operation="read",
                        content_preview="Hello from Project Sovereign!",
                        verification_notes=[
                            "Expected normalized path: hello.txt; actual normalized path: hello.txt.",
                            "Verified read operation returned content.",
                        ],
                    )
                ],
            ),
        ]

        with patch.object(settings, "openrouter_api_key", None):
            evaluation, mode = GoalEvaluator().evaluate(task)

        self.assertEqual(mode, "deterministic")
        self.assertTrue(evaluation.satisfied)
        self.assertIn("Reviewer verified", evaluation.reasoning)

    def test_deterministic_evaluation_accepts_reviewed_non_file_evidence(self) -> None:
        task = Task(
            goal="Open the requested page",
            title="Open page",
            description="Open page",
            results=[
                AgentResult(
                    subtask_id="1",
                    agent="browser_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Opened the requested page.",
                    tool_name="browser_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="browser_tool",
                            summary="Opened the requested page.",
                            payload={
                                "requested_url": "https://example.com",
                                "final_url": "https://example.com",
                                "title": "Example Domain",
                                "headings": ["Example Domain"],
                                "text_preview": "Example Domain page text.",
                                "screenshot_path": "C:/tmp/fake-browser.png",
                                "browser_task": {"synthesis_result": "Example Domain page text."},
                            },
                        )
                    ],
                ),
                AgentResult(
                    subtask_id="2",
                    agent="reviewer_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Confirmed browser execution completed and produced concrete evidence.",
                    tool_name="browser_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="browser_tool",
                            summary="Opened the requested page.",
                            payload={
                                "requested_url": "https://example.com",
                                "final_url": "https://example.com",
                                "title": "Example Domain",
                                "headings": ["Example Domain"],
                                "text_preview": "Example Domain page text.",
                                "screenshot_path": "C:/tmp/fake-browser.png",
                                "browser_task": {"synthesis_result": "Example Domain page text."},
                            },
                            verification_notes=[
                                "Verified execution completed and emitted evidence for browser_tool."
                            ],
                        )
                    ],
                ),
            ],
        )

        with patch.object(settings, "openrouter_api_key", None):
            evaluation, mode = GoalEvaluator().evaluate(task)

        self.assertEqual(mode, "deterministic")
        self.assertTrue(evaluation.satisfied)
        self.assertIn("Reviewer verified", evaluation.reasoning)

    def test_deterministic_evaluation_accepts_reviewed_runtime_evidence(self) -> None:
        task = Task(
            goal="Run python --version",
            title="Run python --version",
            description="Run python --version",
            results=[
                AgentResult(
                    subtask_id="1",
                    agent="coding_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Executed runtime command 'python --version' successfully.",
                    tool_name="runtime_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="runtime_tool",
                            summary="Executed runtime command 'python --version' successfully.",
                            payload={
                                "command": "python --version",
                                "exit_code": 0,
                                "stdout_preview": "Python 3.11.0",
                                "stderr_preview": None,
                                "timed_out": False,
                            },
                        )
                    ],
                ),
                AgentResult(
                    subtask_id="2",
                    agent="reviewer_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Reviewed runtime command result successfully.",
                    tool_name="runtime_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="runtime_tool",
                            summary="Executed runtime command 'python --version' successfully.",
                            payload={
                                "command": "python --version",
                                "exit_code": 0,
                                "stdout_preview": "Python 3.11.0",
                                "stderr_preview": None,
                                "timed_out": False,
                            },
                            verification_notes=[
                                "Verified runtime execution completed.",
                                "Verified runtime command was captured: python --version",
                                "Verified runtime exit code was captured: 0",
                                "Verified runtime output preview was captured.",
                            ],
                        )
                    ],
                ),
            ],
        )

        with patch.object(settings, "openrouter_api_key", None):
            evaluation, mode = GoalEvaluator().evaluate(task)

        self.assertEqual(mode, "deterministic")
        self.assertTrue(evaluation.satisfied)
        self.assertIn("runtime command completed", evaluation.reasoning)

    def test_llm_evaluation_is_overridden_without_reviewed_evidence(self) -> None:
        task = Task(
            goal="Open the requested page",
            title="Open page",
            description="Open page",
            results=[
                AgentResult(
                    subtask_id="1",
                    agent="browser_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Opened the requested page.",
                    tool_name="browser_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="browser_tool",
                            summary="Opened the requested page.",
                            payload={"url": "https://example.com"},
                        )
                    ],
                )
            ],
        )
        evaluator = GoalEvaluator(
            openrouter_client=FakeOpenRouterClient(
                response='{"satisfied":true,"reasoning":"Looks complete.","missing":[]}'
            )
        )

        evaluation, mode = evaluator.evaluate(task)

        self.assertEqual(mode, "openrouter")
        self.assertFalse(evaluation.satisfied)
        self.assertIn("overridden", evaluation.reasoning)

    def test_evaluator_rejects_simulated_coding_success_without_evidence(self) -> None:
        task = Task(
            goal="Build a tiny Python script that prints hello",
            title="Build script",
            description="Build script",
            results=[
                AgentResult(
                    subtask_id="1",
                    agent="coding_agent",
                    status=AgentExecutionStatus.SIMULATED,
                    summary="Simulated coding work is complete.",
                    tool_name="coding_artifact",
                )
            ],
        )

        with patch.object(settings, "openrouter_api_key", None):
            evaluation, mode = GoalEvaluator().evaluate(task)

        self.assertEqual(mode, "deterministic")
        self.assertFalse(evaluation.satisfied)
        self.assertIn("No completed reviewer verification", evaluation.reasoning)


class SupervisorTests(unittest.TestCase):
    """Cover end-to-end orchestration behavior."""

    def setUp(self) -> None:
        task_state_store._tasks.clear()
        operator_context.memory_store.reset()

    class FakeReminderService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def schedule_one_time_reminder(
            self,
            *,
            summary: str,
            deliver_at,
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
                reminder_id = "reminder-123"

            return True, "scheduled", Record(), []

    class AnswerOnlyAssistantLayer:
        def decide(self, _: str) -> AssistantDecision:
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                reasoning="This is a conversational request.",
                should_use_tools=False,
            )

        def build_answer_response(self, _: str, decision: AssistantDecision) -> ChatResponse:
            return ChatResponse(
                task_id="answer-1",
                status=TaskStatus.COMPLETED,
                planner_mode="assistant",
                request_mode=decision.mode,
                response="Here is the direct answer.",
                outcome=TaskOutcome(total_subtasks=0),
                subtasks=[],
                results=[],
            )

    def _build_local_supervisor(
        self,
        fake_openrouter: FakeOpenRouterClient,
        memory_path: Path,
    ) -> tuple[Supervisor, MemoryStore]:
        store = MemoryStore(memory_path)
        service = OperatorContextService(
            openrouter_client=fake_openrouter,
            memory_store_instance=store,
            task_store=TaskStateStore(),
        )
        assistant = AssistantLayer(
            openrouter_client=fake_openrouter,
            operator_context_service=service,
        )
        return (
            Supervisor(
                assistant_layer=assistant,
                operator_context_service=service,
            ),
            store,
        )

    def test_supervisor_returns_structured_response_for_non_file_goal(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                "Design the implementation plan for a modular backend operator."
            )

        self.assertEqual(response.planner_mode, "deterministic")
        self.assertEqual(response.outcome.total_subtasks, 2)
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(response.results[0].tool_name, "web_search_tool")
        self.assertIn("source-backed research", response.results[0].summary.lower())
        self.assertEqual(response.status, TaskStatus.BLOCKED)

    def test_supervisor_stops_when_goal_evaluation_is_satisfied(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            supervisor = Supervisor()
            with (
                patch("agents.research_agent.build_search_provider", return_value=FakeSearchProvider()),
                patch.object(
                    supervisor.evaluator,
                    "evaluate",
                    return_value=(
                        GoalEvaluation(satisfied=True, reasoning="Enough evidence was collected.", missing=[]),
                        "deterministic",
                    ),
                ),
            ):
                response = supervisor.handle_user_goal(
                    "Design the implementation plan for a modular backend operator."
                )

        self.assertEqual(len(response.results), 2)
        self.assertEqual(response.status, TaskStatus.COMPLETED)

    def test_supervisor_can_short_circuit_into_answer_mode(self) -> None:
        supervisor = Supervisor(assistant_layer=self.AnswerOnlyAssistantLayer())

        response = supervisor.handle_user_goal("Hi there")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "assistant")
        self.assertEqual(response.response, "Here is the direct answer.")
        self.assertEqual(response.outcome.total_subtasks, 0)
        self.assertEqual(task_state_store.list_tasks(), [])

    def test_simple_greeting_stays_on_fast_assistant_path_without_openrouter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, _store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal("hi")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "conversation_fast_path")
        self.assertEqual(response.response, "Hi. What can I help with?")
        self.assertEqual(fake_openrouter.prompt_calls, [])
        self.assertEqual(task_state_store.list_tasks(), [])
        combined = "\n".join(logs.output)
        self.assertIn("SUPERVISOR_TRACE", combined)
        self.assertIn("assistant_path=assistant_fast_path", combined)
        self.assertIn("openrouter_called=False", combined)

    def test_simple_name_statement_avoids_execution_and_persists_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            response = supervisor.handle_user_goal("my name is Connor Hodgson")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertEqual(response.planner_mode, "conversation_fast_path")
        self.assertIn("Connor Hodgson", response.response)
        self.assertEqual(fake_openrouter.prompt_calls, [])
        self.assertEqual(task_state_store.list_tasks(), [])
        user_name = next(fact for fact in store.list_facts("user") if fact.key == "user:name")
        self.assertEqual(user_name.value, "Your name is Connor Hodgson.")

    def test_remember_that_statement_uses_light_memory_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal("remember that my name is Connor")

        self.assertEqual(response.planner_mode, "conversation_fast_path")
        self.assertEqual(fake_openrouter.prompt_calls, [])
        self.assertTrue(any(fact.key == "user:name" for fact in store.list_facts("user")))
        combined = "\n".join(logs.output)
        self.assertIn("assistant_path=assistant_fast_path", combined)
        self.assertIn("memory_write_ops", combined)

    def test_memory_question_uses_memory_layer_without_openrouter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, _store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            supervisor.handle_user_goal("my name is Connor Hodgson")
            response = supervisor.handle_user_goal("what do you remember about me?")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertIn("Connor Hodgson", response.response)
        self.assertEqual(fake_openrouter.prompt_calls, [])

    def test_memory_follow_up_stays_on_memory_fast_path_without_openrouter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, _store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            supervisor.handle_user_goal("my name is Connor Hodgson")
            supervisor.handle_user_goal("what do you remember about me?")
            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal("Is that all you have in memory?")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertIn("local memory", response.response.lower())
        self.assertEqual(fake_openrouter.prompt_calls, [])
        self.assertEqual(task_state_store.list_tasks(), [])
        combined = "\n".join(logs.output)
        self.assertIn("assistant_path=assistant_memory_fast_path", combined)
        self.assertIn("openrouter_called=False", combined)
        self.assertNotIn("planner_mode=deterministic", combined)

    def test_memory_what_else_follow_up_stays_on_memory_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, _store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            supervisor.handle_user_goal("Remember that I prefer concise answers.")
            supervisor.handle_user_goal("What do you remember about me?")
            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal("What else do you remember?")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertIn("don't have anything beyond that", response.response.lower())
        self.assertEqual(fake_openrouter.prompt_calls, [])
        self.assertEqual(task_state_store.list_tasks(), [])
        combined = "\n".join(logs.output)
        self.assertIn("assistant_path=assistant_memory_fast_path", combined)
        self.assertIn("openrouter_called=False", combined)

    def test_forget_my_name_uses_light_memory_path_and_deletes_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            fake_openrouter = FakeOpenRouterClient(
                response='{"mode":"EXECUTE","reasoning":"wrong","should_use_tools":true}',
                configured=False,
            )
            supervisor, store = self._build_local_supervisor(
                fake_openrouter,
                Path(temp_dir) / "memory.json",
            )

            supervisor.handle_user_goal("my name is Connor Hodgson")
            with self.assertLogs("core.supervisor", level="INFO") as logs:
                response = supervisor.handle_user_goal("forget my name")

        self.assertEqual(response.request_mode, RequestMode.ANSWER)
        self.assertIn("forgot your name", response.response.lower())
        self.assertEqual(fake_openrouter.prompt_calls, [])
        self.assertFalse(any(fact.key == "user:name" for fact in store.list_facts("user")))
        combined = "\n".join(logs.output)
        self.assertIn("assistant_path=assistant_fast_path", combined)
        self.assertIn("memory_write_ops", combined)

    def test_complex_request_still_uses_supervisor_and_planner(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                "Research browser automation options and summarize the tradeoffs."
            )

        self.assertEqual(response.request_mode, RequestMode.EXECUTE)
        self.assertGreater(response.outcome.total_subtasks, 0)
        self.assertTrue(task_state_store.list_tasks())

    def test_supervisor_uses_fast_path_for_simple_reminder(self) -> None:
        reminder_service = self.FakeReminderService()
        fast_actions = FastActionHandler(
            operator_context_service=operator_context,
            reminder_service=reminder_service,
            openrouter_client=FakeOpenRouterClient(configured=False),
        )
        supervisor = Supervisor(fast_action_handler=fast_actions)

        with bind_interaction_context(
            InteractionContext(source="slack", channel_id="D123", user_id="U123")
        ):
            response = supervisor.handle_user_goal("Remind me in 2 minutes to drink water.")

        self.assertEqual(response.planner_mode, "fast_action")
        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertIn("drink water", response.response.lower())
        self.assertEqual(len(task_state_store.list_tasks()), 0)
        self.assertEqual(reminder_service.calls[0]["channel_id"], "D123")

    def test_supervisor_executes_and_reviews_workspace_file_flow(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                "Create a file called hello.txt with a short greeting"
            )

            created_file = Path(temp_dir) / "created_items" / "hello.txt"

            self.assertTrue(created_file.exists())
            self.assertEqual(created_file.read_text(encoding="utf-8"), "Hello from Project Sovereign!")
            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertEqual(response.escalation_level, ExecutionEscalation.SINGLE_ACTION)
            self.assertEqual(response.planner_mode, "fast_action")
            self.assertEqual(response.outcome.total_subtasks, 1)
            self.assertEqual(len(response.results), 1)
            self.assertEqual(response.results[0].status, AgentExecutionStatus.COMPLETED)
            self.assertEqual(response.results[0].evidence[0].operation, "write")
            self.assertEqual(Path(response.results[0].evidence[0].file_path or ""), created_file)

    def test_supervisor_respects_explicit_workspace_subpath_for_file_flow(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                "Create a file at app/generated/main.py with a short greeting"
            )

            explicit_file = Path(temp_dir) / "app" / "generated" / "main.py"
            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertTrue(explicit_file.exists())
            self.assertFalse((Path(temp_dir) / "created_items" / "app" / "generated" / "main.py").exists())
            self.assertEqual(Path(response.results[0].evidence[0].file_path or ""), explicit_file)

    def test_supervisor_executes_and_reviews_runtime_flow(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                f'Run "{sys.executable}" -c "print(\'runtime loop ok\')"'
        )

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertEqual(response.escalation_level, ExecutionEscalation.SINGLE_ACTION)
        self.assertEqual(response.outcome.total_subtasks, 2)
        self.assertEqual(len(response.results), 2)
        self.assertEqual(response.results[1].status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(response.results[1].tool_name, "runtime_tool")
        self.assertEqual(response.results[1].evidence[0].payload["exit_code"], 0)
        self.assertIn("runtime loop ok", response.results[1].evidence[0].payload["stdout_preview"])

    def test_supervisor_builds_hello_script_and_captures_run_evidence(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "codex_cli_enabled", False),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal("Build a tiny Python script that prints hello")

            created_file = Path(temp_dir) / "created_items" / "hello.py"
            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertTrue(created_file.exists())
            self.assertIn('print("hello")', created_file.read_text(encoding="utf-8"))
            file_result = next(result for result in response.results if result.tool_name == "file_tool")
            runtime_result = next(result for result in response.results if result.tool_name == "runtime_tool")
            review_result = next(result for result in response.results if result.tool_name == "coding_artifact")
            self.assertEqual(Path(file_result.evidence[0].file_path or ""), created_file)
            self.assertEqual(runtime_result.evidence[0].payload["exit_code"], 0)
            self.assertIn("hello", runtime_result.evidence[0].payload["stdout_preview"])
            self.assertTrue(review_result.evidence[0].verification_notes)
            self.assertNotIn("planner_mode", response.response.lower())
            self.assertNotIn("subtask", response.response.lower())
            self.assertNotIn("backend", response.response.lower())

    def test_supervisor_builds_quadratic_script_and_verifies_execution(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
            patch.object(settings, "codex_cli_enabled", False),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal("Create a Python script that solves a quadratic")

            created_file = Path(temp_dir) / "created_items" / "quadratic.py"
            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertTrue(created_file.exists())
            runtime_result = next(result for result in response.results if result.tool_name == "runtime_tool")
            self.assertEqual(runtime_result.evidence[0].payload["exit_code"], 0)
            self.assertIn("Roots:", runtime_result.evidence[0].payload["stdout_preview"])

    def test_supervisor_creates_simple_readme_artifact(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal("Make a simple README file")

            created_file = Path(temp_dir) / "created_items" / "README.md"
            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertTrue(created_file.exists())
            self.assertIn("simple README", created_file.read_text(encoding="utf-8"))

    def test_failed_runtime_result_does_not_complete(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                f'Run "{sys.executable}" -c "import sys; sys.exit(2)"'
            )

        self.assertEqual(response.status, TaskStatus.BLOCKED)
        runtime_result = next(result for result in response.results if result.tool_name == "runtime_tool")
        self.assertEqual(runtime_result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(runtime_result.evidence[0].payload["exit_code"], 2)

    def test_supervisor_tracks_objective_completion_state_for_owned_goal(self) -> None:
        with patch.object(settings, "openrouter_api_key", None):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal(
                "Build the reminder system and keep going until it works or you're blocked."
            )

        self.assertEqual(response.escalation_level, ExecutionEscalation.OBJECTIVE_COMPLETION)
        stored_task = task_state_store.get_task(response.task_id)
        self.assertIsNotNone(stored_task)
        self.assertIsNotNone(stored_task.objective_state)
        self.assertEqual(stored_task.objective_state.escalation_level, ExecutionEscalation.OBJECTIVE_COMPLETION)
        self.assertIn(stored_task.objective_state.stage, {ObjectiveStage.ADAPTING, ObjectiveStage.BLOCKED, ObjectiveStage.COMPLETED})
        self.assertGreaterEqual(len(stored_task.objective_state.delegated_agents), 1)

    def test_supervisor_creates_python_file_with_honest_name(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            response = supervisor.handle_user_goal("Create a python file")

            created_file = Path(temp_dir) / "created_items" / "script.py"

            self.assertEqual(response.status, TaskStatus.COMPLETED)
            self.assertTrue(created_file.exists())
            self.assertEqual(Path(response.results[0].evidence[0].file_path or ""), created_file)
            self.assertIn("script.py", response.response)

    def test_supervisor_carries_forward_recent_file_context_for_follow_up_create(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            supervisor = Supervisor()
            first = supervisor.handle_user_goal("Create a python file called hello")
            second = supervisor.handle_user_goal("Now create one called goodbye")

            hello_file = Path(temp_dir) / "created_items" / "hello.py"
            goodbye_file = Path(temp_dir) / "created_items" / "goodbye.py"

            self.assertEqual(first.status, TaskStatus.COMPLETED)
            self.assertEqual(second.status, TaskStatus.COMPLETED)
            self.assertTrue(hello_file.exists())
            self.assertTrue(goodbye_file.exists())
            self.assertIn("goodbye.py", second.response)


if __name__ == "__main__":
    unittest.main()
