"""Tool capability manifest, cost policy, and context-bleed regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.assistant import AssistantLayer
from core.models import ExecutionEscalation, RequestMode
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from memory.memory_store import MemoryStore
from tools.capability_manifest import build_capability_catalog
from tools.tool_policy import build_tool_cost_policy


class _NoLlmClient:
    def is_configured(self) -> bool:
        return False


class ToolAwarenessPolicyTests(unittest.TestCase):
    def _supervisor(self, temp_dir: str) -> Supervisor:
        llm = _NoLlmClient()
        operator_context = OperatorContextService(
            openrouter_client=llm,
            task_store=TaskStateStore(),
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
        )
        router = Router(openrouter_client=llm)
        planner = Planner(
            openrouter_client=llm,
            agent_registry=router.agent_registry,
        )
        assistant = AssistantLayer(
            openrouter_client=llm,
            operator_context_service=operator_context,
        )
        return Supervisor(
            assistant_layer=assistant,
            planner=planner,
            router=router,
            operator_context_service=operator_context,
        )

    def test_manifest_contains_current_and_future_tool_capabilities(self) -> None:
        catalog = build_capability_catalog()

        playwright = catalog.snapshot_for("playwright_browser")
        browser_execution = catalog.snapshot_for("browser_execution")
        browser_use = catalog.snapshot_for("browser_use_browser")
        codex = catalog.snapshot_for("codex_cli")
        manus = catalog.snapshot_for("manus_agent")
        reminders = catalog.snapshot_for("reminder_scheduler")
        memory = catalog.snapshot_for("memory_context")
        files = catalog.snapshot_for("file_tool")

        self.assertIsNotNone(playwright)
        self.assertEqual(playwright.cost_tier, "cheap")
        self.assertEqual(playwright.category, "browser")
        self.assertIn(playwright.status, {"live", "configured_but_disabled", "unavailable"})

        self.assertIsNotNone(browser_execution)
        self.assertIn(browser_execution.status, {"live", "configured_but_disabled", "unavailable"})
        self.assertEqual(browser_execution.owner_agent, "supervisor")
        self.assertIn("Direct simple browser", browser_execution.summary)

        self.assertIsNotNone(browser_use)
        self.assertEqual(browser_use.category, "browser")
        self.assertIn(browser_use.status, {"planned", "configured_but_disabled"})
        self.assertTrue(browser_use.requires_credentials)

        self.assertIsNotNone(codex)
        self.assertEqual(codex.category, "coding")
        self.assertIn("build", " ".join(codex.strengths).lower())

        self.assertIsNotNone(manus)
        self.assertEqual(manus.cost_tier, "premium")
        self.assertIn(manus.status, {"planned", "configured_but_disabled"})

        self.assertEqual(reminders.cost_tier, "local")
        self.assertEqual(memory.cost_tier, "local")
        self.assertEqual(files.cost_tier, "local")

    def test_browser_agent_catalog_wording_stays_future_facing_for_complex_workflows(self) -> None:
        agent = __import__("agents.catalog", fromlist=["build_agent_catalog"]).build_agent_catalog().by_name("browser_agent")

        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent.status, "scaffolded")
        self.assertNotIn("browser_execution", agent.owns_capabilities)
        self.assertIn("complex-browser", agent.summary)

    def test_context_assembly_exposes_tool_cost_policy_to_planning(self) -> None:
        planner = Planner(openrouter_client=_NoLlmClient())
        block = planner.context_assembler.build(
            "planning_agent",
            goal="open cnn.com and save the headlines to headlines.txt",
        ).to_prompt_block()

        self.assertIn("tool_cost_policy:", block)
        self.assertIn("prefer free/local/cheap tools", block)
        self.assertIn("capabilities_live:", block)
        self.assertIn("playwright_browser", block)

    def test_cheap_first_policy_prefers_local_browser_and_blocks_disabled_premium(self) -> None:
        policy = build_tool_cost_policy()

        simple_browser = policy.assess("open cnn.com")
        complex_browser = policy.assess("book a flight on a website")
        explicit_manus = policy.assess("use Manus to do this")

        self.assertEqual(simple_browser.preferred_capability_ids, ("playwright_browser",))
        self.assertNotIn("manus_agent", complex_browser.preferred_capability_ids)
        self.assertIn("playwright_browser", complex_browser.preferred_capability_ids)
        self.assertTrue(explicit_manus.blocked)
        self.assertIn("Manus", explicit_manus.blocker or "")
        self.assertIn("MANUS_API_KEY", explicit_manus.blocker or "")

    def test_mixed_browser_file_request_preserves_capability_sequence(self) -> None:
        router = Router(openrouter_client=_NoLlmClient())
        planner = Planner(
            openrouter_client=_NoLlmClient(),
            agent_registry=router.agent_registry,
        )

        subtasks, planner_mode = planner.create_plan(
            "open cnn.com and save the headlines to headlines.txt",
            escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
        )

        tools = [
            subtask.tool_invocation.tool_name
            for subtask in subtasks
            if subtask.tool_invocation is not None
        ]
        self.assertEqual(planner_mode, "deterministic")
        self.assertIn("browser_tool", tools)
        self.assertIn("file_tool", tools)
        self.assertLess(tools.index("browser_tool"), tools.index("file_tool"))

    def test_context_bleed_after_codex_like_task_stays_assistant_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            coding_response = supervisor.handle_user_goal("Refactor a failing auth module and add tests.")
            thanks_response = supervisor.handle_user_goal("thanks")

            self.assertEqual(coding_response.request_mode, RequestMode.EXECUTE)
            self.assertEqual(thanks_response.request_mode, RequestMode.ANSWER)
            self.assertEqual(thanks_response.planner_mode, "conversation_fast_path")

    def test_capability_question_after_tool_task_does_not_execute_browsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            file_response = supervisor.handle_user_goal("write a file called note.txt saying hello")
            capability_response = supervisor.handle_user_goal("can you browse websites?")

            self.assertEqual(file_response.planner_mode, "fast_action")
            self.assertEqual(capability_response.request_mode, RequestMode.ANSWER)
            self.assertFalse(capability_response.results)
            self.assertIn("browser", capability_response.response.lower())

    def test_ceo_capability_answers_are_natural_and_readiness_aware(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            prompts = {
                "what can you do?": ("CEO-style operator", "Live right now"),
                "can you use the browser?": ("browser", "Browser Use"),
                "can you use Codex?": ("Codex", "coding"),
                "can you send emails?": ("Email", "Communications Agent"),
                "can you see my calendar/tasks?": ("Scheduling Agent", "Calendar"),
                "what agents do you have?": ("Research Agent", "Reviewer"),
                "what is currently connected?": ("Live now", "Needs setup"),
                "what should we build next?": ("next", "Agent"),
            }

            for prompt, expected in prompts.items():
                response = supervisor.handle_user_goal(prompt)
                self.assertEqual(response.request_mode, RequestMode.ANSWER, prompt)
                self.assertFalse(response.results, prompt)
                for phrase in expected:
                    self.assertIn(phrase.lower(), response.response.lower(), prompt)
                lowered = response.response.lower()
                for marker in ("planner_mode", "request_mode", "tool id", "cost=", "risk=", "langgraph"):
                    self.assertNotIn(marker, lowered, prompt)

    def test_ceo_capability_context_uses_plain_status_without_cost_risk_metadata(self) -> None:
        catalog = build_capability_catalog()
        block = catalog.ceo_context().prompt_block()

        self.assertIn("ceo_capability_context:", block)
        self.assertIn("ceo_agent_context:", block)
        self.assertIn("Browser Use", block)
        self.assertNotIn("cost=", block)
        self.assertNotIn("risk=", block)

    def test_explicit_manus_request_returns_unavailable_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            response = supervisor.handle_user_goal("use Manus to research this")

            self.assertEqual(response.request_mode, RequestMode.ANSWER)
            self.assertFalse(response.results)
            self.assertIn("Manus", response.response)
            self.assertIn("not configured", response.response)


if __name__ == "__main__":
    unittest.main()
