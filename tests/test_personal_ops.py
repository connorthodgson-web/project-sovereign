"""Personal Ops parent domain, lists/notes, and routine placeholder coverage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.personal_ops_agent import PersonalOpsAgent
from app.config import settings
from core.assistant import AssistantLayer
from core.fast_actions import FastActionHandler
from core.models import AgentExecutionStatus, RequestMode, SubTask, TaskStatus
from core.operator_context import OperatorContextService
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from memory.memory_store import MemoryStore
from memory.personal_ops_store import JsonPersonalOpsStore


class _NoLlmClient:
    def is_configured(self) -> bool:
        return False


class PersonalOpsTests(unittest.TestCase):
    def _context(self, temp_dir: str) -> OperatorContextService:
        return OperatorContextService(
            openrouter_client=_NoLlmClient(),
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
            task_store=TaskStateStore(),
        )

    def _supervisor(self, temp_dir: str) -> Supervisor:
        context = self._context(temp_dir)
        llm = _NoLlmClient()
        return Supervisor(
            assistant_layer=AssistantLayer(openrouter_client=llm, operator_context_service=context),
            operator_context_service=context,
            fast_action_handler=FastActionHandler(
                operator_context_service=context,
                openrouter_client=llm,
            ),
        )

    def test_create_add_read_summarize_remove_update_and_rename_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            agent = PersonalOpsAgent(
                personal_store=JsonPersonalOpsStore(Path(temp_dir) / "personal_ops.json"),
                operator_context_service=self._context(temp_dir),
            )

            created = agent.handle_message("make a list for my classes")
            added = agent.handle_message("add AP Calc, AP Gov, English, Spanish to my classes list")
            read = agent.handle_message("what classes did I tell you?")
            updated = agent.handle_message("update English to English Literature in my classes list")
            removed = agent.handle_message("remove Spanish from my classes list")
            renamed = agent.handle_message("rename my classes list to school classes")
            summarized = agent.handle_message("summarize my school classes list")

        self.assertEqual(created.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(added.status, AgentExecutionStatus.COMPLETED)
        self.assertIn("4 item", read.summary)
        self.assertEqual(updated.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(removed.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(renamed.status, AgentExecutionStatus.COMPLETED)
        self.assertIn("English Literature", summarized.summary)
        self.assertNotIn("Spanish", summarized.summary)

    def test_list_names_are_normalized_and_stored_outside_memory_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            response = supervisor.handle_user_goal("add AP Gov to my class list")
            store = JsonPersonalOpsStore(Path(temp_dir) / ".sovereign" / "personal_ops.json")
            record = store.get_list("classes")
            memory = supervisor.operator_context.memory_store.snapshot()

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.normalized_name, "classes")
        self.assertEqual([item.text for item in record.items], ["AP Gov"])
        self.assertEqual(memory.user_facts, [])
        self.assertEqual(memory.project_facts, [])

    def test_personal_ops_state_is_prompted_only_when_relevant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            response = supervisor.handle_user_goal("add AP Gov to my class list")
            unrelated = supervisor.operator_context.build_runtime_snapshot(
                focus_text="What is my favorite color?",
                context_profile="memory",
            ).compiled_context
            relevant = supervisor.operator_context.build_runtime_snapshot(
                focus_text="What's on my classes list?",
                context_profile="task",
            ).compiled_context

        self.assertEqual(response.status, TaskStatus.COMPLETED)
        self.assertEqual(unrelated.personal_ops_state, [])
        self.assertTrue(any("AP Gov" in item for item in relevant.personal_ops_state))

    def test_personal_ops_user_response_hides_internal_agent_and_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            response = supervisor.handle_user_goal("add AP Gov to my class list")

        self.assertEqual(response.response, "I added AP Gov to your classes list.")
        self.assertNotIn("personal_ops_agent", response.response)
        self.assertNotIn("personal_ops_lists", response.response)
        self.assertNotIn("tool", response.response.lower())

    def test_that_list_and_add_too_use_short_term_referent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            first = supervisor.handle_user_goal("add AP Calc to my classes list")
            second = supervisor.handle_user_goal("add Spanish too")
            third = supervisor.handle_user_goal("what's on that list?")
            fourth = supervisor.handle_user_goal("remove the last one")
            fifth = supervisor.handle_user_goal("what's on that list?")

        self.assertEqual(first.status, TaskStatus.COMPLETED)
        self.assertEqual(second.status, TaskStatus.COMPLETED)
        self.assertIn("AP Calc", third.response)
        self.assertIn("Spanish", third.response)
        self.assertEqual(fourth.status, TaskStatus.COMPLETED)
        self.assertIn("AP Calc", fifth.response)
        self.assertNotIn("Spanish", fifth.response)

    def test_routing_boundaries_keep_casual_browser_and_coding_out_of_personal_ops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            supervisor = self._supervisor(temp_dir)

            hi = supervisor.handle_user_goal("hi")
            list_decision = supervisor.assistant_layer.decide("add this to my project ideas list")
            reminder_decision = supervisor.assistant_layer.decide("remind me in 5 minutes to stretch")
            gmail_decision = supervisor.assistant_layer.decide("summarize unread emails")
            browser_decision = supervisor.assistant_layer.decide("open https://example.com")
            coding_decision = supervisor.assistant_layer.decide("Refactor the auth module and add tests.")

        self.assertEqual(hi.request_mode, RequestMode.ANSWER)
        self.assertEqual(list_decision.intent_label, "personal_ops")
        self.assertEqual(reminder_decision.intent_label, "reminder_action")
        self.assertEqual(gmail_decision.intent_label, "communications_email")
        self.assertEqual(browser_decision.intent_label, "browser_action")
        self.assertNotEqual(coding_decision.intent_label, "personal_ops")

    def test_router_assigns_personal_list_subtasks_to_parent_agent(self) -> None:
        router = Router(openrouter_client=_NoLlmClient())
        decision = router.assign_agent(
            SubTask(
                title="Add list item",
                description="Personal list update",
                objective="add AP Gov to my classes list",
            )
        )

        self.assertEqual(decision.agent_name, "personal_ops_agent")

    def test_greetings_are_not_saved_to_durable_session_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._context(temp_dir)

            context.record_user_message("hi")
            context.record_assistant_reply("Hi. What can I help with?")

            snapshot = context.memory_store.snapshot()

        self.assertEqual(snapshot.session_turns, [])
        self.assertEqual(snapshot.user_facts, [])

    def test_proactive_routine_placeholder_is_honest_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            agent = PersonalOpsAgent(
                personal_store=JsonPersonalOpsStore(Path(temp_dir) / "personal_ops.json"),
                operator_context_service=self._context(temp_dir),
            )

            result = agent.handle_message("every Sunday summarize my open tasks")
            routines = agent.personal_store.list_proactive_routines()

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("not fully live", result.summary)
        self.assertEqual(len(routines), 1)
        self.assertFalse(routines[0].execution_live)
        self.assertEqual(routines[0].status, "planned")


if __name__ == "__main__":
    unittest.main()
