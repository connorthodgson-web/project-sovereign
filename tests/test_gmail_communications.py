"""Gmail coverage under the unified Communications Agent."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.communications_agent import CommunicationsAgent
from app.config import settings
from core.assistant import AssistantLayer
from core.interaction_context import InteractionContext, bind_interaction_context
from core.models import AgentExecutionStatus, RequestMode, TaskStatus
from core.operator_context import OperatorContextService
from core.planner import Planner
from core.router import Router
from core.state import TaskStateStore
from core.supervisor import Supervisor
from integrations.gmail_client import GmailClient, GmailReadiness, NormalizedGmailMessage
from memory.memory_store import MemoryStore
from memory.personal_ops_store import JsonPersonalOpsStore
from tools.capability_manifest import build_capability_catalog


class FakeGmailClient:
    def __init__(self, *, live: bool = True) -> None:
        self.live = live
        self.created_drafts: list[dict[str, str | None]] = []
        self.sent: list[dict[str, str]] = []
        self.archived: list[str] = []
        self.trashed: list[str] = []
        self.deleted: list[str] = []
        self.searches: list[dict[str, object]] = []

    def readiness(self) -> GmailReadiness:
        return GmailReadiness(
            enabled=self.live,
            configured=self.live,
            can_run_local_auth=self.live,
            live=self.live,
            blockers=() if self.live else ("GMAIL_ENABLED is false.",),
        )

    def setup_needed_message(self) -> str:
        return "Gmail setup is needed before I can use your mailbox. GMAIL_ENABLED is false."

    def search_messages(self, query: str, *, max_results: int = 10, include_body: bool = False):
        self.searches.append({"query": query, "max_results": max_results, "include_body": include_body})
        return [
            NormalizedGmailMessage(
                message_id="msg-1",
                thread_id="thread-1",
                subject="Homework update",
                from_="teacher@example.com",
                to="connor@example.com",
                date="Sun, 26 Apr 2026 09:00:00 -0400",
                snippet="Please review the assignment.",
                labels=("INBOX", "UNREAD"),
                body_text="Please review the assignment." if include_body else None,
            )
        ]

    def create_draft(self, *, to: str, subject: str, body_text: str, thread_id: str | None = None):
        payload = {"to": to, "subject": subject, "body_text": body_text, "thread_id": thread_id}
        self.created_drafts.append(payload)
        return {
            "draft_id": "draft-1",
            "message_id": "msg-draft",
            "thread_id": thread_id,
            "to": to,
            "subject": subject,
            "snippet": body_text[:160],
            "source": "gmail",
            "status": "draft_created",
        }

    def send_email(self, *, to: str, subject: str, body_text: str):
        payload = {"to": to, "subject": subject, "body_text": body_text}
        self.sent.append(payload)
        return {
            "message_id": "msg-sent",
            "thread_id": "thread-sent",
            "to": to,
            "subject": subject,
            "source": "gmail",
            "status": "sent",
        }

    def archive_message(self, *, message_id: str):
        self.archived.append(message_id)
        return {"message_id": message_id, "source": "gmail", "status": "archived"}

    def trash_message(self, *, message_id: str):
        self.trashed.append(message_id)
        return {"message_id": message_id, "source": "gmail", "status": "trashed"}

    def delete_message(self, *, message_id: str):
        self.deleted.append(message_id)
        return {"message_id": message_id, "source": "gmail", "status": "deleted"}


class _NoLlmClient:
    def is_configured(self) -> bool:
        return False


class GmailCommunicationsTests(unittest.TestCase):
    def _operator_context(self, temp_dir: str) -> OperatorContextService:
        return OperatorContextService(
            openrouter_client=_NoLlmClient(),
            task_store=TaskStateStore(),
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
        )

    def _operator_context_with_contacts(self, temp_dir: str) -> OperatorContextService:
        return OperatorContextService(
            openrouter_client=_NoLlmClient(),
            task_store=TaskStateStore(),
            memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
            personal_ops_store_instance=JsonPersonalOpsStore(Path(temp_dir) / "personal_ops.json"),
        )

    def _supervisor(self, temp_dir: str) -> Supervisor:
        llm = _NoLlmClient()
        operator_context = self._operator_context(temp_dir)
        router = Router(openrouter_client=llm)
        planner = Planner(openrouter_client=llm, agent_registry=router.agent_registry)
        assistant = AssistantLayer(openrouter_client=llm, operator_context_service=operator_context)
        return Supervisor(
            assistant_layer=assistant,
            planner=planner,
            router=router,
            operator_context_service=operator_context,
        )

    def test_gmail_disabled_returns_setup_needed_response_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            agent = CommunicationsAgent(
                gmail_client=FakeGmailClient(live=False),
                operator_context_service=context,
            )

            result = agent.handle_message("what emails did I get today?")

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("setup", result.summary.lower())
        self.assertIn("GMAIL_ENABLED", " ".join(result.blockers))
        self.assertNotIn("GMAIL_ENABLED", result.summary)

    def test_readiness_checks_paths_without_exposing_secret_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "gmail_credentials.json"
            token = Path(temp_dir) / "gmail_token.json"
            credentials.write_text('{"client_secret":"super-secret"}', encoding="utf-8")
            with (
                patch.object(settings, "gmail_enabled", True),
                patch.object(settings, "gmail_credentials_path", str(credentials)),
                patch.object(settings, "gmail_token_path", str(token)),
            ):
                readiness = GmailClient(runtime_settings=settings).readiness()

        blocker_text = " ".join(readiness.blockers)
        self.assertIn("GMAIL_TOKEN_PATH", blocker_text)
        self.assertNotIn("super-secret", blocker_text)

    def test_gmail_auth_helper_exists(self) -> None:
        self.assertTrue(callable(getattr(GmailClient(), "run_local_auth_flow")))

    def test_gmail_read_search_routes_to_communications_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            supervisor = self._supervisor(temp_dir)
            decision = supervisor.assistant_layer.decide("summarize unread emails")
            lane = supervisor._select_lane("summarize unread emails", decision)

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertEqual(lane.agent_id, "communications_agent")

    def test_do_i_have_emails_from_routes_to_communications_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            supervisor = self._supervisor(temp_dir)
            decision = supervisor.assistant_layer.decide("do I have any emails from teacher@example.com?")
            lane = supervisor._select_lane("do I have any emails from teacher@example.com?", decision)

        self.assertEqual(decision.mode, RequestMode.ACT)
        self.assertEqual(lane.agent_id, "communications_agent")

    def test_search_email_for_phrase_uses_clean_query(self) -> None:
        fake = FakeGmailClient()
        agent = CommunicationsAgent(gmail_client=fake)

        result = agent.handle_message("search my email for tuition receipt")

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.searches[0]["query"], "tuition receipt")

    def test_summarize_recent_important_emails_includes_fake_body_results(self) -> None:
        fake = FakeGmailClient()
        agent = CommunicationsAgent(gmail_client=fake)

        result = agent.handle_message("summarize recent important emails")

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertIn("newer_than:7d", fake.searches[0]["query"])
        self.assertIn("is:important", fake.searches[0]["query"])
        self.assertTrue(fake.searches[0]["include_body"])
        self.assertIn("Please review the assignment.", result.summary)

    def test_draft_email_can_be_created_without_sending(self) -> None:
        fake = FakeGmailClient()
        agent = CommunicationsAgent(gmail_client=fake)

        result = agent.handle_message('draft an email to teacher@example.com saying "Thanks, I will review it."')

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(len(fake.created_drafts), 1)
        self.assertEqual(fake.sent, [])

    def test_ambiguous_recipient_asks_one_follow_up_without_drafting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            result = agent.handle_message('draft an email to Alex saying "Can you send the notes?"')

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(result.summary, "Which email address should I use for Alex?")
        self.assertEqual(fake.created_drafts, [])
        self.assertEqual(fake.sent, [])

    def test_explicit_contact_statement_saves_mom_email_outside_semantic_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)

            context.record_user_message("Mom's email is mom@example.com")
            contacts = context.personal_ops_store.find_contacts("Mom")
            memory = context.memory_store.snapshot()

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].alias, "Mom")
        self.assertEqual(contacts[0].email, "mom@example.com")
        self.assertEqual(memory.user_facts, [])
        self.assertEqual(memory.session_turns, [])

    def test_draft_email_to_saved_contact_resolves_alias_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            saved = agent.handle_message("Mom's email is mom@example.com")
            result = agent.handle_message('draft an email to Mom saying "Thanks for everything."')

        self.assertEqual(saved.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.created_drafts[0]["to"], "mom@example.com")
        self.assertEqual(fake.sent, [])

    def test_send_email_to_saved_contact_still_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            agent.handle_message("Mom's email is mom@example.com")
            staged = agent.handle_message('send Mom an email saying "I arrived."')
            self.assertEqual(fake.sent, [])
            confirmed = agent.handle_message("confirm")

        self.assertEqual(staged.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("confirm", staged.summary.lower())
        self.assertEqual(confirmed.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.sent[0]["to"], "mom@example.com")

    def test_unknown_contact_asks_for_email_without_backend_jargon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            result = agent.handle_message('draft an email to Taylor saying "Can you send the notes?"')

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(result.summary, "Which email address should I use for Taylor?")
        self.assertEqual(fake.created_drafts, [])
        self.assertNotIn("gmail_recipient", result.summary)
        self.assertNotIn("communications_agent", result.summary)
        self.assertNotIn("tool", result.summary.lower())

    def test_ambiguous_contact_asks_clarification_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)
            context.personal_ops_store.upsert_contact(alias="Alex Smith", email="alex.smith@example.com")
            context.personal_ops_store.upsert_contact(alias="Alex Jones", email="alex.jones@example.com")
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            result = agent.handle_message('draft an email to Alex saying "Can you send the notes?"')

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("more than one contact", result.summary.lower())
        self.assertIn("Alex Smith", result.summary)
        self.assertIn("Alex Jones", result.summary)
        self.assertEqual(fake.created_drafts, [])
        self.assertEqual(fake.sent, [])

    def test_contact_update_uses_new_email_when_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            agent.handle_message("Mom's email is old-mom@example.com")
            updated = agent.handle_message("Mom's email changed to new-mom@example.com")
            result = agent.handle_message('draft an email to Mom saying "Thanks."')

        self.assertEqual(updated.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.created_drafts[0]["to"], "new-mom@example.com")
        self.assertEqual(len(context.personal_ops_store.find_contacts("Mom")), 1)

    def test_secret_like_contact_statement_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context_with_contacts(temp_dir)

            context.record_user_message("api key is fake@example.com")
            context.record_user_message("Remember that my access token is fake@example.com")

        self.assertEqual(context.personal_ops_store.list_contacts(), [])

    def test_ambiguous_recipient_follow_up_resumes_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            first = agent.handle_message('draft an email to Alex saying "Can you send the notes?"')
            resumed = context.resume_pending_question_if_answer("alex@example.com")
            assert resumed is not None
            second = agent.handle_message(resumed.replace("to Alex", "to alex@example.com"))

        self.assertEqual(first.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(second.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.created_drafts[0]["to"], "alex@example.com")
        self.assertEqual(fake.sent, [])

    def test_reply_to_latest_email_creates_threaded_draft_without_sending(self) -> None:
        fake = FakeGmailClient()
        agent = CommunicationsAgent(gmail_client=fake)

        result = agent.handle_message('reply to the latest email from teacher@example.com saying "Got it, thank you."')

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.searches[0]["query"], "from:teacher@example.com")
        self.assertEqual(fake.searches[0]["max_results"], 1)
        self.assertEqual(fake.created_drafts[0]["to"], "teacher@example.com")
        self.assertEqual(fake.created_drafts[0]["subject"], "Re: Homework update")
        self.assertEqual(fake.created_drafts[0]["thread_id"], "thread-1")
        self.assertEqual(fake.sent, [])

    def test_send_email_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            staged = agent.handle_message('send an email to parent@example.com saying "I arrived."')
            self.assertEqual(fake.sent, [])
            confirmed = agent.handle_message("yes")

        self.assertEqual(staged.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("confirm", staged.summary.lower())
        self.assertEqual(confirmed.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.sent[0]["to"], "parent@example.com")

    def test_gmail_confirmations_are_session_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U1")):
                staged = agent.handle_message('send an email to parent@example.com saying "I arrived."')
            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U2")):
                other_user = agent.handle_message("yes")
            with bind_interaction_context(InteractionContext(source="slack", channel_id="D1", user_id="U1")):
                confirming_user = agent.handle_message("yes")

        self.assertEqual(staged.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(other_user.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(fake.sent[0]["to"], "parent@example.com")
        self.assertEqual(confirming_user.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(len(fake.sent), 1)

    def test_delete_trash_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            staged = agent.handle_message("delete email msg-1")
            self.assertEqual(fake.trashed, [])
            confirmed = agent.handle_message("confirm")

        self.assertEqual(staged.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(confirmed.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.trashed, ["msg-1"])

    def test_archive_bulk_action_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            staged = agent.handle_message("archive all newsletters")
            self.assertEqual(fake.archived, [])
            confirmed = agent.handle_message("yes")

        self.assertEqual(staged.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(confirmed.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(fake.archived, ["msg-1"])

    def test_external_recipient_send_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self._operator_context(temp_dir)
            fake = FakeGmailClient()
            agent = CommunicationsAgent(gmail_client=fake, operator_context_service=context)

            result = agent.handle_message('send an email to external@example.com saying "hello"')

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(fake.sent, [])
        self.assertIn("external@example.com", result.summary)

    def test_gmail_output_normalized_cleanly(self) -> None:
        fake = FakeGmailClient()
        agent = CommunicationsAgent(gmail_client=fake)

        result = agent.handle_message("find emails from teacher@example.com")
        payload = result.evidence[0].payload
        message = payload["messages"][0]

        self.assertEqual(message["message_id"], "msg-1")
        self.assertEqual(message["thread_id"], "thread-1")
        self.assertEqual(message["from"], "teacher@example.com")
        self.assertEqual(message["source"], "gmail")

    def test_sms_discord_future_disabled_not_callable(self) -> None:
        agent = CommunicationsAgent(gmail_client=FakeGmailClient())

        sms = agent.handle_message("send a text message saying hello")
        discord = agent.handle_message("send this to Discord")

        self.assertEqual(sms.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(discord.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("future", sms.summary.lower())
        self.assertIn("future", discord.summary.lower())

    def test_calendar_and_reminder_requests_do_not_route_to_gmail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            supervisor = self._supervisor(temp_dir)
            calendar_decision = supervisor.assistant_layer.decide("what do I have today?")
            reminder_decision = supervisor.assistant_layer.decide("remind me in 5 minutes to stretch")

        self.assertNotEqual(calendar_decision.intent_label, "communications_email")
        self.assertEqual(reminder_decision.intent_label, "reminder_action")

    def test_thanks_after_email_action_stays_assistant_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            supervisor = self._supervisor(temp_dir)

            thanks = supervisor.handle_user_goal("thanks")

        self.assertEqual(thanks.request_mode, RequestMode.ANSWER)
        self.assertEqual(thanks.planner_mode, "conversation_fast_path")

    def test_manifest_lists_gmail_and_future_channels(self) -> None:
        catalog = build_capability_catalog()

        gmail = catalog.snapshot_for("gmail")
        sms = catalog.snapshot_for("sms")
        discord = catalog.snapshot_for("discord")

        self.assertIsNotNone(gmail)
        self.assertEqual(gmail.owner_agent, "communications_agent")
        self.assertTrue(gmail.requires_credentials)
        self.assertEqual(sms.status, "planned")
        self.assertEqual(discord.status, "planned")


if __name__ == "__main__":
    unittest.main()
