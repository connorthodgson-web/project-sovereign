"""Regression coverage for assistant-style memory recall behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.conversation import ConversationalHandler
from core.models import AssistantDecision, ExecutionEscalation, RequestMode
from core.operator_context import OperatorContextService
from core.state import TaskStateStore
from memory.memory_store import MemoryStore
from memory.retrieval import MemoryRetriever


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
    ) -> str:
        raise AssertionError("LLM prompt should not be used in these deterministic tests.")


def answer_decision() -> AssistantDecision:
    return AssistantDecision(
        mode=RequestMode.ANSWER,
        escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
        reasoning="deterministic test",
        should_use_tools=False,
    )


class MemoryRecallTests(unittest.TestCase):
    def test_retrieval_ranking_prefers_recent_high_priority_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            store.upsert_fact(
                layer="project",
                category="priority",
                key="memory-priority",
                value="Memory is the next priority before tool expansion in Sovereign.",
                confidence=0.95,
                source="test",
            )
            store.upsert_fact(
                layer="user",
                category="preference",
                key="brief-answers",
                value="Keep answers brief.",
                confidence=0.9,
                source="test",
            )
            store.upsert_fact(
                layer="operational",
                category="context",
                key="memory-database-note",
                value="There is a memory database somewhere in the stack.",
                confidence=0.5,
                source="test",
            )

            stale_fact = next(fact for fact in store._snapshot.operational_facts if fact.key == "memory-database-note")
            stale_fact.updated_at = "2024-01-01T00:00:00+00:00"

            matches = MemoryRetriever(store).retrieve(
                "Why are we prioritizing memory in Sovereign?",
                limit=3,
            ).matches

        self.assertGreaterEqual(len(matches), 2)
        self.assertEqual(matches[0].fact.category, "priority")
        self.assertIn("next priority", matches[0].fact.value.lower())
        self.assertNotIn("database somewhere", matches[0].fact.value.lower())

    def test_preference_application_keeps_conversational_answer_brief(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("I prefer brief answers.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What can you do?", answer_decision())

        self.assertIn("right now i can", response.response.lower())
        self.assertNotIn("i'm also tracking unfinished areas honestly", response.response.lower())
        self.assertLess(len(response.response), 170)

    def test_relevant_project_context_recall_shapes_next_step_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Memory is the next priority in Sovereign before tool expansion.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What should we work on next in Sovereign?", answer_decision())

        self.assertIn("memory", response.response.lower())

    def test_remembering_project_priority_saves_and_retrieves_naturally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that my project priority is memory hardening before calendar.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What's my current priority?", answer_decision())

        project_facts = store.list_facts("project", category="current_priority")
        self.assertEqual(len(project_facts), 1)
        self.assertIn("memory hardening", project_facts[0].value.lower())
        self.assertIn("memory hardening", response.response.lower())
        self.assertIn("calendar", response.response.lower())

    def test_duplicate_explicit_memories_are_updated_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Remember that I prefer concise answers.")
            service.record_user_message("Remember that I prefer concise answers.")

        preference_facts = [
            fact for fact in store.list_facts("user", category="preference") if "concise" in fact.value.lower()
        ]
        self.assertEqual(len(preference_facts), 1)

    def test_secret_like_memory_request_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Remember that my API key is ghp_1234567890abcdef1234567890abcdef123456.")

        self.assertEqual(store.list_facts("user"), [])
        self.assertEqual(store.list_facts("project"), [])
        self.assertEqual(store.list_turns(), [])
        self.assertTrue(any(action.kind == "memory_safety" for action in store.snapshot().recent_actions))

    def test_unrelated_memory_does_not_leak_into_greeting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("hi", answer_decision())

        self.assertEqual(response.response, "Hi. What can I help with?")
        self.assertNotIn("blue sign", response.response.lower())

    def test_memory_context_helps_later_execution_prompt_without_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that my project priority is memory hardening before integrations.")
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")

            compiled = service.compile_prompt_context(
                focus_text="Build the next Project Sovereign pass.",
                context_profile="task",
            )

        prompt_text = "\n".join(compiled.core_memory + compiled.retrieved_memory)
        self.assertIn("memory hardening", prompt_text.lower())
        self.assertNotIn("blue sign", "\n".join(compiled.retrieved_memory).lower())

    def test_open_loop_continuity_survives_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "memory.json"
            store = MemoryStore(memory_path)
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.remember_open_loop("finish the memory continuity pass")

            reloaded_store = MemoryStore(memory_path)
            reloaded_service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=reloaded_store,
                task_store=TaskStateStore(),
            )
            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=reloaded_service,
            )
            response = handler.handle("What were we focused on before?", answer_decision())

        self.assertIn("memory continuity pass", response.response.lower())

    def test_natural_response_composition_avoids_memory_jargon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Memory is the next priority before tool expansion.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("Why are you recommending memory first?", answer_decision())

        self.assertIn("i remember", response.response.lower())
        self.assertNotIn("retrieved memory", response.response.lower())
        self.assertNotIn("loaded prior project context", response.response.lower())

    def test_explicit_practical_memory_is_recalled_after_unrelated_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            handler.handle("What can you do?", answer_decision())
            handler.handle("What do you remember about Project Sovereign?", answer_decision())
            response = handler.handle("Where did I park?", answer_decision())

        self.assertIn("level 3", response.response.lower())
        self.assertIn("blue sign", response.response.lower())

    def test_updated_memory_supersedes_older_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")
            service.record_user_message("Update that: I actually parked on level 4 near the red sign.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("Where did I park?", answer_decision())

        self.assertIn("level 4", response.response.lower())
        self.assertIn("red sign", response.response.lower())
        self.assertNotIn("level 3", response.response.lower())

    def test_unknown_specific_memory_question_is_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=OperatorContextService(
                    openrouter_client=FakeOpenRouterClient(configured=False),
                    memory_store_instance=store,
                    task_store=TaskStateStore(),
                ),
            )

            response = handler.handle("What is my favorite color?", answer_decision())

        self.assertIn("don't have", response.response.lower())
        self.assertIn("favorite color", response.response.lower())

    def test_memory_specific_questions_do_not_fall_back_to_generic_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I prefer concise answers.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What preference did I tell you earlier?", answer_decision())

        self.assertIn("prefer", response.response.lower())
        self.assertNotIn("right now i can", response.response.lower())

    def test_broad_user_memory_answer_feels_like_recall_not_single_fact_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")
            service.record_user_message("I prefer concise, natural replies.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What do you remember about me?", answer_decision())

        self.assertIn("i remember", response.response.lower())
        self.assertIn("concise", response.response.lower())
        self.assertIn("blue sign", response.response.lower())

    def test_memory_follow_up_includes_all_relevant_durable_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "memory.json"
            store = MemoryStore(memory_path)
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("My name is Connor Hodgson.")
            service.record_user_message("Remember that I like concise answers.")
            service.record_user_message("Project Sovereign should feel like one main operator with a hidden team.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            service.record_user_message("What do you remember about me?")
            handler.handle("What do you remember about me?", answer_decision())
            service.record_user_message("Is that all you have in memory?")
            response = handler.handle("Is that all you have in memory?", answer_decision())

        self.assertIn("saved", response.response.lower())
        self.assertIn("connor hodgson", response.response.lower())
        self.assertIn("concise", response.response.lower())
        self.assertIn("one main operator", response.response.lower())

    def test_broad_memory_answer_survives_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "memory.json"
            store = MemoryStore(memory_path)
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("My name is Connor Hodgson.")
            service.record_user_message("Remember that I like concise answers.")

            reloaded_store = MemoryStore(memory_path)
            reloaded_service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=reloaded_store,
                task_store=TaskStateStore(),
            )
            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=reloaded_service,
            )
            response = handler.handle("What do you remember about me?", answer_decision())

        self.assertIn("connor hodgson", response.response.lower())
        self.assertIn("concise", response.response.lower())

    def test_current_priority_prefers_open_loops_over_stale_project_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Project Sovereign should feel like one real assistant/operator, not middleware.")
            service.remember_open_loop("finish the live assistant quality pass for Sovereign")
            service.remember_open_loop("improve capability, continuity, and blocked-state replies")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What's my current priority?", answer_decision())

        self.assertIn("current priority", response.response.lower())
        self.assertIn("assistant quality pass", response.response.lower())
        self.assertNotIn("not middleware", response.response.lower())

    def test_capability_answer_stays_concrete_even_with_brief_preference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(configured=False),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("I prefer brief answers.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(configured=False),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What can you do?", answer_decision())

        self.assertIn("right now i can", response.response.lower())
        self.assertTrue(
            any(phrase in response.response.lower() for phrase in ("files", "commands", "reminders"))
        )


if __name__ == "__main__":
    unittest.main()
