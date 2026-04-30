"""Communications-focused agent implementation."""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.operator_context import OperatorContextService, operator_context
from core.logging import get_logger
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    NormalizedToolOutput,
    SubTask,
    Task,
    ToolEvidence,
    ToolInvocation,
)
from integrations.gmail_client import GmailClient, NormalizedGmailMessage
from integrations.readiness import build_integration_readiness
from memory.contacts import is_email_address, parse_explicit_contact_statement
from memory.contracts import PersonalOpsStore
from tools.registry import ToolRegistry, build_default_tool_registry


class CommunicationsAgent(BaseAgent):
    """Owns communication channels: Slack now, Gmail now, SMS/Discord later."""

    name = "communications_agent"
    supported_tool_names = frozenset({"slack_messaging_tool"})

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        gmail_client: GmailClient | None = None,
        operator_context_service: OperatorContextService | None = None,
        contacts_store: PersonalOpsStore | None = None,
    ) -> None:
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.gmail_client = gmail_client or GmailClient()
        self.operator_context = operator_context_service or operator_context
        self.contacts_store = contacts_store or self.operator_context.personal_ops_store
        self.logger = get_logger(__name__)

    def handle_message(self, user_message: str, *, allow_follow_up_state: bool = True) -> AgentResult:
        """Handle a bounded natural-language communications request."""

        confirmation = self._handle_pending_gmail_confirmation(user_message)
        if confirmation is not None:
            return confirmation
        contact = parse_explicit_contact_statement(user_message)
        if contact is not None:
            try:
                record = self.contacts_store.upsert_contact(alias=contact.alias, email=contact.email)
            except (AttributeError, ValueError) as exc:
                return AgentResult(
                    subtask_id="communications-contact-memory",
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary="I couldn't save that contact email safely.",
                    tool_name="contacts",
                    blockers=[str(exc)],
                )
            return AgentResult(
                subtask_id="communications-contact-memory",
                agent=self.name,
                status=AgentExecutionStatus.COMPLETED,
                summary=f"Got it. I'll use {record.alias} for future email drafts and guarded sends.",
                tool_name="contacts",
                evidence=[
                    ToolEvidence(
                        tool_name="contacts",
                        summary="Saved explicit contact alias outside ordinary semantic memory.",
                        payload={
                            "alias": record.alias,
                            "email": record.email,
                            "source": "personal_ops_contacts",
                        },
                        verification_notes=["Contact was explicitly supplied by the user."],
                    )
                ],
            )
        if self._looks_like_future_channel_request(user_message):
            return AgentResult(
                subtask_id="communications-future-channel",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="That communications channel is a future placeholder and is not callable yet.",
                tool_name="communications_agent",
                blockers=["SMS and Discord are intentionally future-disabled in this pass."],
                next_actions=["Use Gmail or Slack for currently supported communications work."],
            )
        if not self._looks_like_gmail_request(user_message):
            return AgentResult(
                subtask_id="communications-unhandled",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="This does not look like a supported communications action.",
                tool_name="communications_agent",
                blockers=["No supported Gmail or Slack communication intent was detected."],
            )

        readiness = self.gmail_client.readiness()
        if not readiness.live:
            return self._gmail_setup_needed_result()

        try:
            parsed = self._parse_gmail_request(user_message)
            action = parsed["action"]
            if action == "blocked_ambiguous_recipient":
                recipient_name = str(parsed.get("recipient_name") or "that recipient")
                question = f"Which email address should I use for {recipient_name}?"
                if allow_follow_up_state:
                    self.operator_context.set_pending_question(
                        original_user_intent=user_message,
                        missing_field="gmail_recipient",
                        expected_answer_type="text",
                        resume_target="gmail_recipient",
                        tool_or_agent="communications_agent",
                        question=question,
                        supplied_slots={"recipient_name": recipient_name},
                    )
                return AgentResult(
                    subtask_id="communications-gmail-recipient",
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary=question,
                    tool_name="gmail",
                    blockers=["The recipient name did not include a concrete email address."],
                    next_actions=["Reply with the email address to use."],
                )
            if action == "blocked_multiple_recipients":
                recipient_name = str(parsed.get("recipient_name") or "that recipient")
                matches = list(parsed.get("matches") or [])
                options = ", ".join(str(item) for item in matches[:5])
                question = f"I found more than one contact for {recipient_name}: {options}. Which one should I use?"
                if allow_follow_up_state:
                    self.operator_context.set_pending_question(
                        original_user_intent=user_message,
                        missing_field="gmail_recipient",
                        expected_answer_type="text",
                        resume_target="gmail_recipient",
                        tool_or_agent="communications_agent",
                        question=question,
                        supplied_slots={"recipient_name": recipient_name},
                    )
                return AgentResult(
                    subtask_id="communications-gmail-recipient-ambiguous",
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary=question,
                    tool_name="gmail",
                    blockers=["More than one saved contact matched that recipient name."],
                    next_actions=["Reply with the exact contact name or email address to use."],
                )
            if action == "reply":
                return self._create_reply_draft(parsed)
            if action == "draft":
                payload = self.gmail_client.create_draft(
                    to=parsed["to"],
                    subject=parsed["subject"],
                    body_text=parsed["body_text"],
                )
                return self._completed_gmail_result(
                    "Created a Gmail draft without sending it.",
                    payload,
                )
            if action == "send":
                self.operator_context.set_pending_confirmation("gmail_action", parsed)
                return self._confirmation_required_result(
                    f"Please confirm: send email with Gmail to `{parsed['to']}` with subject `{parsed['subject']}`?",
                    "Sending email requires explicit confirmation.",
                    parsed,
                )
            if action == "forward":
                self.operator_context.set_pending_confirmation("gmail_action", parsed)
                return self._confirmation_required_result(
                    f"Please confirm: prepare to forward Gmail message(s) matching `{parsed.get('query')}` to `{parsed.get('to')}`?",
                    "Forwarding email requires explicit confirmation.",
                    parsed,
                )
            if action in {"trash", "delete", "archive"}:
                self.operator_context.set_pending_confirmation("gmail_action", parsed)
                verb = "permanently delete" if action == "delete" else action
                target = parsed.get("message_id") or parsed.get("query")
                return self._confirmation_required_result(
                    f"Please confirm: {verb} Gmail message(s) matching `{target}`?",
                    "Mailbox changes require explicit confirmation.",
                    parsed,
                )
            if action == "blocked_missing_recipient":
                question = "Who should receive that email?"
                if allow_follow_up_state:
                    self.operator_context.set_pending_question(
                        original_user_intent=user_message,
                        missing_field="gmail_recipient",
                        expected_answer_type="text",
                        resume_target="gmail_recipient",
                        tool_or_agent="communications_agent",
                        question=question,
                    )
                return AgentResult(
                    subtask_id="communications-gmail-missing-recipient",
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary=question,
                    tool_name="gmail",
                    blockers=["The outbound email request did not include a recipient."],
                    next_actions=["Tell me who should receive the email and what it should say."],
                )
            if action == "blocked_missing_body":
                return AgentResult(
                    subtask_id="communications-gmail-missing-body",
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary="What should the reply say?",
                    tool_name="gmail",
                    blockers=["The reply request did not include message text."],
                    next_actions=["Tell me the reply text to draft."],
                )
            messages = self.gmail_client.search_messages(
                parsed["query"],
                max_results=int(parsed.get("max_results", 10)),
                include_body=bool(parsed.get("include_body", False)),
            )
            return self._gmail_read_result(messages, query=parsed["query"])
        except RuntimeError as exc:
            return AgentResult(
                subtask_id="communications-gmail-runtime",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary=str(exc),
                tool_name="gmail",
                blockers=[str(exc)],
            )

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        invocation = subtask.tool_invocation
        self.logger.info(
            "COMMUNICATIONS_AGENT_START task=%s subtask=%s action=%s tool=%s",
            task.id,
            subtask.id,
            invocation.action if invocation is not None else None,
            invocation.tool_name if invocation is not None else None,
        )
        if invocation is None:
            if self._looks_like_gmail_request(f"{task.goal} {subtask.objective}"):
                return self.handle_message(task.goal, allow_follow_up_state=False)
            return self._blocked_without_invocation(task, subtask)

        if invocation.tool_name == "slack_messaging_tool":
            result = self.execute_tool_invocation(
                task,
                subtask,
                tool_registry=self.tool_registry,
                validate_invocation=self._validate_invocation,
                build_evidence=self._build_execution_evidence,
                build_summary=self._build_summary,
                build_details=self._build_execution_details,
                build_artifacts=self._build_execution_artifacts,
                build_next_actions=self._build_next_actions,
            )
            if result.evidence:
                self.logger.info(
                    "TOOL_EVIDENCE agent=%s task=%s subtask=%s target=%s status=%s",
                    self.name,
                    task.id,
                    subtask.id,
                    result.evidence[0].payload.get("target", ""),
                    result.evidence[0].payload.get("status", ""),
                )
            return result

        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="The requested communications tool is not supported by the current runtime.",
            tool_name=invocation.tool_name,
            details=[
                f"Goal context: {task.goal}",
                f"Communication objective: {subtask.objective}",
                f"Invocation: {invocation.model_dump()}",
            ],
            blockers=[f"Unsupported communications tool: {invocation.tool_name}."],
            next_actions=["Use the Slack outbound messaging path or keep this request blocked honestly."],
        )

    def _blocked_without_invocation(self, task: Task, subtask: SubTask) -> AgentResult:
        lowered = f"{task.goal} {subtask.objective}".lower()
        readiness = build_integration_readiness()
        if "email" in lowered:
            email = readiness["integration:gmail"]
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="Gmail setup is needed before I can use your mailbox.",
                details=[
                    f"Goal context: {task.goal}",
                    f"Communication objective: {subtask.objective}",
                    f"Gmail capability status: {email.status}",
                ],
                blockers=[
                    "Gmail is not ready for live use.",
                    *[
                        f"Missing Gmail config: {field}"
                        for field in email.missing_fields[:3]
                    ],
                ],
                next_actions=[
                    "Complete Gmail OAuth setup and make sure saved Gmail access exists.",
                ],
            )

        channels = self._infer_channels(task.goal)
        slack_readiness = readiness["integration:slack_outbound"]
        blockers = []
        if "slack" in channels:
            blockers.append(
                "Slack outbound needs a concrete channel id/name or Slack user id before sending."
            )
            blockers.extend(
                f"Missing Slack config: {field}" for field in slack_readiness.missing_fields[:2]
            )
        if "email" in channels:
            blockers.append("Gmail needs OAuth setup before email execution can run.")
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="A communication-oriented step was recognized, but it did not include enough structured Slack delivery input to run.",
            details=[
                f"Goal context: {task.goal}",
                f"Communication objective: {subtask.objective}",
                f"Candidate channels: {', '.join(channels)}",
                f"Slack outbound status: {slack_readiness.status}",
            ],
            blockers=blockers or ["No supported communications invocation was attached to this subtask."],
            next_actions=[
                "Attach a supported Slack outbound tool invocation with a message and target.",
                "Use Gmail through the Communications Agent once OAuth setup is complete.",
            ],
        )

    def _handle_pending_gmail_confirmation(self, user_message: str) -> AgentResult | None:
        if user_message.lower().strip() not in {"yes", "confirm", "confirmed", "do it", "go ahead"}:
            return None
        pending = self.operator_context.pop_pending_confirmation("gmail_action")
        if not pending:
            return None
        readiness = self.gmail_client.readiness()
        if not readiness.live:
            return self._gmail_setup_needed_result()
        action = str(pending.get("action", ""))
        try:
            if action == "send":
                payload = self.gmail_client.send_email(
                    to=str(pending["to"]),
                    subject=str(pending["subject"]),
                    body_text=str(pending["body_text"]),
                )
                return self._completed_gmail_result("Sent the Gmail message after confirmation.", payload)
            if action == "send_draft":
                payload = self.gmail_client.send_draft(draft_id=str(pending["draft_id"]))
                return self._completed_gmail_result("Sent the Gmail draft after confirmation.", payload)
            if action == "archive":
                return self._confirmed_message_change(pending, action="archive")
            if action == "trash":
                return self._confirmed_message_change(pending, action="trash")
            if action == "delete":
                return self._confirmed_message_change(pending, action="delete")
            if action == "forward":
                return AgentResult(
                    subtask_id="communications-gmail-forward",
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Gmail forwarding is not implemented yet after confirmation.",
                    tool_name="gmail",
                    blockers=["Forwarding needs a dedicated Gmail message construction path before it can safely execute."],
                    next_actions=["Draft a new email manually from the source message content for now."],
                )
        except RuntimeError as exc:
            return AgentResult(
                subtask_id="communications-gmail-confirmation",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary=str(exc),
                tool_name="gmail",
                blockers=[str(exc)],
            )
        return None

    def _confirmed_message_change(self, pending: dict[str, object], *, action: str) -> AgentResult:
        message_ids = self._resolve_message_ids_for_pending_change(pending)
        if not message_ids:
            return AgentResult(
                subtask_id="communications-gmail-change",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="No Gmail messages matched the confirmed mailbox action.",
                tool_name="gmail",
                blockers=["No message id or matching search result was available."],
            )
        if action in {"archive", "trash"} and len(message_ids) > 25:
            return AgentResult(
                subtask_id="communications-gmail-bulk-change",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="That Gmail bulk action is too broad for this pass.",
                tool_name="gmail",
                blockers=["Bulk Gmail changes are capped at 25 messages per confirmed action."],
            )
        changed = []
        for message_id in message_ids:
            if action == "archive":
                changed.append(self.gmail_client.archive_message(message_id=message_id))
            elif action == "trash":
                changed.append(self.gmail_client.trash_message(message_id=message_id))
            else:
                changed.append(self.gmail_client.delete_message(message_id=message_id))
        return self._completed_gmail_result(
            f"Confirmed Gmail {action} completed for {len(changed)} message(s).",
            {"messages": changed, "source": "gmail", "status": action},
        )

    def _resolve_message_ids_for_pending_change(self, pending: dict[str, object]) -> list[str]:
        message_id = str(pending.get("message_id") or "").strip()
        if message_id:
            return [message_id]
        query = str(pending.get("query") or "").strip()
        if not query:
            return []
        max_results = int(pending.get("max_results", 10))
        return [item.message_id for item in self.gmail_client.search_messages(query, max_results=max_results)]

    def _parse_gmail_request(self, user_message: str) -> dict[str, object]:
        lowered = user_message.lower()
        if any(word in lowered for word in ("delete", "trash", "remove")):
            action = "delete" if "permanent" in lowered else "trash"
            return self._parse_mailbox_change(user_message, action=action)
        if "archive" in lowered:
            return self._parse_mailbox_change(user_message, action="archive")
        if "forward" in lowered:
            return {
                **self._parse_mailbox_change(user_message, action="forward"),
                "to": self._extract_recipient(user_message) or "",
            }
        if "reply" in lowered:
            content = self._parse_email_content(user_message)
            query = self._reply_query_from_request(user_message)
            if not self._extract_body(user_message) or self._extract_body(user_message) == user_message.strip():
                return {
                    "action": "blocked_missing_body",
                    "query": query,
                    "max_results": 1,
                    "include_body": False,
                }
            return {
                "action": "reply",
                "query": query,
                "max_results": 1,
                "include_body": False,
                "body_text": content["body_text"],
            }
        if "send" in lowered and self._extract_recipient(user_message):
            if "draft" in lowered:
                return self._outbound_email_action("draft", user_message)
            return self._outbound_email_action("send", user_message)
        if lowered.startswith("message ") and self._extract_recipient(user_message):
            return self._outbound_email_action("draft", user_message)
        if self._looks_like_outbound_email_without_recipient(user_message):
            return {
                "action": "blocked_missing_recipient",
                "query": "",
                "max_results": 0,
                "include_body": False,
            }
        if "draft" in lowered:
            return self._outbound_email_action("draft", user_message)
        return {
            "action": "search",
            "query": self._query_from_request(user_message),
            "max_results": 10,
            "include_body": any(word in lowered for word in ("summarize", "summary", "full", "read")),
        }

    def _outbound_email_action(self, action: str, user_message: str) -> dict[str, object]:
        content = self._parse_email_content(user_message)
        recipient = content["to"]
        if not recipient:
            return {"action": "blocked_missing_recipient", "query": "", "max_results": 0, "include_body": False}
        if not self._is_email_address(recipient):
            resolution = self._resolve_contact_recipient(recipient)
            if resolution["status"] == "resolved":
                content["to"] = str(resolution["email"])
                content["recipient_alias"] = str(resolution["alias"])
            elif resolution["status"] == "ambiguous":
                return {
                    "action": "blocked_multiple_recipients",
                    "recipient_name": recipient,
                    "matches": resolution["matches"],
                }
            else:
                return {"action": "blocked_ambiguous_recipient", "recipient_name": recipient}
        return {"action": action, **content}

    def _create_reply_draft(self, parsed: dict[str, object]) -> AgentResult:
        messages = self.gmail_client.search_messages(
            str(parsed["query"]),
            max_results=int(parsed.get("max_results", 1)),
            include_body=bool(parsed.get("include_body", False)),
        )
        if not messages:
            return AgentResult(
                subtask_id="communications-gmail-reply-missing-source",
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="I couldn't find an email to reply to.",
                tool_name="gmail",
                blockers=["No source Gmail message matched the reply request."],
            )
        source = messages[0]
        subject = source.subject if source.subject.lower().startswith("re:") else f"Re: {source.subject or 'Message'}"
        payload = self.gmail_client.create_draft(
            to=source.from_,
            subject=subject,
            body_text=str(parsed["body_text"]),
            thread_id=source.thread_id,
        )
        return self._completed_gmail_result(
            f"Created a reply draft to {source.from_} without sending it.",
            payload,
        )

    def _parse_mailbox_change(self, user_message: str, *, action: str) -> dict[str, object]:
        message_id_match = re.search(r"\b(?:message|email)\s+([A-Za-z0-9_-]{8,})\b", user_message, flags=re.IGNORECASE)
        message_id = message_id_match.group(1) if message_id_match else ""
        query = self._query_from_request(user_message)
        bulk = any(word in user_message.lower() for word in ("all", "many", "newsletters", "bulk"))
        return {
            "action": action,
            "message_id": message_id,
            "query": query,
            "max_results": 10 if bulk else 1,
            "bulk": bulk,
        }

    def _parse_email_content(self, user_message: str) -> dict[str, str]:
        recipient = self._extract_recipient(user_message) or ""
        subject = self._extract_subject(user_message)
        body = self._extract_body(user_message)
        return {"to": recipient, "subject": subject, "body_text": body}

    def _extract_recipient(self, user_message: str) -> str | None:
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", user_message)
        if email_match:
            return email_match.group(0)
        to_match = re.search(r"\bto\s+([^,]+?)(?:\s+saying|\s+that|\s+with subject|$)", user_message, flags=re.IGNORECASE)
        if to_match:
            return to_match.group(1).strip(" .")
        direct_message_match = re.search(
            r"^(?:please\s+)?(?:message|email|mail)\s+([^,]+?)(?:\s+saying|\s+that|\s+with subject|$)",
            user_message,
            flags=re.IGNORECASE,
        )
        if direct_message_match:
            return direct_message_match.group(1).strip(" .")
        send_name_match = re.search(
            r"\bsend\s+(?!an?\s+email\b)(?!email\b)([^,]+?)\s+(?:an?\s+)?(?:email|gmail|message|mail)\b",
            user_message,
            flags=re.IGNORECASE,
        )
        if send_name_match:
            return send_name_match.group(1).strip(" .")
        return None

    def _looks_like_outbound_email_without_recipient(self, user_message: str) -> bool:
        lowered = user_message.lower()
        if not any(token in lowered for token in ("email", "gmail", "mail")):
            return False
        return lowered.startswith(("email ", "send email", "send an email", "send a gmail", "mail "))

    def _extract_subject(self, user_message: str) -> str:
        subject_match = re.search(r"\bsubject\s+['\"]?(.+?)['\"]?(?:\s+saying|\s+body\s+|$)", user_message, flags=re.IGNORECASE)
        if subject_match:
            return subject_match.group(1).strip(" .'\"")
        return "Message from Sovereign"

    def _extract_body(self, user_message: str) -> str:
        quoted = re.findall(r'"([^"]+)"|' + r"'([^']+)'", user_message)
        for pair in quoted:
            candidate = next((item for item in pair if item), "").strip()
            if candidate:
                return candidate
        for marker in (" saying ", " that says ", " body ", " with message "):
            index = user_message.lower().find(marker)
            if index >= 0:
                return user_message[index + len(marker) :].strip(" .'\"")
        return user_message.strip()

    def _query_from_request(self, user_message: str) -> str:
        lowered = user_message.lower()
        parts: list[str] = []
        if "unread" in lowered:
            parts.append("is:unread")
        if "today" in lowered:
            parts.append("newer_than:1d")
        if "recent" in lowered and "today" not in lowered:
            parts.append("newer_than:7d")
        if "important" in lowered:
            parts.append("is:important")
        if "newsletter" in lowered:
            parts.append("newsletter")
        sender = re.search(r"\bfrom\s+([\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|[A-Za-z][A-Za-z ._-]{1,40})", user_message, flags=re.IGNORECASE)
        if sender:
            value = sender.group(1).strip(" .")
            parts.append(f"from:{value}")
        search_for = re.search(r"\b(?:search|find)\b.+?\b(?:email|emails|gmail|mailbox|inbox)\b\s+for\s+(.+)$", user_message, flags=re.IGNORECASE)
        if search_for:
            value = search_for.group(1).strip(" .'\"")
            if value:
                parts.append(value)
        if not parts:
            cleaned = re.sub(r"\b(what|which|do|have|any|my|for|emails?|gmail|mailbox|inbox|did|i|get|got|summarize|summary|find|search|read|recent|important|today|unread|delete|trash|archive|all|this)\b", " ", lowered)
            cleaned = " ".join(cleaned.split())
            if cleaned:
                parts.append(cleaned)
        return " ".join(parts) or "newer_than:7d"

    def _reply_query_from_request(self, user_message: str) -> str:
        sender = re.search(r"\bfrom\s+([\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|[A-Za-z][A-Za-z ._-]{1,40})", user_message, flags=re.IGNORECASE)
        if sender:
            return f"from:{sender.group(1).strip(' .')}"
        return self._query_from_request(user_message)

    def _is_email_address(self, value: str) -> bool:
        return is_email_address(value.strip())

    def _looks_like_gmail_request(self, user_message: str) -> bool:
        lowered = user_message.lower()
        if parse_explicit_contact_statement(user_message) is not None:
            return True
        if lowered.startswith("message ") and " saying " in lowered:
            return True
        return any(token in lowered for token in ("gmail", "email", "emails", "mailbox", "newsletter", "newsletters", "inbox"))

    def _resolve_contact_recipient(self, recipient: str) -> dict[str, object]:
        try:
            matches = self.contacts_store.find_contacts(recipient)
        except AttributeError:
            matches = []
        if len(matches) == 1:
            contact = matches[0]
            return {
                "status": "resolved",
                "email": contact.email,
                "alias": contact.alias,
            }
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "matches": [contact.alias for contact in matches],
            }
        return {"status": "unknown"}

    def _looks_like_future_channel_request(self, user_message: str) -> bool:
        lowered = user_message.lower()
        return any(channel in lowered for channel in ("sms", "text message", "discord"))

    def _gmail_setup_needed_result(self) -> AgentResult:
        readiness = self.gmail_client.readiness()
        return AgentResult(
            subtask_id="communications-gmail-readiness",
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=(
                "Gmail setup is needed before I can use your mailbox. Gmail is not live yet. "
                "I need Gmail enabled, OAuth credentials, and saved Gmail access before I can read, draft, or send email."
            ),
            tool_name="gmail",
            blockers=["Gmail/email is scaffolded until OAuth setup is complete.", *list(readiness.blockers)],
            next_actions=["Add Gmail OAuth credentials, run the local auth helper once, then retry."],
        )

    def _confirmation_required_result(
        self,
        summary: str,
        blocker: str,
        pending: dict[str, object],
    ) -> AgentResult:
        return AgentResult(
            subtask_id="communications-gmail-confirm",
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=summary,
            tool_name="gmail",
            evidence=[
                ToolEvidence(
                    tool_name="gmail",
                    summary="Gmail action staged pending explicit user confirmation.",
                    payload=self._redacted_pending_payload(pending),
                )
            ],
            blockers=[blocker],
            next_actions=["Reply yes or confirm to execute the staged Gmail action."],
        )

    def _completed_gmail_result(self, summary: str, payload: dict[str, object]) -> AgentResult:
        return AgentResult(
            subtask_id="communications-gmail",
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=summary,
            tool_name="gmail",
            evidence=[
                ToolEvidence(
                    tool_name="gmail",
                    summary=summary,
                    payload=dict(payload),
                    verification_notes=["Gmail API returned structured provider metadata."],
                )
            ],
        )

    def _gmail_read_result(self, messages: list[NormalizedGmailMessage], *, query: str) -> AgentResult:
        payload = {
            "query": query,
            "messages": [message.as_dict() for message in messages],
            "count": len(messages),
            "source": "gmail",
        }
        subjects = [self._message_summary_line(message) for message in messages[:5]]
        summary = (
            f"Found {len(messages)} Gmail message(s): {', '.join(subjects)}"
            if messages
            else "No Gmail messages matched that request."
        )
        return self._completed_gmail_result(summary, payload)

    def _message_summary_line(self, message: NormalizedGmailMessage) -> str:
        subject = message.subject or "(no subject)"
        sender = message.from_ or "unknown sender"
        preview = (message.body_text or message.snippet or "").strip()
        if preview:
            return f"{subject} from {sender}: {preview[:120]}"
        return f"{subject} from {sender}"

    def _redacted_pending_payload(self, pending: dict[str, object]) -> dict[str, object]:
        allowed = {"action", "to", "subject", "message_id", "query", "max_results", "bulk"}
        return {key: value for key, value in pending.items() if key in allowed}

    def _validate_invocation(self, invocation: ToolInvocation) -> str | None:
        if self.tool_registry.get(invocation.tool_name) is None:
            return f"Unsupported tool invocation: {invocation.tool_name}"
        if invocation.tool_name != "slack_messaging_tool":
            return f"Unsupported communications tool: {invocation.tool_name}"
        if invocation.action not in {"send_channel_message", "send_dm"}:
            return f"Unsupported Slack messaging action: {invocation.action}"
        message_text = " ".join((invocation.parameters.get("message_text") or "").split())
        if not message_text:
            return "Slack messaging invocation is missing the required 'message_text' parameter."
        if invocation.action == "send_channel_message":
            if not any(invocation.parameters.get(key) for key in ("channel_id", "channel", "target")):
                return "Slack channel messaging requires a concrete channel id or channel name."
            return None
        if not any(invocation.parameters.get(key) for key in ("channel_id", "user_id", "target")):
            return "Slack DM messaging requires a DM channel id or Slack user id."
        target = " ".join((invocation.parameters.get("target") or "").split())
        if target.startswith("@"):
            return "Slack DM messaging cannot resolve a plain @username yet; provide a Slack user id."
        return None

    def _build_execution_evidence(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[ToolEvidence]:
        return [
            ToolEvidence(
                tool_name=invocation.tool_name,
                summary=normalized_output.summary,
                payload=normalized_output.payload,
                verification_notes=[],
            )
        ]

    def _build_summary(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> str:
        payload = normalized_output.payload
        target = self._payload_text(payload, "channel") or self._payload_text(payload, "target")
        message_text = self._payload_text(payload, "message_text")
        if normalized_output.success:
            if invocation.action == "send_dm":
                return f"Sent Slack DM to {target or 'the requested recipient'}: {message_text or ''}".strip()
            return f"Sent Slack message to {target or 'the requested channel'}: {message_text or ''}".strip()
        return normalized_output.summary or "Slack delivery could not be completed."

    def _build_execution_details(
        self,
        task: Task,
        subtask: SubTask,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        payload = normalized_output.payload
        details = [
            f"Goal context: {task.goal}",
            f"Communication objective: {subtask.objective}",
            f"Invocation: {invocation.model_dump()}",
        ]
        target = self._payload_text(payload, "channel") or self._payload_text(payload, "target")
        if target:
            details.append(f"Slack target: {target}")
        message_text = self._payload_text(payload, "message_text")
        if message_text:
            details.append(f"Message text: {message_text}")
        timestamp = self._payload_text(payload, "timestamp")
        if timestamp:
            details.append(f"Slack timestamp: {timestamp}")
        response_id = self._payload_text(payload, "response_id")
        if response_id:
            details.append(f"Slack response id: {response_id}")
        status = self._payload_text(payload, "status")
        if status:
            details.append(f"Delivery status: {status}")
        return details

    def _build_execution_artifacts(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        payload = normalized_output.payload
        target = self._payload_text(payload, "channel") or self._payload_text(payload, "target") or "unknown"
        return [f"slack:{invocation.action}:{target}"]

    def _build_next_actions(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        del invocation
        if normalized_output.success:
            return []
        message = " ".join(
            [
                normalized_output.summary or "",
                normalized_output.error or "",
            ]
        ).lower()
        if "channel" in message:
            return ["Retry with a concrete Slack channel id or channel name like #general."]
        if "user id" in message or "dm" in message:
            return ["Retry with a concrete Slack user id or DM channel id."]
        return ["Inspect the Slack target and message text, then retry the outbound request."]

    def _payload_text(self, payload: dict[str, object], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _infer_channels(self, goal: str) -> list[str]:
        lowered = goal.lower()
        channels: list[str] = []
        if "slack" in lowered:
            channels.append("slack")
        if "telegram" in lowered:
            channels.append("telegram")
        if "email" in lowered:
            channels.append("email")
        return channels or ["slack", "email"]
