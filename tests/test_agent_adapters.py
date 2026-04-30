"""Coverage for the shared agent adapter and registry layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.assistant import AssistantLayer
from core.models import AgentExecutionStatus, RequestMode, SubTask, Task, TaskStatus
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.openrouter_client import OpenRouterClient
from memory.memory_store import MemoryStore


class AgentAdapterTests(unittest.TestCase):
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

    def test_registry_lists_capability_candidates(self) -> None:
        router = Router(openrouter_client=None)

        browser_candidates = router.agent_registry.candidates_for_capability("browser")
        reminder_candidates = router.agent_registry.candidates_for_capability("reminders")

        self.assertTrue(any(adapter.agent_id == "browser_agent" for adapter in browser_candidates))
        self.assertTrue(any(adapter.agent_id == "scheduling_agent" for adapter in reminder_candidates))

    def test_registry_exposes_disabled_managed_agent_stubs(self) -> None:
        with (
            patch.object(settings, "codex_cli_enabled", False),
            patch.object(settings, "codex_cli_workspace_root", None),
        ):
            router = Router(openrouter_client=None)

        descriptors = router.agent_registry.list_descriptors(include_disabled=True)
        by_id = {item["agent_id"]: item for item in descriptors}

        self.assertFalse(by_id["openai_agents_agent"]["enabled"])
        self.assertFalse(by_id["manus_agent"]["enabled"])
        self.assertFalse(by_id["codex_cli_agent"]["enabled"])

    def test_registry_exposes_planner_and_verifier_agents(self) -> None:
        router = Router(openrouter_client=None)

        planner_adapter = router.agent_registry.get("planner_agent")
        verifier_adapter = router.agent_registry.get("verifier_agent")

        self.assertIsNotNone(planner_adapter)
        self.assertIsNotNone(verifier_adapter)
        assert planner_adapter is not None
        assert verifier_adapter is not None
        self.assertTrue(planner_adapter.supports_capability("planning"))
        self.assertTrue(verifier_adapter.supports_capability("final_verification"))

    def test_disabled_managed_agent_reports_missing_config_honestly(self) -> None:
        router = Router(openrouter_client=None)
        adapter = router.agent_registry.get("openai_agents_agent")
        self.assertIsNotNone(adapter)
        assert adapter is not None

        result = adapter.run(
            task=Task(goal="Use the managed agent", title="Use the managed agent", description="Use the managed agent"),
            subtask=SubTask(
                title="Run managed agent",
                description="Run managed agent",
                objective="Use the OpenAI Agents SDK stub",
                assigned_agent="openai_agents_agent",
            ),
        )

        self.assertEqual(result.agent, "openai_agents_agent")
        self.assertEqual(result.status.value, "blocked")
        self.assertIn("missing config", result.summary.lower())
        self.assertIn("OPENAI_AGENTS_API_KEY", result.blockers[0])

    def test_router_resolves_legacy_reminder_alias_to_new_adapter(self) -> None:
        router = Router(openrouter_client=None)

        adapter = router.agent_registry.get("reminder_scheduler_agent")

        self.assertIsNotNone(adapter)
        assert adapter is not None
        self.assertEqual(adapter.agent_id, "scheduling_agent")

    def test_browser_request_followed_by_reminder_chooses_fresh_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = Supervisor(
                assistant_layer=self._assistant_layer(),
            )

            browser_decision = supervisor.assistant_layer.decide("open https://example.com")
            reminder_decision = supervisor.assistant_layer.decide("remind me in 5 minutes to stretch")

            browser_lane = supervisor._select_lane("open https://example.com", browser_decision)
            reminder_lane = supervisor._select_lane(
                "remind me in 5 minutes to stretch",
                reminder_decision,
            )

        self.assertEqual(browser_lane.agent_id, "browser_agent")
        self.assertEqual(reminder_lane.agent_id, "scheduling_agent")
        self.assertNotEqual(browser_lane.agent_id, reminder_lane.agent_id)

    def test_greeting_routes_to_assistant_agent(self) -> None:
        supervisor = Supervisor(assistant_layer=self._assistant_layer())

        decision = supervisor.assistant_layer.decide("hi")
        lane = supervisor._select_lane("hi", decision)

        self.assertEqual(lane.agent_id, "assistant_agent")

    def test_name_update_routes_to_memory_agent(self) -> None:
        supervisor = Supervisor(assistant_layer=self._assistant_layer())

        decision = supervisor.assistant_layer.decide("my name is Connor")
        lane = supervisor._select_lane("my name is Connor", decision)

        self.assertEqual(lane.agent_id, "memory_agent")

    def test_memory_follow_up_routes_to_memory_agent(self) -> None:
        supervisor = Supervisor(assistant_layer=self._assistant_layer())

        decision = supervisor.assistant_layer.decide("what do you remember about me?")
        lane = supervisor._select_lane("what do you remember about me?", decision)

        self.assertEqual(lane.agent_id, "memory_agent")

    def test_reminder_request_routes_to_reminder_agent(self) -> None:
        supervisor = Supervisor(assistant_layer=self._assistant_layer())

        decision = supervisor.assistant_layer.decide("remind me in 5 minutes to stretch")
        lane = supervisor._select_lane("remind me in 5 minutes to stretch", decision)

        self.assertEqual(lane.agent_id, "scheduling_agent")

    def test_browser_url_request_routes_to_browser_agent(self) -> None:
        supervisor = Supervisor(assistant_layer=self._assistant_layer())

        decision = supervisor.assistant_layer.decide("open https://example.com")
        lane = supervisor._select_lane("open https://example.com", decision)

        self.assertEqual(lane.agent_id, "browser_agent")

    def test_complex_task_routes_to_planner_agent_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            supervisor = Supervisor(assistant_layer=self._assistant_layer(memory_path=Path(temp_dir) / "memory.json"))

            decision = supervisor.assistant_layer.decide(
                "Research browser automation options and summarize the tradeoffs."
            )
            lane = supervisor._select_lane(
                "Research browser automation options and summarize the tradeoffs.",
                decision,
            )

        self.assertEqual(lane.agent_id, "planner_agent")

    def test_planner_uses_registry_capabilities_for_candidate_selection(self) -> None:
        router = Router(openrouter_client=None)
        planner = Planner(
            openrouter_client=OpenRouterClient(api_key=None),
            agent_registry=router.agent_registry,
        )

        subtasks, planner_mode = planner.create_plan("Open https://example.com in the browser and inspect it.")

        self.assertEqual(planner_mode, "deterministic")
        execute_subtask = subtasks[1]
        self.assertEqual(execute_subtask.assigned_agent, "browser_agent")
        self.assertTrue(any("Planner candidates: browser_agent" in note for note in execute_subtask.notes))

    def test_reviewer_and_verifier_handle_blocked_browser_evidence(self) -> None:
        router = Router(openrouter_client=None)
        reviewer = router.agent_registry.get("reviewer_agent")
        verifier = router.agent_registry.get("verifier_agent")
        self.assertIsNotNone(reviewer)
        self.assertIsNotNone(verifier)
        assert reviewer is not None
        assert verifier is not None

        execution_subtask = SubTask(
            id="exec-1",
            title="Open page",
            description="Open the browser page",
            objective="Open the browser page",
            assigned_agent="browser_agent",
            status=TaskStatus.BLOCKED,
        )
        task = Task(
            goal="Open the requested page in the browser.",
            title="Browser task",
            description="Browser task",
            status=TaskStatus.RUNNING,
            subtasks=[execution_subtask],
            results=[
                router.agent_registry.get("browser_agent").run(  # type: ignore[union-attr]
                    Task(
                        goal="Open the requested page in the browser.",
                        title="Browser task",
                        description="Browser task",
                    ),
                    execution_subtask,
                )
            ],
        )
        review_subtask = SubTask(
            title="Review browser blocker",
            description="Review the blocked browser execution",
            objective="Review the blocked browser execution",
            assigned_agent="reviewer_agent",
        )
        review_result = reviewer.run(task, review_subtask)
        task.results.append(review_result)

        verify_subtask = SubTask(
            title="Verify browser blocker",
            description="Verify final status",
            objective="Verify final status",
            assigned_agent="verifier_agent",
        )
        verification_result = verifier.run(task, verify_subtask)

        self.assertEqual(review_result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(review_result.evidence[0].verification_notes)
        self.assertEqual(verification_result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(verification_result.blockers)

    def test_who_are_you_stays_on_assistant_lane(self) -> None:
        supervisor = Supervisor(assistant_layer=self._assistant_layer())

        decision = supervisor.assistant_layer.decide("who are you?")
        lane = supervisor._select_lane("who are you?", decision)

        self.assertEqual(decision.mode, RequestMode.ANSWER)
        self.assertEqual(lane.agent_id, "assistant_agent")


if __name__ == "__main__":
    unittest.main()
