"""Fast-path handling for lightweight assistant actions."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re
from time import perf_counter
from uuid import uuid4

from agents.browser_agent import BrowserAgent
from agents.communications_agent import CommunicationsAgent
from agents.personal_ops_agent import PersonalOpsAgent
from agents.scheduling_agent import SchedulingPersonalOpsAgent
from app.config import settings
from core.browser_requests import extract_obvious_browser_request
from core.invocation_builders import BrowserToolInvocationBuilder, FileToolInvocationBuilder
from core.interaction_context import get_interaction_context
from core.logging import get_logger
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ChatResponse,
    ExecutionEscalation,
    FileEvidence,
    RequestMode,
    SubTask,
    Task,
    TaskOutcome,
    TaskStatus,
    ToolEvidence,
)
from core.operator_context import OperatorContextService, operator_context
from core.personal_ops_intent import looks_like_personal_ops_request
from core.request_trace import current_request_trace
from integrations.calendar.parsing import (
    CalendarEventDraft,
    parse_calendar_event_reference,
)
from integrations.calendar.service import CalendarService
from integrations.openrouter_client import OpenRouterClient
from memory.contacts import is_email_address, parse_explicit_contact_statement
from integrations.reminders.parsing import parse_one_time_reminder_request_with_fallback
from integrations.reminders.recurring import parse_recurring_reminder_request
from integrations.reminders.service import ReminderSchedulerService
from integrations.tasks.service import GoogleTasksService
from tools.file_tool import FileToolResult
from tools.registry import ToolRegistry, build_default_tool_registry


class _NoOpenRouterClient:
    def is_configured(self) -> bool:
        return False

    def prompt(self, *args, **kwargs) -> str:
        raise AssertionError("Fast browser lanes should not call OpenRouter.")


class FastActionHandler:
    """Handle clearly bounded assistant actions without the heavy execution loop."""

    def __init__(
        self,
        *,
        operator_context_service: OperatorContextService | None = None,
        reminder_service: ReminderSchedulerService | None = None,
        openrouter_client: OpenRouterClient | None = None,
        calendar_service: CalendarService | None = None,
        tasks_service: GoogleTasksService | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.operator_context = operator_context_service or operator_context
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.reminder_service = reminder_service or ReminderSchedulerService(
            runtime_settings=settings,
            memory_store_instance=self.operator_context.memory_store,
        )
        self.calendar_service = calendar_service or CalendarService(runtime_settings=settings)
        self.tasks_service = tasks_service or GoogleTasksService(runtime_settings=settings)
        self.scheduling_agent = SchedulingPersonalOpsAgent(
            calendar_service=self.calendar_service,
            tasks_service=self.tasks_service,
        )
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.file_builder = FileToolInvocationBuilder()
        self.browser_builder = BrowserToolInvocationBuilder()
        self.browser_agent = BrowserAgent(
            tool_registry=self.tool_registry,
            openrouter_client=_NoOpenRouterClient(),
        )
        self.communications_agent = CommunicationsAgent(
            tool_registry=self.tool_registry,
            operator_context_service=self.operator_context,
        )
        self.personal_ops_agent = PersonalOpsAgent(
            scheduling_agent=self.scheduling_agent,
            communications_agent=self.communications_agent,
            personal_store=self.operator_context.personal_ops_store,
            operator_context_service=self.operator_context,
        )

    def handle(self, user_message: str, decision: AssistantDecision) -> ChatResponse | None:
        if decision.mode != RequestMode.ACT:
            return None
        pending_response = self._handle_short_term_state(user_message, decision)
        if pending_response is not None:
            return pending_response
        personal_ops_response = self._handle_personal_ops_request(user_message, decision)
        if personal_ops_response is not None:
            return personal_ops_response
        browser_response = self._handle_browser_request(user_message, decision)
        if browser_response is not None:
            return browser_response
        file_response = self._handle_file_operation(user_message, decision)
        if file_response is not None:
            return file_response
        if self._looks_like_cancel_reminder_request(user_message):
            return self._handle_cancel_reminder(user_message, decision)
        if self._looks_like_update_reminder_request(user_message):
            return self._handle_update_reminder(user_message, decision)
        if self._looks_like_unavailable_email_request(user_message):
            return self._handle_gmail_request(user_message, decision)
        if self._looks_like_gmail_request(user_message):
            return self._handle_gmail_request(user_message, decision)
        if self._looks_like_google_tasks_request(user_message):
            return self._handle_google_tasks_request(user_message, decision)
        if self._looks_like_calendar_read_request(user_message):
            return self._handle_calendar_read(user_message, decision)
        if self._looks_like_calendar_event_request(user_message):
            return self._handle_calendar_event(user_message, decision)
        if self._looks_like_calendar_delete_request(user_message):
            return self._handle_calendar_delete(user_message, decision)
        if self._looks_like_calendar_update_request(user_message):
            return self._handle_calendar_update(user_message, decision)
        if self._is_simple_reminder_request(user_message):
            return self._handle_reminder(user_message, decision)
        return None

    def should_hide_progress(self, user_message: str, decision: AssistantDecision) -> bool:
        return decision.mode == RequestMode.ANSWER or (
            decision.mode == RequestMode.ACT
            and (
                self.can_handle_local_file_request(user_message)
                or self.can_handle_browser_request(user_message)
                or self._looks_like_personal_ops_request(user_message)
                or self._is_simple_reminder_request(user_message)
                or self._looks_like_cancel_reminder_request(user_message)
                or self._looks_like_update_reminder_request(user_message)
                or self._looks_like_calendar_event_request(user_message)
                or self._looks_like_calendar_read_request(user_message)
                or self._looks_like_calendar_delete_request(user_message)
                or self._looks_like_calendar_update_request(user_message)
                or self._looks_like_google_tasks_request(user_message)
                or self._looks_like_unavailable_email_request(user_message)
                or self._looks_like_gmail_request(user_message)
                or self._looks_like_referent_scheduling_action(user_message)
            )
        )

    def can_handle_local_file_request(self, user_message: str) -> bool:
        if self.can_handle_browser_request(user_message):
            return False
        if self._looks_like_browser_file_objective(user_message):
            return False
        return self.file_builder.can_build(user_message)

    def can_handle_browser_request(self, user_message: str) -> bool:
        if self._looks_like_browser_file_objective(user_message):
            return False
        return extract_obvious_browser_request(user_message) is not None

    def _looks_like_browser_file_objective(self, user_message: str) -> bool:
        lowered = " ".join(user_message.lower().split())
        if extract_obvious_browser_request(lowered) is None:
            return False
        if not any(term in lowered for term in ("save", "write", "create", "put")):
            return False
        return any(term in lowered for term in (".txt", ".md", ".json", "file", "summary"))

    def _looks_like_personal_ops_request(self, user_message: str) -> bool:
        return looks_like_personal_ops_request(user_message) or self.personal_ops_agent.can_handle_message(user_message)

    def _handle_short_term_state(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        confirmation_response = self._handle_pending_gmail_confirmation(user_message, decision)
        if confirmation_response is not None:
            return confirmation_response
        confirmation_response = self._handle_pending_reminder_confirmation(user_message, decision)
        if confirmation_response is not None:
            return confirmation_response
        confirmation_response = self._handle_pending_calendar_confirmation(user_message, decision)
        if confirmation_response is not None:
            return confirmation_response

        state = self.operator_context.get_short_term_state()
        if state.pending_question is not None:
            self.operator_context.resume_pending_question_if_answer(user_message)
            state = self.operator_context.get_short_term_state()

        slot_response = self._attempt_pending_slot_merge(user_message, decision)
        if slot_response is not None:
            return slot_response

        return self._attempt_referent_action(user_message, decision)

    def _attempt_pending_slot_merge(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        state = self.operator_context.get_short_term_state()
        pending_action = state.pending_action if isinstance(state.pending_action, dict) else None
        if pending_action and pending_action.get("kind") == "resumed_pending_question":
            original_intent = str(pending_action.get("original_user_intent") or state.original_user_intent or "")
            answer = str(pending_action.get("answer") or user_message)
            missing_field = str(pending_action.get("missing_field") or "")
            resume_target = str(pending_action.get("resume_target") or state.resume_target or "")
        elif state.pending_question is not None:
            pending = state.pending_question
            if not self.operator_context._looks_like_answer_to_pending_question(user_message, pending):
                return None
            original_intent = pending.original_user_intent
            answer = user_message
            missing_field = pending.missing_field
            resume_target = pending.resume_target or state.resume_target or ""
        else:
            return None

        if resume_target == "reminder" or missing_field.startswith("reminder") or missing_field.startswith("recurring_reminder"):
            return self._merge_pending_reminder_slots(
                original_intent=original_intent,
                answer=answer,
                missing_field=missing_field,
                decision=decision,
            )
        if resume_target == "calendar_delete" or missing_field == "calendar_event_id":
            return self._merge_pending_calendar_delete_slots(
                original_intent=original_intent,
                answer=answer,
                decision=decision,
            )
        if resume_target == "calendar_update" or missing_field.startswith("calendar_update"):
            return self._merge_pending_calendar_update_slots(
                original_intent=original_intent,
                answer=answer,
                missing_field=missing_field,
                decision=decision,
            )
        if resume_target == "calendar_create" or missing_field == "calendar_event_details":
            for candidate in self._calendar_create_merge_candidates(original_intent, answer):
                if self.scheduling_agent.interpret_calendar_create(candidate) is None:
                    continue
                response = self._handle_calendar_event(candidate, decision)
                if response.status == TaskStatus.COMPLETED:
                    self.operator_context.consume_short_term_state()
                return response
            combined = f"{original_intent.rstrip()} {answer.strip()}".strip()
            response = self._handle_calendar_event(combined, decision)
            if response.status == TaskStatus.COMPLETED:
                self.operator_context.consume_short_term_state()
            return response
        if resume_target == "google_tasks_complete" or missing_field == "google_task_id":
            response = self._handle_google_tasks_request(f"complete {answer.strip()}", decision)
            if response.status == TaskStatus.COMPLETED:
                self.operator_context.consume_short_term_state()
            return response
        if resume_target == "gmail_recipient" or missing_field == "gmail_recipient":
            return self._merge_pending_gmail_recipient(
                original_intent=original_intent,
                answer=answer,
                decision=decision,
            )
        if resume_target == "browser_continuation" or missing_field == "browser_human_step":
            if answer.lower().strip() not in {"continue", "done", "ready", "try again", "retry"}:
                return None
            response = self._handle_browser_request(original_intent, decision)
            if response is not None and response.status == TaskStatus.COMPLETED:
                self.operator_context.consume_short_term_state()
            return response
        return None

    def _merge_pending_gmail_recipient(
        self,
        *,
        original_intent: str,
        answer: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        recipient = " ".join(answer.strip().split()).strip(" .'\"")
        if not recipient:
            return None
        if not is_email_address(recipient):
            question = "Which email address should I use?"
            self.operator_context.set_pending_question(
                original_user_intent=original_intent,
                missing_field="gmail_recipient",
                expected_answer_type="text",
                resume_target="gmail_recipient",
                tool_or_agent="communications_agent",
                question=question,
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=question,
                results=[
                    AgentResult(
                        subtask_id="fast-action-gmail-recipient",
                        agent="communications_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary=question,
                        tool_name="gmail",
                        blockers=["The supplied recipient was not an email address."],
                    )
                ],
                trace_path="communications_gmail_recipient_merge",
            )
        alias = self.communications_agent._extract_recipient(original_intent) or ""
        if alias and not is_email_address(alias):
            try:
                self.operator_context.personal_ops_store.upsert_contact(alias=alias, email=recipient)
            except (AttributeError, ValueError):
                pass
        combined = re.sub(
            r"\bto\s+([^,]+?)(?=\s+saying|\s+that|\s+with subject|$)",
            f"to {recipient}",
            original_intent,
            count=1,
            flags=re.IGNORECASE,
        )
        if combined == original_intent:
            combined = f"{original_intent.rstrip()} to {recipient}"
        return self._handle_gmail_request(combined, decision)

    def _merge_pending_reminder_slots(
        self,
        *,
        original_intent: str,
        answer: str,
        missing_field: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        combined_candidates = self._reminder_merge_candidates(original_intent, answer, missing_field)
        for candidate in combined_candidates:
            recurring = parse_recurring_reminder_request(candidate, timezone_name=settings.scheduler_timezone)
            if recurring is not None and recurring.schedule is not None and not recurring.follow_up_question:
                summary = recurring.summary or self._default_recurring_reminder_summary(original_intent)
                executable = re.sub(
                    r"\s+(?:to|that|about)\s*$",
                    "",
                    candidate.strip(),
                    flags=re.IGNORECASE,
                )
                if recurring.summary is None:
                    executable = f"{executable} to {summary}"
                response = self._handle_reminder(executable, decision)
                if response.status == TaskStatus.COMPLETED:
                    self.operator_context.consume_short_term_state()
                return response

            one_time = parse_one_time_reminder_request_with_fallback(
                candidate,
                timezone_name=settings.scheduler_timezone,
                openrouter_client=self.openrouter_client,
            )
            if one_time.parsed is not None:
                response = self._handle_reminder(candidate, decision)
                if response.status == TaskStatus.COMPLETED:
                    self.operator_context.consume_short_term_state()
                return response

        question = self._next_reminder_slot_question(original_intent, answer, missing_field)
        self.operator_context.set_pending_question(
            original_user_intent=original_intent,
            missing_field="reminder_time_or_summary",
            expected_answer_type="datetime_or_text",
            resume_target="reminder",
            tool_or_agent="scheduling_agent",
            question=question,
            supplied_slots={missing_field: answer},
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=question,
            results=[
                AgentResult(
                    subtask_id="fast-action-reminder-continuation",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Reminder continuation still needs another slot.",
                    tool_name="reminder_scheduler",
                    blockers=["The merged reminder payload is still incomplete."],
                )
            ],
            trace_path="reminder_slot_merge",
        )

    def _merge_pending_calendar_delete_slots(
        self,
        *,
        original_intent: str,
        answer: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        event_id = parse_calendar_event_reference(answer) or self._extract_bare_identifier(answer)
        if event_id is None:
            referents = self.operator_context.resolve_recent_referents(
                object_type="calendar_event",
                pronoun_text=answer,
            )
            if len(referents) == 1:
                event_id = referents[0].object_id
        target_summary = self._calendar_target_summary(event_id)
        if not event_id:
            question = "Which calendar event should I delete?"
            self.operator_context.set_pending_question(
                original_user_intent=original_intent,
                missing_field="calendar_event_id",
                expected_answer_type="text",
                resume_target="calendar_delete",
                tool_or_agent="scheduling_agent",
                question=question,
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=question,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-delete",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar delete continuation still needs an event id.",
                        tool_name="google_calendar",
                        blockers=["No event id or unambiguous calendar referent was supplied."],
                    )
                ],
            )
        self.operator_context.set_pending_confirmation(
            "calendar_action",
            {
                "action": "delete",
                "event_id": event_id,
                "summary": target_summary,
                "send_updates": self._mentions_invites_or_updates(original_intent),
            },
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"Please confirm: delete {target_summary}?",
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar-delete-confirm",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Calendar delete requires confirmation after slot merge.",
                    tool_name="google_calendar",
                    blockers=["Deleting calendar events requires user confirmation."],
                )
            ],
            trace_path="calendar_slot_merge",
        )

    def _merge_pending_calendar_update_slots(
        self,
        *,
        original_intent: str,
        answer: str,
        missing_field: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        event_id = parse_calendar_event_reference(original_intent)
        if event_id is None and missing_field == "calendar_event_id":
            event_id = parse_calendar_event_reference(answer) or self._extract_bare_identifier(answer)
        if event_id is None:
            question = "Which calendar event should I update?"
            self.operator_context.set_pending_question(
                original_user_intent=original_intent,
                missing_field="calendar_event_id",
                expected_answer_type="text",
                resume_target="calendar_update",
                tool_or_agent="scheduling_agent",
                question=question,
                supplied_slots={missing_field: answer},
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=question,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar update continuation still needs an event id.",
                        tool_name="google_calendar",
                        blockers=["No event id was supplied."],
                    )
                ],
            )
        update_text = answer
        original_day = re.search(
            r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+\w+|this\s+\w+)\b",
            original_intent,
            flags=re.IGNORECASE,
        )
        if original_day and self._looks_like_time_answer(answer):
            update_text = f"{original_day.group(1)} at {answer}"
        if missing_field == "calendar_event_id":
            question = "What should I update about that event?"
            self.operator_context.set_pending_question(
                original_user_intent=f"update event {event_id}",
                missing_field="calendar_update_fields",
                expected_answer_type="text",
                resume_target="calendar_update",
                tool_or_agent="scheduling_agent",
                question=question,
                supplied_slots={"calendar_event_id": event_id},
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=question,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar update now has an event id and needs update fields.",
                        tool_name="google_calendar",
                        blockers=["No update fields were supplied yet."],
                    )
                ],
            )
        update_draft = self.scheduling_agent.interpret_calendar_update(
            f"update event {event_id} {update_text}",
            self.operator_context,
        ).update
        if update_draft is None:
            update_draft = self.scheduling_agent.interpret_calendar_update(
                f"move event {event_id} to {update_text}",
                self.operator_context,
            ).update
        if update_draft is None:
            question = "I'm not sure what you want to change about the event. What should I update?"
            self.operator_context.set_pending_question(
                original_user_intent=f"update event {event_id}",
                missing_field="calendar_update_fields",
                expected_answer_type="text",
                resume_target="calendar_update",
                tool_or_agent="scheduling_agent",
                question=question,
                supplied_slots={"calendar_event_id": event_id},
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=question,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar update continuation still needs concrete fields.",
                        tool_name="google_calendar",
                        blockers=["No concrete calendar update fields were parsed."],
                    )
                ],
            )
        self.operator_context.set_pending_confirmation(
            "calendar_action",
            {
                "action": "update",
                "event_id": update_draft.event_id,
                "updates": update_draft.updates,
                "summary": self._calendar_target_summary(update_draft.event_id),
                "send_updates": self._mentions_invites_or_updates(original_intent),
            },
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"Please confirm: update {self._calendar_target_summary(update_draft.event_id)} {update_draft.description}?",
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar-update-confirm",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Calendar update requires confirmation after slot merge.",
                    tool_name="google_calendar",
                    blockers=["Modifying calendar events requires user confirmation."],
                )
            ],
            trace_path="calendar_slot_merge",
        )

    def _attempt_referent_action(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        lowered = user_message.lower()
        if not self._contains_referent_phrase(lowered):
            return None
        if any(word in lowered for word in ("mark", "complete", "finish", "done")):
            task_referents = self.operator_context.resolve_recent_referents(
                object_type="google_task",
                pronoun_text=user_message,
            )
            if task_referents:
                return self._handle_google_tasks_request(user_message, decision)
        if any(word in lowered for word in ("cancel", "delete", "remove")):
            if "reminder" in lowered or self.operator_context.resolve_recent_referents(
                object_type="reminder",
                pronoun_text=user_message,
            ):
                referents = self.operator_context.resolve_recent_referents(
                    object_type="reminder",
                    pronoun_text=user_message,
                )
                if len(referents) > 1:
                    return self._ambiguous_referent_response(referents, decision, action="cancel")
                if len(referents) == 1 and referents[0].object_id:
                    return self._handle_cancel_reminder(f"cancel {referents[0].summary} reminder", decision)
            calendar_referents = self.operator_context.resolve_recent_referents(
                object_type="calendar_event",
                pronoun_text=user_message,
            )
            if "event" in lowered or "calendar" in lowered or calendar_referents:
                referents = self.operator_context.resolve_recent_referents(
                    object_type="calendar_event",
                    pronoun_text=user_message,
                )
                if len(referents) > 1:
                    return self._ambiguous_referent_response(referents, decision, action="delete")
                if len(referents) == 1 and referents[0].object_id:
                    return self._handle_calendar_delete(f"delete calendar event {referents[0].object_id}", decision)
        if any(word in lowered for word in ("move", "update", "change", "reschedule", "make")):
            calendar_referents = self.operator_context.resolve_recent_referents(
                object_type="calendar_event",
                pronoun_text=user_message,
            )
            if len(calendar_referents) > 1:
                return self._ambiguous_referent_response(calendar_referents, decision, action="update")
            if len(calendar_referents) == 1 and calendar_referents[0].object_id:
                schedule = self.scheduling_agent.extract_schedule_after_update(user_message)
                if schedule:
                    return self._handle_calendar_update(
                        f"move event {calendar_referents[0].object_id} to {schedule}",
                        decision,
                    )
                question = "What day or time should I move that event to?"
                self.operator_context.set_pending_question(
                    original_user_intent=f"update event {calendar_referents[0].object_id}",
                    missing_field="calendar_update_fields",
                    expected_answer_type="datetime_or_text",
                    resume_target="calendar_update",
                    tool_or_agent="scheduling_agent",
                    question=question,
                    supplied_slots={"calendar_event_id": calendar_referents[0].object_id},
                )
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message=question,
                    results=[
                        AgentResult(
                            subtask_id="fast-action-calendar-update",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Calendar referent update needs a concrete new time.",
                            tool_name="google_calendar",
                            blockers=["No concrete calendar update fields were supplied."],
                        )
                    ],
                    trace_path="referent_resolution",
                )
            referents = self.operator_context.resolve_recent_referents(
                object_type="reminder",
                pronoun_text=user_message,
            )
            if len(referents) == 1 and referents[0].object_id:
                schedule = self.scheduling_agent.extract_schedule_after_update(user_message)
                if schedule:
                    return self._handle_update_reminder(
                        f"change {referents[0].summary} reminder to {schedule}",
                        decision,
                    )
        return None

    def _handle_file_operation(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        if not self.can_handle_local_file_request(user_message):
            return None

        started_at = perf_counter()
        invocation = self.file_builder.build(user_message).invocation
        self._carry_forward_recent_file_extension(user_message, invocation)
        expected_requested_path = invocation.parameters.get("path", ".")
        self.logger.info("FAST_FILE_OP_START action=%s path=%r", invocation.action, expected_requested_path)
        self.logger.info("TOOL_SELECTED agent=fast_action tool=file_tool action=%s", invocation.action)
        raw_result = self.tool_registry.execute(invocation)
        result = FileToolResult.model_validate(raw_result)
        verification_notes: list[str] = []
        verified = result.success

        if result.success and invocation.action == "write":
            verify_result = FileToolResult.model_validate(
                self.tool_registry.execute(
                    invocation.model_copy(update={"action": "read", "parameters": {"path": expected_requested_path}})
                )
            )
            verified = verify_result.success and result.normalized_path == verify_result.normalized_path
            verification_notes.append(
                "Verified file exists at the requested normalized path."
                if verified
                else "File write verification failed at the requested normalized path."
            )
        elif result.success and invocation.action == "read":
            verification_notes.append("Verified file content was returned from the requested path.")
        elif result.success and invocation.action == "list":
            verification_notes.append("Verified directory listing returned entries from the requested path.")

        status = TaskStatus.COMPLETED if verified else TaskStatus.BLOCKED
        latency_ms = int((perf_counter() - started_at) * 1000)
        self.logger.info(
            "FAST_FILE_OP_END success=%s action=%s latency_ms=%s requested=%r normalized=%r actual=%r",
            verified,
            invocation.action,
            latency_ms,
            result.requested_path,
            result.normalized_path,
            result.actual_path,
        )
        message = self._format_file_response_message(result, verified)
        response = self._build_response(
            status=status,
            decision=decision,
            message=message,
            results=[
                AgentResult(
                    subtask_id="fast-action-file",
                    agent="fast_action",
                    status=AgentExecutionStatus.COMPLETED if verified else AgentExecutionStatus.BLOCKED,
                    summary=message,
                    tool_name="file_tool",
                    evidence=[
                        FileEvidence(
                            tool_name="file_tool",
                            operation=result.operation,  # type: ignore[arg-type]
                            requested_path=result.requested_path,
                            normalized_path=result.normalized_path,
                            workspace_root=result.workspace_path,
                            actual_path=result.actual_path or result.file_path,
                            file_path=result.file_path,
                            content_preview=result.content_preview,
                            listed_entries=result.listed_entries,
                            verification_notes=verification_notes,
                        )
                    ],
                    blockers=[] if verified else [result.error or "Workspace file verification failed."],
                    next_actions=[] if verified else ["Retry with a valid workspace path inside the current workspace root."],
                )
            ],
            trace_path="local_file_fast_path",
        )
        return response

    def _carry_forward_recent_file_extension(self, user_message: str, invocation: object) -> None:
        if getattr(invocation, "action", None) != "write":
            return
        parameters = getattr(invocation, "parameters", None)
        if not isinstance(parameters, dict):
            return
        raw_path = parameters.get("path")
        if not isinstance(raw_path, str) or Path(raw_path).suffix:
            return
        lowered = user_message.lower()
        if not re.search(r"\b(?:one|another|same kind|called|named)\b", lowered):
            return
        recent_file = self.operator_context.resolve_recent_referent(
            object_type="file_output",
            pronoun_text=user_message,
        )
        if recent_file is None:
            return
        suffix = Path(recent_file.object_id or recent_file.summary).suffix
        if suffix:
            parameters["path"] = f"{raw_path}{suffix}"

    def _handle_personal_ops_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        if not self._looks_like_personal_ops_request(user_message):
            return None
        result = self.personal_ops_agent.handle_message(user_message)
        status = TaskStatus.COMPLETED if result.status == AgentExecutionStatus.COMPLETED else TaskStatus.BLOCKED
        return self._build_response(
            status=status,
            decision=decision,
            message=result.summary,
            results=[result],
            trace_path="personal_ops_fast_path",
        )

    def _handle_browser_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        if not self.can_handle_browser_request(user_message):
            return None

        started_at = perf_counter()
        built = self.browser_builder.build(user_message)
        task = Task(
            goal=user_message,
            title=user_message[:80],
            description=user_message,
            status=TaskStatus.RUNNING,
            request_mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
        )
        subtask = SubTask(
            title=built.execution_title,
            description=built.execution_description,
            objective=built.execution_objective,
            assigned_agent="browser_agent",
            tool_invocation=built.invocation,
        )
        self.logger.info(
            "FAST_BROWSER_OP_START action=%s url=%r",
            built.invocation.action,
            built.invocation.parameters.get("url"),
        )
        self.logger.info("TOOL_SELECTED agent=browser_agent tool=browser_tool action=%s", built.invocation.action)
        result = self.browser_agent.run(task, subtask)
        if result.status == AgentExecutionStatus.BLOCKED and self._needs_human_browser_continuation(result):
            self.operator_context.set_pending_question(
                original_user_intent=user_message,
                missing_field="browser_human_step",
                expected_answer_type="text",
                resume_target="browser_continuation",
                tool_or_agent="browser_agent",
                question="Finish the login, CAPTCHA, or verification step yourself, then say continue and I will retry the inspection.",
            )
        latency_ms = int((perf_counter() - started_at) * 1000)
        self.logger.info(
            "FAST_BROWSER_OP_END success=%s latency_ms=%s url=%r",
            result.status == AgentExecutionStatus.COMPLETED,
            latency_ms,
            built.invocation.parameters.get("url"),
        )
        return self._build_response(
            status=TaskStatus.COMPLETED if result.status == AgentExecutionStatus.COMPLETED else TaskStatus.BLOCKED,
            decision=decision,
            message=result.summary,
            results=[result],
            trace_path="browser_fast_path",
        )

    def _needs_human_browser_continuation(self, result: AgentResult) -> bool:
        combined = " ".join(
            part
            for part in [
                result.summary,
                *result.blockers,
                *result.next_actions,
            ]
            if part
        ).lower()
        return any(
            token in combined
            for token in (
                "login",
                "log in",
                "sign in",
                "captcha",
                "human verification",
                "2fa",
                "verification code",
                "one-time code",
            )
        )

    def _handle_reminder(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        interaction = get_interaction_context()
        if interaction is None or not interaction.channel_id:
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=(
                    "I can schedule that once I'm running in a live Slack conversation, because I need a real delivery target for the reminder."
                ),
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Reminder scheduling needs a live delivery target.",
                        tool_name="reminder_scheduler",
                        blockers=["No Slack delivery context was available for this reminder request."],
                        next_actions=["Send the reminder request from Slack so I know where to deliver it."],
                    )
                ],
            )

        recurring = parse_recurring_reminder_request(
            user_message,
            timezone_name=settings.scheduler_timezone,
        )
        if recurring is not None:
            if recurring.follow_up_question:
                self.operator_context.set_pending_question(
                    original_user_intent=user_message,
                    missing_field="recurring_reminder_time",
                    expected_answer_type="time",
                    resume_target="reminder",
                    tool_or_agent="scheduling_agent",
                    question=recurring.follow_up_question,
                )
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message=recurring.follow_up_question,
                    results=[
                        AgentResult(
                            subtask_id="fast-action-reminder",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Recurring reminder needs a clearer time.",
                            tool_name="reminder_scheduler",
                            blockers=["The recurring reminder pattern did not include a specific time."],
                            next_actions=["Ask the user for the exact recurring reminder time."],
                        )
                    ],
                )
            if recurring.schedule is None or recurring.summary is None:
                blocker = recurring.failure_reason or "I couldn't confidently parse that recurring reminder."
                if recurring.schedule is not None and recurring.schedule.requires_time():
                    question = (
                        "Sure, what time in the morning?"
                        if recurring.schedule.part_of_day == "morning"
                        else "Sure, what time should I use?"
                    )
                    self.operator_context.set_pending_question(
                        original_user_intent=user_message,
                        missing_field="recurring_reminder_time",
                        expected_answer_type="time",
                        resume_target="reminder",
                        tool_or_agent="scheduling_agent",
                        question=question,
                        missing_slots={"recurring_reminder_time": None},
                        supplied_slots={"summary": self._default_recurring_reminder_summary(user_message)},
                    )
                    return self._build_response(
                        status=TaskStatus.BLOCKED,
                        decision=decision,
                        message=question,
                        results=[
                            AgentResult(
                                subtask_id="fast-action-reminder",
                                agent="scheduling_agent",
                                status=AgentExecutionStatus.BLOCKED,
                                summary="Recurring reminder needs a clearer time.",
                                tool_name="reminder_scheduler",
                                blockers=["The recurring reminder pattern did not include a specific time."],
                            )
                        ],
                    )
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message=f"I couldn't set that recurring reminder yet because {self._lowercase_first(blocker).rstrip('.')}.",
                    results=[
                        AgentResult(
                            subtask_id="fast-action-reminder",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Recurring reminder parsing failed on the fast path.",
                            tool_name="reminder_scheduler",
                            blockers=[blocker],
                        )
                    ],
                )

            success, _summary, record, blockers = self.reminder_service.schedule_recurring_reminder(
                summary=recurring.summary,
                schedule=recurring.schedule,
                channel_id=interaction.channel_id,
                user_id=interaction.user_id,
                source="fast_action",
                metadata={
                    "source": "fast_action",
                    "channel_id": interaction.channel_id,
                    "user_id": interaction.user_id or "",
                    "schedule_phrase": recurring.schedule.describe(),
                    "delivery_text": f"Reminder: {recurring.summary}",
                },
            )
            if not success or record is None:
                blocker = blockers[0] if blockers else "The recurring reminder service is not ready in this runtime."
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message=f"I couldn't set that recurring reminder yet because {self._lowercase_first(blocker).rstrip('.')}.",
                    results=[
                        AgentResult(
                            subtask_id="fast-action-reminder",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Recurring reminder scheduling failed on the fast path.",
                            tool_name="reminder_scheduler",
                            blockers=blockers or [blocker],
                        )
                    ],
            )

            description = recurring.schedule.describe()
            self.operator_context.register_actionable_object(
                object_type="reminder",
                object_id=record.reminder_id,
                summary=recurring.summary,
                source="reminder_scheduler",
                confidence=0.95,
            )
            return self._build_response(
                status=TaskStatus.COMPLETED,
                decision=decision,
                message=f"I set that for {description} to {recurring.summary}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary=f"Scheduled a recurring reminder for {description} to {recurring.summary}.",
                        tool_name="reminder_scheduler",
                        evidence=[
                            ToolEvidence(
                                tool_name="reminder_scheduler",
                                summary="Scheduled a recurring reminder on the fast path.",
                                payload={
                                    "reminder_id": record.reminder_id,
                                    "summary": recurring.summary,
                                    "recurrence_description": description,
                                    "deliver_at": record.deliver_at,
                                    "channel_id": interaction.channel_id,
                                },
                            )
                        ],
                    )
                ],
            )

        one_time_message = self._canonicalize_one_time_reminder_message(user_message)
        parse_outcome = parse_one_time_reminder_request_with_fallback(
            one_time_message,
            timezone_name=settings.scheduler_timezone,
            openrouter_client=self.openrouter_client,
        )
        parsed = parse_outcome.parsed
        if parsed is None:
            blocker = parse_outcome.failure_reason or (
                "I couldn't confidently parse the time and reminder details from that request."
            )
            blocker_sentence = blocker.strip().rstrip(".")
            if blocker_sentence.startswith("I "):
                message = f"I couldn't schedule that reminder yet. {blocker_sentence}."
            else:
                message = (
                    "I couldn't schedule that reminder yet because "
                    f"{self._lowercase_first(blocker_sentence)}."
                )
            self.operator_context.set_pending_question(
                original_user_intent=user_message,
                missing_field="reminder_time_or_summary",
                expected_answer_type="datetime_or_text",
                resume_target="reminder",
                tool_or_agent="scheduling_agent",
                question=message,
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=message,
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Reminder scheduling failed on the fast path.",
                        tool_name="reminder_scheduler",
                        blockers=[blocker],
                        next_actions=[
                            "Try a one-time format like 'remind me in 10 minutes to stretch' or 'remind me at 6 PM to call mom'.",
                        ],
                    )
                ],
            )

        success, _summary, record, blockers = self.reminder_service.schedule_one_time_reminder(
            summary=parsed.summary,
            deliver_at=parsed.deliver_at,
            channel_id=interaction.channel_id,
            user_id=interaction.user_id,
            source="fast_action",
            metadata={
                "source": "fast_action",
                "channel_id": interaction.channel_id,
                "user_id": interaction.user_id or "",
                "schedule_phrase": parsed.schedule_phrase,
                "delivery_text": f"Reminder: {parsed.summary}",
            },
        )
        if not success or record is None:
            blocker = blockers[0] if blockers else "The reminder service is not ready in this runtime."
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=f"I couldn't schedule that reminder yet because {self._lowercase_first(blocker).rstrip('.')}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Reminder scheduling failed on the fast path.",
                        tool_name="reminder_scheduler",
                        blockers=blockers or [blocker],
                    )
                ],
            )

        scheduled_for = self._format_delivery_time(parsed.deliver_at)
        self.operator_context.register_actionable_object(
            object_type="reminder",
            object_id=record.reminder_id,
            summary=parsed.summary,
            source="reminder_scheduler",
            confidence=0.95,
        )
        return self._build_response(
            status=TaskStatus.COMPLETED,
            decision=decision,
            message=f"I'll remind you {scheduled_for} to {parsed.summary}.",
            results=[
                AgentResult(
                    subtask_id="fast-action-reminder",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Scheduled a reminder for {scheduled_for} to {parsed.summary}.",
                    tool_name="reminder_scheduler",
                    evidence=[
                        ToolEvidence(
                            tool_name="reminder_scheduler",
                            summary="Scheduled a one-time reminder on the fast path.",
                            payload={
                                "reminder_id": record.reminder_id,
                                "summary": parsed.summary,
                                "deliver_at": parsed.deliver_at.isoformat(),
                                "channel_id": interaction.channel_id,
                                "parser": parsed.parser,
                                "confidence": parsed.confidence,
                            },
                        )
                    ],
                )
            ],
        )

    def _handle_cancel_reminder(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        lowered = user_message.lower()
        recurring_only = "recurring" in lowered or "daily" in lowered
        target = self._extract_reminder_target(user_message)
        referent = self.operator_context.resolve_recent_referent(
            object_type="reminder",
            pronoun_text=user_message,
        )
        matches = []
        if self._is_pronoun_target(target) and referent is not None and referent.object_id:
            reminder = self.reminder_service.memory_store.get_reminder(referent.object_id)
            matches = [reminder] if reminder is not None else []
        if not matches:
            matches = self.reminder_service.find_matching_reminders(
                target,
                recurring_only=recurring_only,
            )
        if not matches:
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message="I couldn't find a matching active reminder to cancel.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder-cancel",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="No active reminder matched the cancel request.",
                        tool_name="reminder_scheduler",
                        blockers=["No active reminder matched that description."],
                    )
                ],
            )
        if len(matches) > 1:
            return self._ambiguous_reminder_response(matches, decision, action="cancel")
        reminder = matches[0]
        description = self._describe_reminder_record(reminder)
        self.operator_context.register_actionable_object(
            object_type="reminder",
            object_id=reminder.reminder_id,
            summary=reminder.summary,
            source="reminder_scheduler",
            confidence=0.95,
        )
        self.operator_context.set_pending_confirmation(
            "reminder_action",
            {
                "action": "cancel",
                "reminder_id": reminder.reminder_id,
                "summary": reminder.summary,
                "description": description,
            },
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"Please confirm: cancel {description}?",
            results=[
                AgentResult(
                    subtask_id="fast-action-reminder-cancel-confirm",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Reminder cancellation requires confirmation.",
                    tool_name="reminder_scheduler",
                    blockers=["Canceling reminders requires user confirmation."],
                )
            ],
            trace_path="scheduling_confirmation",
        )

    def _handle_update_reminder(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        parsed = self._parse_reminder_update_request(user_message)
        if parsed is None:
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message="I couldn't tell which reminder to update or what new schedule you wanted.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Reminder update parsing failed.",
                        tool_name="reminder_scheduler",
                        blockers=["The reminder update request was too ambiguous."],
                    )
                ],
            )
        target, schedule_text = parsed
        matches = self.reminder_service.find_matching_reminders(target)
        if not matches:
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message="I couldn't find a matching active reminder to update.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="No active reminder matched the update request.",
                        tool_name="reminder_scheduler",
                        blockers=["No active reminder matched that description."],
                    )
                ],
            )
        if len(matches) > 1:
            return self._ambiguous_reminder_response(matches, decision, action="update")
        reminder = matches[0]
        can_update = False
        recurring = parse_recurring_reminder_request(
            f"remind me {schedule_text} to {reminder.summary}",
            timezone_name=settings.scheduler_timezone,
        )
        if recurring is not None and recurring.schedule is not None and not recurring.follow_up_question:
            can_update = True
        else:
            one_time = parse_one_time_reminder_request_with_fallback(
                f"remind me {schedule_text} to {reminder.summary}",
                timezone_name=settings.scheduler_timezone,
                openrouter_client=self.openrouter_client,
            )
            if one_time.parsed is not None:
                can_update = True
        if not can_update:
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message="I couldn't update that reminder with the new schedule yet.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Reminder rescheduling failed.",
                        tool_name="reminder_scheduler",
                        blockers=["The new reminder schedule could not be parsed or applied."],
                    )
                ],
            )
        self.operator_context.set_pending_confirmation(
            "reminder_action",
            {
                "action": "update",
                "reminder_id": reminder.reminder_id,
                "summary": reminder.summary,
                "schedule_text": schedule_text,
            },
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"Please confirm: update your reminder to {reminder.summary} to {schedule_text}?",
            results=[
                AgentResult(
                    subtask_id="fast-action-reminder-update-confirm",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Reminder update requires confirmation.",
                    tool_name="reminder_scheduler",
                    blockers=["Updating reminders requires user confirmation."],
                )
            ],
            trace_path="scheduling_confirmation",
        )

    def _handle_google_tasks_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        readiness_response = self._tasks_readiness_response(decision)
        if readiness_response is not None:
            return readiness_response
        interpretation = self.scheduling_agent.interpret_task_request(user_message, self.operator_context)
        if interpretation.action == "list":
            result = self.scheduling_agent.list_tasks(
                due_on=interpretation.due if interpretation.due_label == "today" else None
            )
            return self._google_tasks_result_response(
                result,
                decision,
                action="list",
                due_label=interpretation.due_label,
            )
        if interpretation.action == "create" and interpretation.title:
            result = self.scheduling_agent.create_task(
                title=interpretation.title,
                due=interpretation.due,
            )
            return self._google_tasks_result_response(
                result,
                decision,
                action="create",
                due_label=interpretation.due_label,
            )
        if interpretation.action == "complete":
            target = interpretation.target
            if target is not None and target.ambiguous:
                return self._ambiguous_referent_response(target.matches, decision, action="mark done")
            if target is None or target.task_id is None:
                message = interpretation.question or "Which task should I mark done?"
                self.operator_context.set_pending_question(
                    original_user_intent=user_message,
                    missing_field="google_task_id",
                    expected_answer_type="text",
                    resume_target="google_tasks_complete",
                    tool_or_agent="scheduling_agent",
                    question=message,
                )
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message=message,
                    results=[
                        AgentResult(
                            subtask_id="fast-action-google-tasks",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Task completion needs a clearer task target.",
                            tool_name="google_tasks",
                            blockers=["No unambiguous recent Google Task was resolved."],
                        )
                    ],
                    trace_path="scheduling_tasks",
                )
            result = self.scheduling_agent.complete_task(
                task_id=target.task_id,
                task_list_id=target.task_list_id,
            )
            return self._google_tasks_result_response(
                result,
                decision,
                action="complete",
                target_summary=target.summary,
            )
        message = interpretation.question or "I need a clearer task action."
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=message,
            results=[
                AgentResult(
                    subtask_id="fast-action-google-tasks",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Task request could not be interpreted.",
                    tool_name="google_tasks",
                    blockers=[message],
                )
            ],
            trace_path="scheduling_tasks",
        )

    def _google_tasks_result_response(
        self,
        result,
        decision: AssistantDecision,
        *,
        action: str,
        due_label: str | None = None,
        target_summary: str | None = None,
    ) -> ChatResponse:
        if not result.success:
            blocker = result.blockers[0] if result.blockers else "Google Tasks access is not ready here."
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=f"I'm blocked because {self._humanize_blocker(blocker)}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-google-tasks",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary=result.summary,
                        tool_name="google_tasks",
                        blockers=list(result.blockers),
                    )
                ],
                trace_path="scheduling_tasks",
            )
        evidence = [
            ToolEvidence(
                tool_name="google_tasks",
                summary="Used Google Tasks through the Scheduling / Personal Ops Agent.",
                payload={
                    "source": "google_tasks",
                    "tasks": [self._google_task_payload(item) for item in result.tasks],
                    "created_task_id": result.created_task.task_id if result.created_task else None,
                    "completed_task_id": result.completed_task.task_id if result.completed_task else None,
                },
            )
        ]
        if action == "list":
            message = self._format_google_tasks_list(result.tasks, due_label=due_label)
            summary = f"Loaded {len(result.tasks)} Google Task(s)."
        elif action == "create":
            task = result.created_task
            title = task.title if task is not None else "that task"
            due_text = self._format_task_due_suffix(task.due if task is not None else None)
            message = f"I added {title} to your tasks{due_text}."
            summary = f"Created Google Task {title}."
        else:
            task = result.completed_task
            title = task.title if task is not None else target_summary or "that task"
            message = f"Done, I marked {title} complete."
            summary = f"Completed Google Task {title}."
        return self._build_response(
            status=TaskStatus.COMPLETED,
            decision=decision,
            message=message,
            results=[
                AgentResult(
                    subtask_id="fast-action-google-tasks",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary=summary,
                    tool_name="google_tasks",
                    evidence=evidence,
                )
            ],
            trace_path="scheduling_tasks",
        )

    def _handle_calendar_event(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        readiness_response = self._calendar_readiness_response(decision)
        if readiness_response is not None:
            return readiness_response
        draft = self.scheduling_agent.interpret_calendar_create(user_message)
        if draft is None:
            message = self._calendar_create_follow_up(user_message)
            self.operator_context.set_pending_question(
                original_user_intent=user_message,
                missing_field="calendar_event_details",
                expected_answer_type="datetime_or_text",
                resume_target="calendar_create",
                tool_or_agent="scheduling_agent",
                question=message,
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=message,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar event parsing failed.",
                        tool_name="google_calendar",
                        blockers=["The event title or timing was too ambiguous."],
                    )
                ],
            )
        if draft.attendees or self._mentions_invites_or_updates(user_message):
            self.operator_context.set_pending_confirmation(
                "calendar_action",
                {
                    "action": "create",
                    "title": draft.title,
                    "start": draft.start.isoformat(),
                    "end": draft.end.isoformat(),
                    "attendees": list(draft.attendees),
                    "send_updates": self._mentions_invites_or_updates(user_message),
                },
            )
            attendee_text = f" with {len(draft.attendees)} attendee(s)" if draft.attendees else ""
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=(
                    f"Please confirm: create calendar event {draft.title} for "
                    f"{self._format_delivery_time(draft.start)}{attendee_text}?"
                ),
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-confirm",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar create needs confirmation because it includes attendees or invites.",
                        tool_name="google_calendar",
                        blockers=["Calendar events with attendees or invites require user confirmation before creation."],
                    )
                ],
                trace_path="scheduling_confirmation",
            )
        result = self.scheduling_agent.create_calendar_event(draft)
        if not result.success:
            blocker = result.blockers[0] if result.blockers else "Calendar access is not ready here."
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=f"I'm blocked because {self._humanize_blocker(blocker)}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary=result.summary,
                        tool_name="google_calendar",
                        blockers=list(result.blockers),
                    )
                ],
            )
        assert result.created_event is not None
        created = result.created_event
        when = created.start.astimezone().strftime("%B %d at %I:%M %p").replace(" 0", " ").lstrip("0")
        self.operator_context.register_actionable_object(
            object_type="calendar_event",
            object_id=created.event_id,
            summary=self._calendar_event_summary(created),
            source="google_calendar",
            confidence=0.95,
            metadata={
                "start": created.start.isoformat(),
                "end": created.end.isoformat(),
                "title": created.title,
                "calendar_id": created.calendar_id,
            },
        )
        return self._build_response(
            status=TaskStatus.COMPLETED,
            decision=decision,
            message=f"I added {created.title} for {when}.",
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Created calendar event {created.title}.",
                    tool_name="google_calendar",
                    evidence=[
                        ToolEvidence(
                            tool_name="google_calendar",
                            summary="Created a calendar event.",
                            payload={
                                "event_id": created.event_id,
                                "calendar_id": created.calendar_id,
                                "title": created.title,
                                "start": created.start.isoformat(),
                                "end": created.end.isoformat(),
                                "timezone": created.timezone,
                                "location": created.location,
                                "description_snippet": created.description_snippet,
                                "attendees_count": created.attendees_count,
                                "htmlLink": created.html_link,
                                "source": created.source,
                            },
                        )
                    ],
                )
            ],
        )

    def _handle_calendar_read(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        query = self.scheduling_agent.interpret_calendar_query(user_message)
        result = self.scheduling_agent.read_calendar(user_message)
        if result.status == AgentExecutionStatus.BLOCKED:
            blocker = result.blockers[0] if result.blockers else "Calendar access is not ready here."
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=(
                    "Google Calendar setup is needed before I can use your calendar. "
                    f"{self._humanize_blocker(blocker)}."
                ),
                results=[result],
                trace_path="scheduling_calendar_read",
            )
        events = []
        if result.evidence:
            payload = getattr(result.evidence[0], "payload", {}) or {}
            events = list(payload.get("events", [])) if isinstance(payload, dict) else []
            range_label = str(payload.get("range_label") or "").strip() if isinstance(payload, dict) else ""
        else:
            range_label = ""
        label = range_label or (query.label if query is not None else "that range")
        if query is not None and query.window_start is not None:
            original_events = list(events)
            events = [
                event
                for event in events
                if self._event_overlaps_window(event, query.window_start, query.window_end)
            ]
            if not events and query.mode == "availability":
                events = [
                    event
                    for event in original_events
                    if self._event_overlaps_time_of_day(event, query.window_start, query.window_end)
                ]
        if query is not None and query.mode == "next" and events:
            events = events[:1]
        if not events:
            if query is not None and query.mode == "availability":
                message = f"I don't see anything on your calendar {label}."
            else:
                message = f"Your calendar is clear {label}."
        else:
            lines = []
            for event in events[:5]:
                start = datetime.fromisoformat(str(event["start"])).astimezone()
                time_text = start.strftime("%I:%M %p").lstrip("0")
                location = str(event.get("location") or "").strip()
                location_text = f" at {location}" if location else ""
                lines.append(f"{time_text}: {event['title']}{location_text}")
            if query is not None and query.mode == "next":
                message = f"Your next calendar event is {lines[0]}."
            elif query is not None and query.mode == "availability":
                message = f"You have something on your calendar {label}: " + "; ".join(lines)
            else:
                message = f"Here's what's on your calendar {label}: " + "; ".join(lines)
        return self._build_response(
            status=TaskStatus.COMPLETED,
            decision=decision,
            message=message,
            results=[result],
            trace_path="scheduling_calendar_read",
        )

    def _handle_calendar_delete(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        readiness_response = self._calendar_readiness_response(decision)
        if readiness_response is not None:
            return readiness_response
        target = self.scheduling_agent.resolve_calendar_target(user_message, self.operator_context)
        event_id = target.event_id
        target_summary = target.summary
        if target.ambiguous:
            return self._ambiguous_referent_response(target.matches, decision, action="delete")
        if event_id is None:
            clarification = "Which calendar event should I cancel?"
            self.operator_context.set_pending_question(
                original_user_intent=user_message,
                missing_field="calendar_event_id",
                expected_answer_type="text",
                resume_target="calendar_delete",
                tool_or_agent="scheduling_agent",
                question=clarification,
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=clarification,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-delete",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar delete request needs an event id.",
                        tool_name="google_calendar",
                        blockers=["No Google Calendar event id was provided."],
                    )
                ],
            )
        self.operator_context.set_pending_confirmation(
            "calendar_action",
            {
                "action": "delete",
                "event_id": event_id,
                "summary": target_summary,
                "send_updates": self._mentions_invites_or_updates(user_message),
            },
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"Please confirm: delete {target_summary}?",
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar-delete-confirm",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Calendar delete requires confirmation.",
                    tool_name="google_calendar",
                    blockers=["Deleting calendar events requires user confirmation."],
                )
            ],
            trace_path="scheduling_confirmation",
        )

    def _handle_calendar_update(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        readiness_response = self._calendar_readiness_response(decision)
        if readiness_response is not None:
            return readiness_response
        interpretation = self.scheduling_agent.interpret_calendar_update(user_message, self.operator_context)
        update_draft = interpretation.update
        if update_draft is None:
            if interpretation.target.ambiguous:
                return self._ambiguous_referent_response(interpretation.target.matches, decision, action="update")
            if interpretation.target.event_id is None:
                message = interpretation.question or "Which calendar event should I update?"
                blocker = "No Google Calendar event or recent event referent was provided."
                missing_field = "calendar_event_id"
                expected_answer_type = "text"
            else:
                message = interpretation.question or "I'm not sure what you want to change about the event. What should I update?"
                blocker = "No concrete calendar update fields were parsed from the request."
                missing_field = "calendar_update_fields"
                expected_answer_type = "time" if "time" in message.lower() else "text"
            self.operator_context.set_pending_question(
                original_user_intent=user_message,
                missing_field=missing_field,
                expected_answer_type=expected_answer_type,
                resume_target="calendar_update",
                tool_or_agent="scheduling_agent",
                question=message,
            )
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=message,
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary="Calendar update request needs a concrete event id and update payload.",
                        tool_name="google_calendar",
                        blockers=[blocker],
                    )
                ],
            )
        self.operator_context.set_pending_confirmation(
            "calendar_action",
            {
                "action": "update",
                "event_id": update_draft.event_id,
                "updates": update_draft.updates,
                "summary": interpretation.target.summary or self._calendar_target_summary(update_draft.event_id),
                "send_updates": self._mentions_invites_or_updates(user_message),
            },
        )
        target_summary = interpretation.target.summary or self._calendar_target_summary(update_draft.event_id)
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"Please confirm: update {target_summary} {update_draft.description}?",
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar-update-confirm",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Calendar update requires confirmation.",
                    tool_name="google_calendar",
                    blockers=["Modifying calendar events requires user confirmation."],
                )
            ],
            trace_path="scheduling_confirmation",
        )

    def _handle_pending_calendar_confirmation(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        if user_message.lower().strip() not in {"yes", "confirm", "confirmed", "do it", "go ahead"}:
            return None
        pending = self.operator_context.pop_pending_confirmation("calendar_action")
        if not pending:
            return None
        readiness_response = self._calendar_readiness_response(decision)
        if readiness_response is not None:
            return readiness_response
        action = str(pending.get("action") or "")
        if action == "create":
            start = datetime.fromisoformat(str(pending["start"]))
            end = datetime.fromisoformat(str(pending["end"]))
            draft = CalendarEventDraft(
                title=str(pending["title"]),
                start=start,
                end=end,
                attendees=tuple(str(item) for item in pending.get("attendees", [])),
            )
            result = self.scheduling_agent.create_calendar_event(
                draft,
                send_updates=bool(pending.get("send_updates")),
            )
            response = self._calendar_write_result_response(result, decision, action="created")
            if response.status == TaskStatus.COMPLETED:
                self.operator_context.consume_short_term_state()
            return response
        if action == "delete":
            event_id = str(pending["event_id"])
            target_summary = str(pending.get("summary") or "that event")
            result = self.scheduling_agent.delete_calendar_event(
                event_id=event_id,
                send_updates=bool(pending.get("send_updates")),
            )
            if not result.success:
                return self._calendar_write_result_response(result, decision, action="deleted")
            response = self._build_response(
                status=TaskStatus.COMPLETED,
                decision=decision,
                message=f"I deleted {target_summary}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-delete",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary=f"Deleted calendar event {event_id}.",
                        tool_name="google_calendar",
                    )
                ],
            )
            self.operator_context.consume_short_term_state()
            return response
        if action == "update":
            event_id = str(pending["event_id"])
            updates = dict(pending.get("updates", {}))
            if not updates:
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message="I'm not sure what you want to change about the event. What should I update?",
                    results=[
                        AgentResult(
                            subtask_id="fast-action-calendar-update",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Calendar update confirmation had no concrete update payload.",
                            tool_name="google_calendar",
                            blockers=["No calendar update fields were present in the pending confirmation."],
                        )
                    ],
                    trace_path="scheduling_confirmation",
                )
            result = self.scheduling_agent.update_calendar_event(
                event_id=event_id,
                updates=updates,
                send_updates=bool(pending.get("send_updates")),
            )
            response = self._calendar_write_result_response(result, decision, action="updated")
            if response.status == TaskStatus.COMPLETED:
                self.operator_context.consume_short_term_state()
            return response
        return None

    def _calendar_write_result_response(
        self,
        result,
        decision: AssistantDecision,
        *,
        action: str,
    ) -> ChatResponse:
        if not result.success:
            blocker = result.blockers[0] if result.blockers else "Calendar access is not ready here."
            return self._build_response(
                status=TaskStatus.BLOCKED,
                decision=decision,
                message=f"I'm blocked because {self._humanize_blocker(blocker)}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-write",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.BLOCKED,
                        summary=result.summary,
                        tool_name="google_calendar",
                        blockers=list(result.blockers),
                    )
                ],
            )
        event = result.created_event or result.updated_event or (result.events[0] if result.events else None)
        if event is None:
            return self._build_response(
                status=TaskStatus.COMPLETED,
                decision=decision,
                message=f"I {action} that calendar event.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-calendar-write",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary=f"Calendar event {action}.",
                        tool_name="google_calendar",
                    )
                ],
            )
        when = event.start.astimezone().strftime("%B %d at %I:%M %p").replace(" 0", " ").lstrip("0")
        self.operator_context.register_actionable_object(
            object_type="calendar_event",
            object_id=event.event_id,
            summary=self._calendar_event_summary(event),
            source="google_calendar",
            confidence=0.95,
            metadata={
                "start": event.start.isoformat(),
                "end": event.end.isoformat(),
                "title": event.title,
                "calendar_id": event.calendar_id,
            },
        )
        return self._build_response(
            status=TaskStatus.COMPLETED,
            decision=decision,
            message=f"I {action} {event.title} for {when}.",
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar-write",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary=f"Calendar event {action}: {event.title}.",
                    tool_name="google_calendar",
                )
            ],
        )

    def _build_response(
        self,
        *,
        status: TaskStatus,
        decision: AssistantDecision,
        message: str,
        results: list[AgentResult],
        trace_path: str = "fast_action",
    ) -> ChatResponse:
        trace = current_request_trace()
        if trace is not None:
            trace.set_path(trace_path)
        self._register_actionable_objects_from_results(results)
        self.operator_context.record_assistant_reply(message)
        self.logger.info("FINAL_VERIFICATION_STATUS status=%s", status.value)
        return ChatResponse(
            task_id=f"fast-action-{uuid4()}",
            status=status,
            planner_mode="fast_action",
            request_mode=decision.mode,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
            response=message,
            outcome=TaskOutcome(
                completed=sum(1 for result in results if result.status == AgentExecutionStatus.COMPLETED),
                blocked=sum(1 for result in results if result.status == AgentExecutionStatus.BLOCKED),
                total_subtasks=len(results),
            ),
            subtasks=[],
            results=results,
        )

    def _handle_unavailable_email(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse:
        del user_message
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=(
                "Email sending is not connected yet. I need an email provider, credentials, "
                "a from address, and a confirmation path before I can send email."
            ),
            results=[
                AgentResult(
                    subtask_id="fast-action-email-unavailable",
                    agent="communications_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Email delivery is unavailable in the current runtime.",
                    tool_name="email_delivery",
                    blockers=[
                        "Email provider execution is not implemented yet.",
                        "EMAIL_PROVIDER, EMAIL_API_KEY, EMAIL_FROM_ADDRESS, and EMAIL_ENABLED must be ready before sending.",
                    ],
                    next_actions=[
                        "Configure and implement the email provider adapter before retrying outbound email.",
                    ],
                )
            ],
            trace_path="communications_unavailable_fast_path",
        )

    def _handle_gmail_request(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        result = self.communications_agent.handle_message(user_message)
        message = self._humanize_communications_message(result.summary)
        return self._build_response(
            status=TaskStatus.COMPLETED if result.status == AgentExecutionStatus.COMPLETED else TaskStatus.BLOCKED,
            decision=decision,
            message=message,
            results=[result],
            trace_path="communications_gmail_fast_path",
        )

    def _handle_pending_gmail_confirmation(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        if user_message.lower().strip() not in {"yes", "confirm", "confirmed", "do it", "go ahead"}:
            return None
        if not self.operator_context.get_pending_confirmation("gmail_action"):
            return None
        result = self.communications_agent.handle_message(user_message)
        response = self._build_response(
            status=TaskStatus.COMPLETED if result.status == AgentExecutionStatus.COMPLETED else TaskStatus.BLOCKED,
            decision=decision,
            message=result.summary,
            results=[result],
            trace_path="communications_confirmation",
        )
        if response.status == TaskStatus.COMPLETED:
            self.operator_context.consume_short_term_state()
        return response

    def _handle_pending_reminder_confirmation(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> ChatResponse | None:
        if user_message.lower().strip() not in {"yes", "confirm", "confirmed", "do it", "go ahead"}:
            return None
        pending = self.operator_context.pop_pending_confirmation("reminder_action")
        if not pending:
            return None
        action = str(pending.get("action") or "")
        reminder_id = str(pending.get("reminder_id") or "")
        if action == "cancel" and reminder_id:
            canceled = self.reminder_service.cancel_reminder(
                reminder_id,
                reason="Canceled by user confirmation.",
            )
            if canceled is None:
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message="I couldn't find that active reminder anymore.",
                    results=[
                        AgentResult(
                            subtask_id="fast-action-reminder-cancel",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Confirmed reminder cancellation could not find an active reminder.",
                            tool_name="reminder_scheduler",
                            blockers=["The reminder was not active when confirmation arrived."],
                        )
                    ],
                    trace_path="scheduling_confirmation",
                )
            description = self._describe_reminder_record(canceled)
            response = self._build_response(
                status=TaskStatus.COMPLETED,
                decision=decision,
                message=f"I canceled {description}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder-cancel",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary=f"Canceled reminder {canceled.reminder_id}.",
                        tool_name="reminder_scheduler",
                        evidence=[
                            ToolEvidence(
                                tool_name="reminder_scheduler",
                                summary="Canceled a reminder after confirmation.",
                                payload={
                                    "reminder_id": canceled.reminder_id,
                                    "summary": canceled.summary,
                                },
                            )
                        ],
                    )
                ],
                trace_path="scheduling_confirmation",
            )
            self.operator_context.consume_short_term_state()
            return response
        if action == "update" and reminder_id:
            updated = self._apply_reminder_update(
                reminder_id=reminder_id,
                schedule_text=str(pending.get("schedule_text") or ""),
                summary=str(pending.get("summary") or "that reminder"),
            )
            if updated is None:
                return self._build_response(
                    status=TaskStatus.BLOCKED,
                    decision=decision,
                    message="I couldn't update that reminder with the new schedule yet.",
                    results=[
                        AgentResult(
                            subtask_id="fast-action-reminder-update",
                            agent="scheduling_agent",
                            status=AgentExecutionStatus.BLOCKED,
                            summary="Confirmed reminder update could not be applied.",
                            tool_name="reminder_scheduler",
                            blockers=["The new reminder schedule could not be parsed or applied."],
                        )
                    ],
                    trace_path="scheduling_confirmation",
                )
            response = self._build_response(
                status=TaskStatus.COMPLETED,
                decision=decision,
                message=f"I updated that reminder to {self._describe_reminder_record(updated)}.",
                results=[
                    AgentResult(
                        subtask_id="fast-action-reminder-update",
                        agent="scheduling_agent",
                        status=AgentExecutionStatus.COMPLETED,
                        summary=f"Updated reminder {updated.reminder_id}.",
                        tool_name="reminder_scheduler",
                    )
                ],
                trace_path="scheduling_confirmation",
            )
            self.operator_context.consume_short_term_state()
            return response
        return None

    def _calendar_readiness_response(self, decision: AssistantDecision) -> ChatResponse | None:
        blockers = self.calendar_service.readiness_blockers()
        if not blockers:
            return None
        blocker_text = " ".join(self._humanize_blocker(blocker).rstrip(".") + "." for blocker in blockers if blocker.strip())
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=(
                "Google Calendar setup is needed before I can use your calendar. "
                f"{blocker_text} Complete the local Google OAuth sign-in once."
            ).strip(),
            results=[
                AgentResult(
                    subtask_id="fast-action-calendar-readiness",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Google Calendar is not ready for live use.",
                    tool_name="google_calendar",
                    blockers=blockers,
                    next_actions=["Complete Google Calendar OAuth setup and make sure the token file exists."],
                )
            ],
            trace_path="scheduling_readiness_fast_path",
        )

    def _tasks_readiness_response(self, decision: AssistantDecision) -> ChatResponse | None:
        blockers = self.tasks_service.readiness_blockers()
        if not blockers:
            return None
        blocker_text = " ".join(self._humanize_blocker(blocker).rstrip(".") + "." for blocker in blockers if blocker.strip())
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=(
                "Google Tasks setup is needed before I can use your tasks. "
                f"{blocker_text}"
            ).strip(),
            results=[
                AgentResult(
                    subtask_id="fast-action-google-tasks-readiness",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Google Tasks is not ready for live use.",
                    tool_name="google_tasks",
                    blockers=blockers,
                    next_actions=["Connect Google Tasks and make sure saved Google Tasks access exists."],
                )
            ],
            trace_path="scheduling_tasks_readiness",
        )

    def _is_simple_reminder_request(self, message: str) -> bool:
        lowered = message.lower()
        if self.scheduling_agent.is_calendar_read_request(lowered):
            return False
        reminder_markers = (
            "remind me",
            "set a reminder",
            "schedule a reminder",
        )
        return any(marker in lowered for marker in reminder_markers)

    def _looks_like_calendar_read_request(self, message: str) -> bool:
        return self.scheduling_agent.is_calendar_read_request(message)

    def _looks_like_google_tasks_request(self, message: str) -> bool:
        return self.scheduling_agent.is_task_request(message, self.operator_context)

    def _format_delivery_time(self, deliver_at: datetime) -> str:
        local_time = deliver_at.astimezone()
        now = datetime.now(local_time.tzinfo)
        if local_time.date() == now.date():
            return f"at {local_time.strftime('%I:%M %p').lstrip('0')}"
        return f"on {local_time.strftime('%B %d at %I:%M %p').replace(' 0', ' ').lstrip('0')}"

    def _lowercase_first(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return stripped
        return stripped[0].lower() + stripped[1:]

    def _humanize_blocker(self, text: str) -> str:
        cleaned = self._lowercase_first(" ".join(text.strip().rstrip(".").split()))
        replacements = {
            "GOOGLE_CALENDAR_TOKEN_PATH": "saved Google Calendar access",
            "GOOGLE_CALENDAR_CREDENTIALS_PATH": "Google Calendar credentials",
            "GOOGLE_CALENDAR_ENABLED is false": "Google Calendar is not enabled",
            "Google Calendar dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib": "the Google Calendar Python packages are not installed",
            "GOOGLE_TASKS_TOKEN_PATH": "saved Google Tasks access",
            "GOOGLE_TASKS_CREDENTIALS_PATH": "Google Tasks credentials",
            "GOOGLE_TASKS_ENABLED is false": "Google Tasks is not enabled",
            "Google Tasks dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib": "the Google Tasks Python packages are not installed",
            "oauth": "Google sign-in",
            "token file": "saved calendar access",
            "adapter": "connection",
            "runtime": "workspace",
            "missing config": "missing setup",
        }
        for source, target in replacements.items():
            cleaned = re.sub(source, target, cleaned, flags=re.IGNORECASE)
        return cleaned.rstrip(".") or "one more setup step is needed"

    def _humanize_communications_message(self, text: str) -> str:
        cleaned = " ".join(text.strip().split())
        if "gmail/email is not live" not in cleaned.lower():
            return cleaned
        return (
            "Gmail setup is needed before I can use your mailbox. "
            "I need Gmail enabled, OAuth credentials, and saved Gmail access before I can draft, read, or send email."
        )

    def _looks_like_cancel_reminder_request(self, message: str) -> bool:
        lowered = message.lower()
        if "reminder" in lowered and any(word in lowered for word in ("cancel", "delete", "remove")):
            return True
        if any(word in lowered for word in ("cancel", "delete", "remove")) and any(
            token in lowered for token in (" it", " that", " last one", " this")
        ):
            return self.operator_context.resolve_recent_referent(
                object_type="reminder",
                pronoun_text=message,
            ) is not None
        return False

    def _looks_like_update_reminder_request(self, message: str) -> bool:
        lowered = message.lower()
        return "reminder" in lowered and any(word in lowered for word in ("change", "move", "update", "reschedule"))

    def _looks_like_calendar_event_request(self, message: str) -> bool:
        return self.scheduling_agent.is_calendar_create_request(message)

    def _looks_like_calendar_delete_request(self, message: str) -> bool:
        if self.scheduling_agent.is_calendar_delete_request(message):
            return True
        lowered = message.lower()
        if "reminder" in lowered or not any(word in lowered for word in ("delete", "remove", "cancel")):
            return False
        target = self.scheduling_agent.resolve_calendar_target(message, self.operator_context)
        return bool(target.event_id or target.ambiguous)

    def _looks_like_calendar_update_request(self, message: str) -> bool:
        if self.scheduling_agent.is_calendar_update_request(message):
            return True
        lowered = message.lower()
        if "reminder" in lowered or not any(word in lowered for word in ("move", "update", "change", "reschedule")):
            return False
        target = self.scheduling_agent.resolve_calendar_target(message, self.operator_context)
        return bool(target.event_id or target.ambiguous)

    def _looks_like_unavailable_email_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        if parse_explicit_contact_statement(message) is not None:
            return False
        if lowered.startswith("message ") and " saying " in lowered:
            return True
        if "email" not in lowered:
            return False
        return any(word in lowered for word in ("send", "email", "draft", "reply", "forward"))

    def _looks_like_gmail_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        if parse_explicit_contact_statement(message) is not None:
            return False
        if any(token in lowered for token in ("calendar", "reminder")):
            return False
        if lowered.startswith("message ") and " saying " in lowered:
            return True
        return any(token in lowered for token in ("gmail", "email", "emails", "mailbox", "newsletter", "newsletters", "inbox")) and any(
            word in lowered
            for word in (
                "what",
                "which",
                "do",
                "have",
                "any",
                "summarize",
                "summary",
                "find",
                "search",
                "read",
                "draft",
                "reply",
                "send",
                "delete",
                "trash",
                "archive",
                "forward",
                "unread",
                "today",
            )
        )

    def _mentions_invites_or_updates(self, message: str) -> bool:
        lowered = message.lower()
        return any(token in lowered for token in ("invite", "attendee", "send update", "send updates", "notify guests"))

    def _extract_reminder_target(self, message: str) -> str:
        target = re.sub(
            r"^(?:please\s+)?(?:cancel|delete|remove)\s+(?:my\s+)?",
            "",
            message.strip(),
            flags=re.IGNORECASE,
        )
        target = re.sub(r"\s+reminder$", "", target, flags=re.IGNORECASE).strip(" .")
        target = re.sub(r"\s+please$", "", target, flags=re.IGNORECASE).strip(" .")
        return target or "reminder"

    def _is_pronoun_target(self, target: str) -> bool:
        return " ".join(target.lower().split()) in {"it", "that", "the last one", "last one", "this"}

    def _contains_referent_phrase(self, lowered: str) -> bool:
        padded = f" {lowered} "
        return any(
            token in padded
            for token in (
                " it ",
                " that ",
                " this ",
                " the last one ",
                " last one ",
                " first ",
                " second ",
                " third ",
                " 1st ",
                " 2nd ",
                " 3rd ",
            )
        )

    def _reminder_merge_candidates(self, original_intent: str, answer: str, missing_field: str) -> list[str]:
        original = original_intent.strip()
        answer_text = answer.strip()
        candidates: list[str] = []
        if not original or not answer_text:
            return candidates
        if missing_field == "recurring_reminder_time" or "every" in original.lower():
            summary = self._extract_reminder_summary_for_merge(original) or self._default_recurring_reminder_summary(original)
            candidates.append(self._insert_time_into_recurring_reminder(original, answer_text, summary=summary))
        summary = self._extract_reminder_summary_for_merge(original)
        if self._looks_like_time_answer(answer_text) and summary:
            candidates.append(f"remind me at {answer_text} to {summary}")
        if self._looks_like_time_answer(answer_text) and not summary:
            candidates.append(f"{original} at {answer_text} to {self._default_recurring_reminder_summary(original)}")
        if not self._looks_like_time_answer(answer_text):
            time_text = self._extract_time_text(original)
            if time_text:
                candidates.append(f"remind me at {time_text} to {answer_text}")
        candidates.append(f"{original} {answer_text}")
        deduped: list[str] = []
        for candidate in candidates:
            normalized = " ".join(candidate.split())
            if normalized and normalized.lower() not in [item.lower() for item in deduped]:
                deduped.append(normalized)
        return deduped

    def _calendar_create_merge_candidates(self, original_intent: str, answer: str) -> list[str]:
        original = " ".join(original_intent.strip().split())
        answer_text = " ".join(answer.strip().split()).strip(" .")
        if not original or not answer_text:
            return []
        candidates = [f"{original} {answer_text}".strip()]
        if self._contains_calendar_day(answer_text):
            candidates.append(
                re.sub(
                    r"\s+(?P<time>(?:at|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
                    lambda match: f" {answer_text} {match.group('time')}",
                    original,
                    count=1,
                    flags=re.IGNORECASE,
                )
            )
        if self._looks_like_time_answer(answer_text) and not re.search(
            r"\b(?:at|from)\s+\d",
            original,
            flags=re.IGNORECASE,
        ):
            candidates.append(f"{original} at {answer_text}")
        deduped: list[str] = []
        for candidate in candidates:
            normalized = " ".join(candidate.split())
            if normalized and normalized.lower() not in [item.lower() for item in deduped]:
                deduped.append(normalized)
        return deduped

    def _contains_calendar_day(self, text: str) -> bool:
        return bool(
            re.search(
                r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+\w+|this\s+\w+)\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _insert_time_into_recurring_reminder(self, original: str, time_text: str, *, summary: str) -> str:
        if re.search(r"\bat\s+\d{1,2}", original, flags=re.IGNORECASE):
            base = original
        else:
            base = re.sub(
                r"\b(every morning|every night|every day|daily|every weekday|every week|weekly|every month|monthly|every (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
                lambda match: f"{match.group(1)} at {time_text}",
                original,
                count=1,
                flags=re.IGNORECASE,
            )
        if not self._extract_reminder_summary_for_merge(base):
            base = f"{base.rstrip()} to {summary}"
        return base

    def _extract_reminder_summary_for_merge(self, message: str) -> str | None:
        match = re.search(r"\b(?:to|that|about)\s+(?P<summary>.+)$", message, flags=re.IGNORECASE)
        if not match:
            return None
        summary = match.group("summary").strip(" .")
        summary = re.sub(
            r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
            "",
            summary,
            flags=re.IGNORECASE,
        ).strip(" .")
        return summary or None

    def _extract_time_text(self, message: str) -> str | None:
        match = re.search(r"\bat\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", message, flags=re.IGNORECASE)
        return match.group("time").strip() if match else None

    def _looks_like_time_answer(self, text: str) -> bool:
        return bool(
            re.search(
                r"\b(today|tomorrow|morning|night|am|pm|\d{1,2}(?::\d{2})?)\b",
                text.lower(),
            )
        )

    def _canonicalize_one_time_reminder_message(self, message: str) -> str:
        match = re.match(
            r"^(?P<prefix>(?:please\s+)?(?:remind me|set a reminder))\s+to\s+(?P<summary>.+?)\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)$",
            message.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return message
        prefix = match.group("prefix")
        time_text = match.group("time").strip()
        summary = match.group("summary").strip()
        return f"{prefix} at {time_text} to {summary}"

    def _default_recurring_reminder_summary(self, original_intent: str) -> str:
        lowered = original_intent.lower()
        if "morning" in lowered:
            return "your morning reminder"
        if "night" in lowered:
            return "your evening reminder"
        return "follow up"

    def _next_reminder_slot_question(self, original_intent: str, answer: str, missing_field: str) -> str:
        del answer, missing_field
        if self._extract_reminder_summary_for_merge(original_intent) is None:
            return "What should the reminder say?"
        return "What time should I use for that reminder?"

    def _extract_bare_identifier(self, message: str) -> str | None:
        cleaned = " ".join(message.strip().split()).strip("`'\" .")
        if re.fullmatch(r"[A-Za-z0-9_\-@.]{2,}", cleaned):
            return cleaned
        return None

    def _extract_schedule_after_referent_update(self, message: str) -> str | None:
        match = re.search(r"\b(?:to|for)\s+(?P<schedule>.+)$", message, flags=re.IGNORECASE)
        if match:
            return match.group("schedule").strip()
        fallback = re.search(
            r"\b(?P<schedule>(?:today|tomorrow|this\s+\w+|next\s+\w+|\w+day)(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            message,
            flags=re.IGNORECASE,
        )
        return fallback.group("schedule").strip() if fallback else None

    def _ambiguous_referent_response(
        self,
        referents,
        decision: AssistantDecision,
        *,
        action: str,
    ) -> ChatResponse:
        options = "; ".join(
            f"{index + 1}. {item.summary}" for index, item in enumerate(referents[:3])
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"I found more than one possible target to {action}: {options}. Which one do you mean?",
            results=[
                AgentResult(
                    subtask_id="fast-action-referent-clarify",
                    agent="fast_action",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Referent resolution was ambiguous.",
                    tool_name="short_term_state",
                    blockers=["More than one recent actionable object matched the referent."],
                )
            ],
            trace_path="referent_resolution",
        )

    def _ambiguous_reminder_response(
        self,
        reminders,
        decision: AssistantDecision,
        *,
        action: str,
    ) -> ChatResponse:
        options = "; ".join(
            f"{index + 1}. {self._describe_reminder_record(item)}" for index, item in enumerate(reminders[:3])
        )
        return self._build_response(
            status=TaskStatus.BLOCKED,
            decision=decision,
            message=f"I found more than one reminder to {action}: {options}. Which one do you mean?",
            results=[
                AgentResult(
                    subtask_id="fast-action-reminder-clarify",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Reminder target matching was ambiguous.",
                    tool_name="reminder_scheduler",
                    blockers=["More than one reminder matched the request."],
                )
            ],
            trace_path="scheduling_clarification",
        )

    def _apply_reminder_update(self, *, reminder_id: str, schedule_text: str, summary: str):
        recurring = parse_recurring_reminder_request(
            f"remind me {schedule_text} to {summary}",
            timezone_name=settings.scheduler_timezone,
        )
        if recurring is not None and recurring.schedule is not None and not recurring.follow_up_question:
            return self.reminder_service.reschedule_reminder(
                reminder_id,
                recurring_schedule=recurring.schedule,
            )
        one_time = parse_one_time_reminder_request_with_fallback(
            f"remind me {schedule_text} to {summary}",
            timezone_name=settings.scheduler_timezone,
            openrouter_client=self.openrouter_client,
        )
        if one_time.parsed is not None:
            return self.reminder_service.reschedule_reminder(
                reminder_id,
                deliver_at=one_time.parsed.deliver_at,
            )
        return None

    def _looks_like_referent_scheduling_action(self, message: str) -> bool:
        lowered = message.lower()
        if not self._contains_referent_phrase(lowered):
            return False
        if not any(word in lowered for word in ("cancel", "delete", "remove", "move", "update", "change", "reschedule", "make")):
            return False
        return bool(
            self.operator_context.resolve_recent_referents(object_type="calendar_event", pronoun_text=message)
            or self.operator_context.resolve_recent_referents(object_type="reminder", pronoun_text=message)
        )

    def _calendar_create_follow_up(self, message: str) -> str:
        lowered = message.lower()
        has_day = bool(re.search(r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+\w+|this\s+\w+)\b", lowered))
        has_time = bool(re.search(r"\b(at|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b|\b(morning|afternoon|evening|tonight)\b", lowered))
        if not has_day and not has_time:
            return "What day and time should I use for that event?"
        if not has_day:
            return "What day should I put that event on?"
        if not has_time:
            return "What time should I use for that event?"
        return "What should I call that event?"

    def _event_overlaps_window(
        self,
        event: dict[str, object],
        window_start: datetime,
        window_end: datetime | None,
    ) -> bool:
        try:
            event_start = datetime.fromisoformat(str(event.get("start"))).astimezone()
            event_end = datetime.fromisoformat(str(event.get("end"))).astimezone()
        except (TypeError, ValueError):
            return False
        start = window_start.astimezone()
        end = (window_end or (window_start + timedelta(hours=1))).astimezone()
        return event_start < end and event_end > start

    def _event_overlaps_time_of_day(
        self,
        event: dict[str, object],
        window_start: datetime,
        window_end: datetime | None,
    ) -> bool:
        try:
            event_start = datetime.fromisoformat(str(event.get("start"))).astimezone()
            event_end = datetime.fromisoformat(str(event.get("end"))).astimezone()
        except (TypeError, ValueError):
            return False
        reference_start = window_start.astimezone()
        reference_end = (window_end or (window_start + timedelta(hours=1))).astimezone()
        event_start_minutes = event_start.hour * 60 + event_start.minute
        event_end_minutes = event_end.hour * 60 + event_end.minute
        window_start_minutes = reference_start.hour * 60 + reference_start.minute
        window_end_minutes = reference_end.hour * 60 + reference_end.minute
        return event_start_minutes < window_end_minutes and event_end_minutes > window_start_minutes

    def _calendar_event_summary(self, event) -> str:
        start = event.start.astimezone().strftime("%I:%M %p").lstrip("0")
        return f"{event.title} at {start}"

    def _format_google_tasks_list(self, tasks, *, due_label: str | None = None) -> str:
        label = " due today" if due_label == "today" else ""
        if not tasks:
            return f"You don't have any tasks{label}."
        lines = []
        for index, task in enumerate(list(tasks)[:5], start=1):
            due_text = self._format_task_due_suffix(getattr(task, "due", None))
            lines.append(f"{index}. {task.title}{due_text}")
        return f"Your tasks{label}: " + "; ".join(lines)

    def _format_task_due_suffix(self, due: datetime | None) -> str:
        if due is None:
            return ""
        today = datetime.now(due.astimezone().tzinfo).date()
        due_date = due.astimezone().date()
        if due_date == today:
            return " due today"
        if due_date == today + timedelta(days=1):
            return " due tomorrow"
        return f" due {due.astimezone().strftime('%B %d').replace(' 0', ' ')}"

    def _google_task_payload(self, task) -> dict[str, object]:
        return {
            "task_id": task.task_id,
            "task_list_id": task.task_list_id,
            "title": task.title,
            "status": task.status,
            "due": task.due.isoformat() if task.due else None,
            "notes": task.notes,
            "updated": task.updated,
            "completed": task.completed,
            "position": task.position,
            "source": task.source,
        }

    def _calendar_target_summary(self, event_id: str | None) -> str:
        if not event_id:
            return "that event"
        state = self.operator_context.get_short_term_state()
        for item in state.last_actionable_objects:
            if item.object_type == "calendar_event" and item.object_id == event_id:
                return item.summary
        return "that event"

    def _register_actionable_objects_from_results(self, results: list[AgentResult]) -> None:
        for result in results:
            for evidence in result.evidence:
                if result.tool_name == "file_tool":
                    file_path = getattr(evidence, "actual_path", None) or getattr(evidence, "file_path", None)
                    if file_path:
                        self.operator_context.register_actionable_object(
                            object_type="file_output",
                            object_id=str(file_path),
                            summary=str(file_path),
                            source="file_tool",
                            confidence=0.8,
                        )
                payload = getattr(evidence, "payload", None)
                if not isinstance(payload, dict):
                    continue
                if result.tool_name == "reminder_scheduler":
                    reminder_id = payload.get("reminder_id")
                    summary = payload.get("summary") or result.summary
                    if reminder_id:
                        self.operator_context.register_actionable_object(
                            object_type="reminder",
                            object_id=str(reminder_id),
                            summary=str(summary),
                            source="reminder_scheduler",
                            confidence=0.9,
                        )
                if result.tool_name == "google_calendar":
                    events = payload.get("events", [])
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        event_id = event.get("event_id")
                        title = event.get("title")
                        if event_id and title:
                            summary = str(title)
                            start_text = str(event.get("start") or "")
                            if start_text:
                                try:
                                    start = datetime.fromisoformat(start_text).astimezone()
                                    summary = f"{summary} at {start.strftime('%I:%M %p').lstrip('0')}"
                                except ValueError:
                                    pass
                            self.operator_context.register_actionable_object(
                                object_type="calendar_event",
                                object_id=str(event_id),
                                summary=summary,
                                source="google_calendar",
                                confidence=0.85,
                                metadata={
                                    "start": str(event.get("start") or ""),
                                    "end": str(event.get("end") or ""),
                                    "title": str(title),
                                    "calendar_id": str(event.get("calendar_id") or "primary"),
                                },
                            )
                if result.tool_name == "google_tasks":
                    tasks = payload.get("tasks", [])
                    if not isinstance(tasks, list):
                        continue
                    for task in reversed(tasks[:5]):
                        if not isinstance(task, dict):
                            continue
                        task_id = task.get("task_id")
                        title = task.get("title")
                        if task_id and title:
                            self.operator_context.register_actionable_object(
                                object_type="google_task",
                                object_id=str(task_id),
                                summary=str(title),
                                source="google_tasks",
                                confidence=0.9,
                                metadata={
                                    "title": str(title),
                                    "task_list_id": str(task.get("task_list_id") or "@default"),
                                    "due": str(task.get("due") or ""),
                                    "status": str(task.get("status") or ""),
                                },
                            )
                if result.tool_name == "gmail":
                    messages = payload.get("messages", [])
                    if isinstance(messages, list):
                        for message in messages[:5]:
                            if not isinstance(message, dict):
                                continue
                            message_id = message.get("message_id") or message.get("id")
                            summary = message.get("subject") or message.get("snippet") or result.summary
                            if message_id and summary:
                                self.operator_context.register_actionable_object(
                                    object_type="gmail_message",
                                    object_id=str(message_id),
                                    summary=str(summary),
                                    source="gmail",
                                    confidence=0.85,
                                )

    def _parse_reminder_update_request(self, message: str) -> tuple[str, str] | None:
        match = re.match(
            r"^(?:please\s+)?(?:change|move|update|reschedule)\s+(?:my\s+)?(?P<target>.+?)\s+reminder\s+(?:to|for)\s+(?P<schedule>.+)$",
            message.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return match.group("target").strip(), match.group("schedule").strip()

    def _describe_reminder_record(self, reminder) -> str:
        if reminder.schedule_kind == "recurring":
            description = reminder.recurrence_description or "its recurring schedule"
            return f"your reminder to {reminder.summary} ({description})"
        deliver_at = datetime.fromisoformat(reminder.deliver_at).astimezone()
        return f"your reminder to {reminder.summary} at {deliver_at.strftime('%I:%M %p').lstrip('0')}"

    def _format_file_response_message(self, result: FileToolResult, verified: bool) -> str:
        if not verified:
            return (
                f"I couldn't complete that file request safely because the requested path "
                f"{result.requested_path!r} did not verify at the expected workspace location."
            )
        if result.operation == "write":
            location = self._display_workspace_path(result.actual_path or result.file_path)
            return f"I wrote that file to {location}."
        if result.operation == "read":
            preview = result.content_preview or "the requested content"
            return f"I read {self._display_workspace_path(result.actual_path or result.file_path)}: {preview}"
        entries = ", ".join(result.listed_entries[:8]) if result.listed_entries else "no entries"
        return f"I listed {self._display_workspace_path(result.actual_path or result.file_path)}: {entries}"

    def _display_workspace_path(self, path: str | None) -> str:
        if not path:
            return "`workspace`"
        candidate = Path(path)
        try:
            relative = candidate.relative_to(Path(settings.workspace_root))
            return f"`{relative.as_posix()}`"
        except ValueError:
            return f"`{candidate.as_posix()}`"
