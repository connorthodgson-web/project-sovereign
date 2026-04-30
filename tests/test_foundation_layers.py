"""Coverage for the LLM-first foundation, capability metadata, and retrieval boundaries."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.communications_agent import CommunicationsAgent
from app.config import settings
from agents.catalog import build_agent_catalog
from core.context_assembly import ContextAssembler
from core.assistant import AssistantLayer
from core.models import AgentExecutionStatus, AssistantDecision, RequestMode, SubTask, Task, TaskStatus, ToolInvocation
from core.operator_context import OperatorContextService
from core.planner import Planner
from integrations.browser.runtime import BrowserRuntimeSupport
from integrations.readiness import build_integration_readiness
from integrations.openrouter_client import OpenRouterClient
from memory.memory_store import MemoryStore
from memory.retrieval import MemoryRetriever
from tools.capability_manifest import build_capability_catalog
from tools.registry import build_default_tool_registry


class FakeOpenRouterClient:
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
        **kwargs,
    ) -> str:
        del prompt, system_prompt, label, kwargs
        return self.response


class CapabilityCatalogTests(unittest.TestCase):
    def test_capability_catalog_separates_live_and_scaffolded_states(self) -> None:
        registry = build_default_tool_registry()

        with patch.object(settings, "openrouter_api_key", None):
            catalog = build_capability_catalog(tool_registry=registry)
            live, non_live = catalog.user_visible_lines()

        self.assertTrue(any(item.startswith("file_tool (live") for item in live))
        self.assertTrue(any(item.startswith("runtime_tool (live") for item in live))
        self.assertTrue(any("browser_execution" in item for item in live + non_live))
        self.assertTrue(any("semantic_retrieval" in item for item in non_live))

    def test_capability_catalog_exposes_owner_and_activation_requirements(self) -> None:
        catalog = build_capability_catalog(tool_registry=build_default_tool_registry())

        browser_snapshot = catalog.snapshot_for("browser_execution")

        self.assertIsNotNone(browser_snapshot)
        self.assertEqual(browser_snapshot.owner_agent, "supervisor")
        self.assertIn("BROWSER_ENABLED", browser_snapshot.config_requirements)


class AgentCatalogTests(unittest.TestCase):
    def test_agent_catalog_maps_capability_owners(self) -> None:
        catalog = build_agent_catalog()

        owner = catalog.capability_owner("email_delivery")

        self.assertIsNotNone(owner)
        self.assertEqual(owner.name, "communications_agent")


class IntegrationReadinessTests(unittest.TestCase):
    def test_browser_execution_readiness_distinguishes_live_disabled_and_missing_runtime(self) -> None:
        with (
            patch.object(settings, "browser_enabled", True),
            patch("integrations.readiness.detect_browser_runtime_support", return_value=BrowserRuntimeSupport(
                playwright_available=True,
                browser_use_sdk_available=False,
                chromium_binary_available=True,
            )),
        ):
            live = build_integration_readiness()["integration:browser"]

        with (
            patch.object(settings, "browser_enabled", False),
            patch("integrations.readiness.detect_browser_runtime_support", return_value=BrowserRuntimeSupport(
                playwright_available=True,
                browser_use_sdk_available=False,
                chromium_binary_available=True,
            )),
        ):
            disabled = build_integration_readiness()["integration:browser"]

        with (
            patch.object(settings, "browser_enabled", True),
            patch("integrations.readiness.detect_browser_runtime_support", return_value=BrowserRuntimeSupport(
                playwright_available=False,
                browser_use_sdk_available=False,
                chromium_binary_available=False,
            )),
        ):
            missing_runtime = build_integration_readiness()["integration:browser"]

        self.assertEqual(live.status, "live")
        self.assertEqual(disabled.status, "configured_but_disabled")
        self.assertEqual(missing_runtime.status, "unavailable")
        self.assertIn("PLAYWRIGHT_PACKAGE", missing_runtime.missing_fields)

    def test_configured_but_disabled_state_is_reported_honestly(self) -> None:
        with (
            patch.object(settings, "browser_use_api_key", "configured"),
            patch.object(settings, "browser_enabled", False),
        ):
            readiness = build_integration_readiness()

        browser = readiness["integration:browser"]
        self.assertEqual(browser.status, "configured_but_disabled")
        self.assertTrue(browser.configured)
        self.assertFalse(browser.enabled)


class ContextAssemblyTests(unittest.TestCase):
    def test_context_assembly_injects_instruction_and_capability_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=OpenRouterClient(api_key=None),
                memory_store_instance=store,
            )
            bundle = ContextAssembler(operator_context_service=service).build(
                "operator",
                user_message="What tools do you have?",
            )

            prompt_block = bundle.to_prompt_block()

        self.assertIn("Project Sovereign", prompt_block)
        self.assertIn("Tool Selection Policy", prompt_block)
        self.assertIn("capabilities_live:", prompt_block)
        self.assertIn("capabilities_not_live:", prompt_block)
        self.assertIn("browser_execution", prompt_block)

    def test_context_assembly_uses_memory_profile_for_specific_recall_questions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=OpenRouterClient(api_key=None),
                memory_store_instance=store,
            )
            bundle = ContextAssembler(operator_context_service=service).build(
                "conversation",
                user_message="What is my favorite color?",
            )

        self.assertEqual(bundle.context_profile, "memory")


class PlannerFoundationTests(unittest.TestCase):
    class FakeInvocationBuilder:
        def can_build(self, goal: str) -> bool:
            return "hello.txt" in goal

        def build(self, goal: str):
            del goal
            from core.invocation_builders import BuiltInvocation

            return BuiltInvocation(
                invocation=ToolInvocation(
                    tool_name="file_tool",
                    action="write",
                    parameters={"path": "hello.txt", "content": "fallback"},
                ),
                execution_title="Fallback execution",
                execution_description="Fallback execution description",
                execution_objective="Fallback execution objective",
                review_objective="Fallback review objective",
            )

    def test_planner_prefers_llm_plan_when_available(self) -> None:
        planner = Planner(
            openrouter_client=FakeOpenRouterClient(
                response=
                '{"subtasks":['
                '{"title":"Capture context","description":"Store the goal","objective":"Persist goal context","agent_hint":"memory_agent","tool_invocation":null},'
                '{"title":"Create file","description":"Create the requested file","objective":"Create hello.txt","agent_hint":"coding_agent","tool_invocation":{"tool_name":"file_tool","action":"write","parameters":{"path":"hello.txt","content":"from llm"}}},'
                '{"title":"Review result","description":"Verify the created file","objective":"Review hello.txt","agent_hint":"reviewer_agent","tool_invocation":null}'
                "]}"
            ),
            invocation_builders=[self.FakeInvocationBuilder()],
        )

        subtasks, mode = planner.create_plan("Create a file called hello.txt")

        self.assertEqual(mode, "openrouter")
        self.assertEqual(subtasks[1].title, "Create file")
        self.assertEqual(subtasks[1].tool_invocation.parameters["content"], "from llm")


class AssistantFoundationTests(unittest.TestCase):
    def test_assistant_defers_non_fast_path_question_to_llm_when_available(self) -> None:
        layer = AssistantLayer(
            openrouter_client=FakeOpenRouterClient(
                response='{"mode":"ACT","reasoning":"The model chose to act.","should_use_tools":true}'
            )
        )

        decision = layer.decide("Compare two approaches for this task")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertIn("model chose to act", decision.reasoning.lower())

    def test_assistant_routes_browser_open_request_to_action_path_without_llm(self) -> None:
        layer = AssistantLayer(
            openrouter_client=FakeOpenRouterClient(response="", configured=False)
        )

        decision = layer.decide_without_llm("open a browser page")

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertTrue(decision.should_use_tools)


class RetrievalFoundationTests(unittest.TestCase):
    def test_semantic_strategy_falls_back_cleanly_to_keyword_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            store.upsert_fact(
                layer="project",
                category="priority",
                key="priority",
                value="Sovereign should feel like one CEO-style operator.",
                confidence=0.9,
                source="test",
            )
            retriever = MemoryRetriever(store)

            result = retriever.retrieve("CEO operator", strategy="semantic")

        self.assertEqual(result.backend, "keyword")
        self.assertEqual(result.strategy, "semantic_fallback_to_keyword")
        self.assertTrue(any("CEO-style operator" in match.fact.value for match in result.matches))


class HonestyTests(unittest.TestCase):
    def test_communications_agent_reports_scaffolded_execution_as_blocked(self) -> None:
        with patch.object(settings, "gmail_enabled", False):
            agent = CommunicationsAgent()
            task = Task(goal="Email the weekly update", title="Email update", description="Email update")
            subtask = SubTask(
                title="Send email",
                description="Deliver the weekly update",
                objective="Send the weekly update email",
                assigned_agent="communications_agent",
                status=TaskStatus.ROUTED,
            )

            result = agent.run(task, subtask)

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("not live", result.summary.lower())
        self.assertTrue(any("scaffolded" in blocker.lower() for blocker in result.blockers))


if __name__ == "__main__":
    unittest.main()
