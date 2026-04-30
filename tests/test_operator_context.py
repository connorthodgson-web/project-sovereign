"""Coverage for operator continuity, memory capture, and runtime self-knowledge."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.conversation import ConversationalHandler
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    RequestMode,
    Task,
    TaskStatus,
)
from core.operator_context import OperatorContextService
from core.state import TaskStateStore
from integrations.openrouter_client import OpenRouterClient
from memory.memory_store import MemoryStore


class FakeOpenRouterClient:
    def __init__(self, *, configured: bool = False) -> None:
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
        del kwargs
        raise AssertionError("LLM prompt should not be used in these deterministic tests.")


class OperatorContextTests(unittest.TestCase):
    def test_captures_user_preference_and_avoids_secret_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Please keep answers concise.")
            service.record_user_message("My API key is sk-1234567890secret")

            user_facts = store.list_facts("user")
            recent_actions = store.snapshot().recent_actions

        self.assertTrue(any("concise" in fact.value.lower() for fact in user_facts))
        self.assertFalse(any("sk-1234567890secret" in fact.value for fact in user_facts))
        self.assertTrue(any("secret-like" in action.summary.lower() for action in recent_actions))

    def test_runtime_snapshot_includes_live_and_unfinished_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            task_store = TaskStateStore()
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=task_store,
            )
            store.upsert_fact(
                layer="project",
                category="priority",
                key="project-priority",
                value="Sovereign should feel like one CEO-style operator.",
                confidence=0.9,
                source="test",
            )

            snapshot = service.build_runtime_snapshot()

        self.assertIn("LLM provider not configured", snapshot.model_label)
        self.assertTrue(any("file_tool" in item for item in snapshot.live_tools))
        self.assertTrue(
            any("browser_execution" in item for item in snapshot.live_tools + snapshot.scaffolded_tools + snapshot.configured_tools)
        )
        self.assertTrue(any("CEO-style operator" in item for item in snapshot.project_memory))

    def test_runtime_snapshot_exposes_agent_roles_and_disabled_capabilities(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "browser_use_api_key", "configured"),
            patch.object(settings, "browser_enabled", False),
        ):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            snapshot = service.build_runtime_snapshot()

        self.assertTrue(any("browser_execution" in item for item in snapshot.configured_tools))
        self.assertTrue(any("supervisor" in item for item in snapshot.agent_roles))

    def test_explicit_memory_updates_overwrite_prior_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Remember that I parked on level 3 near the blue sign.")
            service.record_user_message("Update that: I actually parked on level 4 near the red sign.")

            user_facts = store.list_facts("user")

        parking_fact = next(fact for fact in user_facts if fact.key == "user:parking_location")
        self.assertIn("level 4", parking_fact.value.lower())
        self.assertIn("red sign", parking_fact.value.lower())

    def test_runtime_snapshot_prunes_transient_task_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            store.upsert_fact(
                layer="project",
                category="current_goal",
                key="task-goal:legacy-task",
                value="Legacy reminder task.",
                confidence=0.8,
                source="test",
            )
            store.upsert_fact(
                layer="operational",
                category="active_task",
                key="task:legacy-task",
                value="write a 24 solver",
                confidence=0.9,
                source="test",
            )
            store.upsert_fact(
                layer="operational",
                category="recent_result",
                key="task:legacy-task:result",
                value="write a 24 solver -> completed",
                confidence=0.8,
                source="test",
            )
            for fact in store._snapshot.project_facts + store._snapshot.operational_facts:
                fact.updated_at = "2024-01-01T00:00:00+00:00"

            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            snapshot = service.build_runtime_snapshot(context_profile="memory")

        self.assertFalse(any("24 solver" in item.lower() for item in snapshot.user_memory))
        self.assertFalse(any("24 solver" in item.lower() for item in snapshot.project_memory))
        self.assertFalse(any("24 solver" in item.lower() for item in snapshot.operational_memory))
        self.assertFalse(any(fact.category == "active_task" for fact in store.list_facts("operational")))


class ConversationalContinuityTests(unittest.TestCase):
    def test_answers_memory_and_runtime_questions_from_operator_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            task_store = TaskStateStore()
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=task_store,
            )
            service.record_user_message("Please keep answers concise.")
            service.record_user_message("Project Sovereign should feel like one CEO-style operator.")
            service.remember_open_loop("check the next Sovereign milestone")

            task = Task(
                goal="Create hello.py",
                title="Create hello.py",
                description="Create hello.py",
                status=TaskStatus.RUNNING,
                request_mode=RequestMode.ACT,
                results=[
                    AgentResult(
                        subtask_id="1",
                        agent="coding_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary="Created workspace file at hello.py.",
                    )
                ],
            )
            task_store.add_task(task)
            service.task_started(task)

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=task_store,
                workspace_root=temp_dir,
                operator_context_service=service,
            )

            memory_response = handler.handle(
                "What do you know about me?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="memory", should_use_tools=False),
            )
            work_response = handler.handle(
                "What are you working on?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="runtime", should_use_tools=False),
            )
            reminder_response = handler.handle(
                "Remind me later to check this milestone.",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="follow up", should_use_tools=False),
            )

        self.assertIn("concise", memory_response.response.lower())
        self.assertNotIn("create hello.py", memory_response.response.lower())
        self.assertIn("create hello.py", work_response.response.lower())
        self.assertIn("scheduler isn't live", reminder_response.response.lower())

    def test_memory_prompt_uses_memory_context_not_recent_task_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            task_store = TaskStateStore()
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=task_store,
            )
            service.record_user_message("Please keep answers concise.")
            task = Task(
                goal="Refactor the routing layer",
                title="Refactor the routing layer",
                description="Refactor the routing layer",
                status=TaskStatus.RUNNING,
                request_mode=RequestMode.EXECUTE,
            )
            task_store.add_task(task)
            service.task_started(task)

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=task_store,
                workspace_root=temp_dir,
                operator_context_service=service,
            )

            response = handler.handle(
                "What do you remember about me?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="memory", should_use_tools=False),
            )

        self.assertIn("concise", response.response.lower())
        self.assertNotIn("refactor the routing layer", response.response.lower())

    def test_broad_user_memory_recall_includes_practical_facts_not_task_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            store = MemoryStore(Path(temp_dir) / "memory.json")
            task_store = TaskStateStore()
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=task_store,
            )
            service.record_user_message("Remember that I prefer concise answers.")
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")
            task = Task(
                goal="Write a 24 solver",
                title="Write a 24 solver",
                description="Write a 24 solver",
                status=TaskStatus.RUNNING,
                request_mode=RequestMode.EXECUTE,
            )
            task_store.add_task(task)
            service.task_started(task)

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=task_store,
                workspace_root=temp_dir,
                operator_context_service=service,
            )

            response = handler.handle(
                "What do you remember about me?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="memory", should_use_tools=False),
            )

        self.assertIn("concise", response.response.lower())
        self.assertIn("parked", response.response.lower())
        self.assertNotIn("24 solver", response.response.lower())

    def test_answers_model_question_honestly_when_llm_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=OperatorContextService(
                    openrouter_client=FakeOpenRouterClient(configured=False),
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
            )

            response = handler.handle(
                "What model are you using?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="model", should_use_tools=False),
            )

        self.assertIn("not configured", response.response.lower())

    def test_answers_capability_owner_and_activation_questions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=OperatorContextService(
                    openrouter_client=FakeOpenRouterClient(configured=False),
                    memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                    task_store=TaskStateStore(),
                ),
            )

            owner_response = handler.handle(
                "Which agent would handle reminders?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="owner", should_use_tools=False),
            )
            activation_response = handler.handle(
                "What would be needed to turn on browser automation?",
                AssistantDecision(mode=RequestMode.ANSWER, reasoning="activation", should_use_tools=False),
            )

        self.assertIn("Scheduling Agent", owner_response.response)
        self.assertIn("reminders", owner_response.response.lower())
        self.assertNotIn("reminder_scheduler", owner_response.response)
        self.assertIn("Browser", activation_response.response)
        self.assertNotIn("browser_execution", activation_response.response)
        self.assertNotIn("adapter", activation_response.response.lower())


if __name__ == "__main__":
    unittest.main()
