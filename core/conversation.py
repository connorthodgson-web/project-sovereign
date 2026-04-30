"""Lightweight conversational handling for ANSWER-mode requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import re

import httpx

from agents.catalog import AgentCatalog, build_agent_catalog
from agents.scheduling_agent import SchedulingPersonalOpsAgent, looks_like_calendar_read_request
from app.config import settings
from core.assistant_fast_path import (
    extract_name_value,
    is_explicit_memory_statement,
    is_forget_name_statement,
    is_greeting_message,
    is_memory_follow_up_phrase,
    is_memory_lookup,
    is_name_statement,
    is_project_memory_question,
    is_thanks_message,
    is_user_memory_question,
)
from core.context_assembly import ContextAssembler
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ChatResponse,
    FileEvidence,
    RequestMode,
    Task,
    TaskOutcome,
    TaskStatus,
    ToolEvidence,
)
from core.operator_context import OperatorContextService, RuntimeSnapshot, operator_context
from core.model_routing import ModelRequestContext
from core.request_trace import current_request_trace
from core.state import TaskStateStore, task_state_store
from core.system_context import SOVEREIGN_SYSTEM_CONTEXT
from integrations.calendar.service import CalendarService
from integrations.openrouter_client import OpenRouterClient
from integrations.reminders.recurring import parse_recurring_reminder_request
from integrations.reminders.service import ReminderSchedulerService
from memory.contacts import parse_explicit_contact_statement
from tools.capability_manifest import CapabilityCatalog, build_capability_catalog


@dataclass
class ConversationContext:
    """Lean context bundle for conversational answers."""

    context_profile: str
    recent_tasks: list[Task]
    recent_created_files: list[str]
    workspace_entries: list[str]
    slack_configured: bool
    recent_task_summaries: list[str]
    runtime_snapshot: RuntimeSnapshot


class ConversationalHandler:
    """Generate fast, contextual, assistant-style replies for ANSWER mode."""

    def __init__(
        self,
        *,
        openrouter_client: OpenRouterClient | None = None,
        task_store: TaskStateStore | None = None,
        workspace_root: str | None = None,
        operator_context_service: OperatorContextService | None = None,
        context_assembler: ContextAssembler | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        agent_catalog: AgentCatalog | None = None,
        reminder_service: ReminderSchedulerService | None = None,
        calendar_service: CalendarService | None = None,
    ) -> None:
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.task_store = task_store or task_state_store
        self.workspace_root = Path(workspace_root or settings.workspace_root)
        self.operator_context = operator_context_service or operator_context
        self.agent_catalog = agent_catalog or build_agent_catalog()
        self.capability_catalog = capability_catalog or build_capability_catalog(
            agent_catalog=self.agent_catalog
        )
        self.reminder_service = reminder_service or ReminderSchedulerService(
            runtime_settings=settings,
            memory_store_instance=self.operator_context.memory_store,
        )
        self.calendar_service = calendar_service or CalendarService(runtime_settings=settings)
        self.scheduling_agent = SchedulingPersonalOpsAgent(calendar_service=self.calendar_service)
        self.context_assembler = context_assembler or ContextAssembler(
            operator_context_service=self.operator_context,
            agent_catalog=self.agent_catalog,
            capability_catalog=self.capability_catalog,
        )

    def handle(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        if decision.requires_minimal_follow_up:
            reply = decision.follow_up_prompt or "What do you want me to do with that?"
            trace = current_request_trace()
            if trace is not None:
                trace.set_path("assistant_clarify")
            self.operator_context.set_pending_question(
                original_user_intent=user_message,
                missing_field="clarification",
                expected_answer_type="text",
                resume_target=decision.intent_label or "assistant",
                tool_or_agent=None,
                question=reply,
            )
            self.operator_context.record_assistant_reply(reply)
            return ChatResponse(
                task_id=f"answer-{uuid4()}",
                status=TaskStatus.COMPLETED,
                planner_mode="conversation_clarify",
                request_mode=decision.mode,
                escalation_level=decision.escalation_level,
                response=reply,
                outcome=TaskOutcome(total_subtasks=0),
                subtasks=[],
                results=[],
            )
        quick_reply = self._quick_local_reply_without_context(user_message)
        if quick_reply is not None:
            trace = current_request_trace()
            if trace is not None:
                trace.set_path("assistant_fast_path")
            self.operator_context.record_assistant_reply(quick_reply)
            return ChatResponse(
                task_id=f"answer-{uuid4()}",
                status=TaskStatus.COMPLETED,
                planner_mode="conversation_fast_path",
                request_mode=decision.mode,
                escalation_level=decision.escalation_level,
                response=quick_reply,
                outcome=TaskOutcome(total_subtasks=0),
                subtasks=[],
                results=[],
            )
        quick_memory_reply = self._quick_local_memory_reply_without_context(user_message)
        if quick_memory_reply is not None:
            self.operator_context.record_assistant_reply(quick_memory_reply)
            return ChatResponse(
                task_id=f"answer-{uuid4()}",
                status=TaskStatus.COMPLETED,
                planner_mode="conversation_memory_fast_path",
                request_mode=decision.mode,
                escalation_level=decision.escalation_level,
                response=quick_memory_reply,
                outcome=TaskOutcome(total_subtasks=0),
                subtasks=[],
                results=[],
            )
        context = self._build_context(user_message)
        reply = self._answer_from_fast_local_context(user_message, context)
        if reply is None:
            reply = self._answer_with_llm(user_message, context)
        if reply is None:
            reply = self._answer_deterministically(user_message, context)
        reply = self._apply_response_preferences(reply, context.runtime_snapshot)
        self.operator_context.record_assistant_reply(reply)
        return ChatResponse(
            task_id=f"answer-{uuid4()}",
            status=TaskStatus.COMPLETED,
            planner_mode="conversation",
            request_mode=decision.mode,
            escalation_level=decision.escalation_level,
            response=reply,
            outcome=TaskOutcome(total_subtasks=0),
            subtasks=[],
            results=[],
        )

    def _build_context(self, user_message: str) -> ConversationContext:
        context_profile = self._context_profile_for_message(user_message)
        recent_tasks = self._select_recent_tasks(user_message, context_profile=context_profile)
        return ConversationContext(
            context_profile=context_profile,
            recent_tasks=recent_tasks,
            recent_created_files=self._recent_created_files(recent_tasks),
            workspace_entries=self._workspace_entries(),
            slack_configured=bool(settings.slack_bot_token and settings.slack_app_token),
            recent_task_summaries=self._recent_task_summaries(recent_tasks),
            runtime_snapshot=self.operator_context.build_runtime_snapshot(
                focus_text=user_message,
                context_profile=context_profile,
            ),
        )

    def _answer_with_llm(self, user_message: str, context: ConversationContext) -> str | None:
        if not self.openrouter_client.is_configured():
            return None

        prompt = (
            f"{self.context_assembler.build('conversation', user_message=user_message, context_profile=context.context_profile).to_prompt_block()}\n"
            "Reply as Project Sovereign's conversational assistant.\n"
            "Use the provided context when it helps, but keep the response concise, natural, direct, and Slack-friendly.\n"
            "Do not sound like logs or mention planners, routers, subtasks, or orchestration internals.\n"
            "Use memory as assistant recall, not as debug output. Mention prior context naturally only when it helps.\n"
            "Do not claim runtime facts you cannot verify.\n"
            "Avoid numbered lists unless the user explicitly asked for a list, steps, or options.\n"
            "For greetings, confirmations, capability answers, memory answers, and continuity answers, prefer a short sentence or a short paragraph.\n"
            f"User message: {user_message}\n"
            f"Context: {json.dumps(self._serialize_context(context), ensure_ascii=True)}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are a fast conversational assistant for an AI operator product. "
                    "Answer like a capable personal assistant: clear, concise, calm, and honest."
                ),
                label="conversation_answer",
                context=ModelRequestContext(
                    intent_label="memory" if context.context_profile == "memory" else "assistant",
                    request_mode="answer",
                    selected_lane="assistant",
                    selected_agent="assistant_agent",
                    task_complexity="low",
                    risk_level="low",
                    requires_tool_use=False,
                    requires_review=False,
                    evidence_quality="high" if context.context_profile == "memory" else "unknown",
                    user_visible_latency_sensitivity="high",
                    cost_sensitivity="high",
                ),
            )
            cleaned = response.strip()
            if self._contains_backend_jargon(cleaned):
                return None
            return cleaned or None
        except (RuntimeError, httpx.HTTPError):
            return None

    def _answer_deterministically(self, user_message: str, context: ConversationContext) -> str:
        message = user_message.lower().strip()
        latest_task = context.recent_tasks[0] if context.recent_tasks else None
        runtime = context.runtime_snapshot

        if self._looks_like_social_acknowledgement(message):
            return "You're welcome."
        if self._looks_like_greeting(message):
            return "Hi. What can I help with?"
        if self._looks_like_preference_statement(message):
            return self._acknowledge_preference(message)
        if self._looks_like_simple_math(message):
            return self._evaluate_simple_math(message)
        memory_answer = self._answer_specific_memory_query(user_message, runtime)
        if memory_answer is not None:
            return memory_answer
        if self._looks_like_identity_question(message):
            return self._describe_identity(runtime)
        if self._looks_like_project_identity_question(message):
            return self._describe_project(runtime)
        if self._looks_like_capability_question(message):
            return self._answer_capability_question(message, context, runtime)
        if self._looks_like_active_work_question(message):
            return self._describe_active_work(runtime)
        if self._looks_like_continuity_question(message):
            return self._describe_continuity(runtime)
        if self._looks_like_user_memory_question(message):
            return self._describe_user_memory(runtime)
        if self._looks_like_project_memory_question(message):
            return self._describe_project_memory(runtime)
        if self._looks_like_planning_discussion(message):
            return self._respond_to_planning_discussion(runtime)
        if self._looks_like_next_work_question(message):
            return self._describe_next_work(runtime)
        if message == "continue":
            return self._describe_continue(runtime)
        if self._looks_like_reminder_question(message):
            return self._answer_reminder_question(user_message, runtime)
        if self._looks_like_calendar_question(message):
            return self._answer_calendar_question(user_message)
        if self._looks_like_soft_reminder_request(message):
            return self._handle_reminder_request(user_message, runtime)
        if self._looks_like_last_task_question(message):
            if latest_task is None:
                if runtime.recent_actions:
                    return f"Most recently, I {runtime.recent_actions[0].rstrip('.')}."
                return "I haven't handled a task yet in this session."
            return self._summarize_latest_task(latest_task)
        if "files you created" in message or "what files did you create" in message:
            return self._describe_recent_files(context.recent_created_files)
        if "workspace" in message and ("what" in message or "show" in message):
            if context.workspace_entries:
                entries = ", ".join(f"`{item}`" for item in context.workspace_entries[:8])
                return f"At the top of the workspace I can see {entries}."
            return "The workspace looks empty right now."

        if latest_task is not None:
            return self._fallback_with_recent_context(latest_task)
        return "I'm here. What do you want to work on next?"

    def _quick_local_reply_without_context(self, user_message: str) -> str | None:
        message = user_message.strip()
        normalized = message.lower().strip()
        if is_thanks_message(normalized):
            return "You're welcome."
        if is_greeting_message(normalized):
            return "Hi. What can I help with?"
        if normalized in {"how are you", "how are you?"}:
            return "I'm doing well and ready to help."
        if normalized in {"who are you", "who are you?"}:
            return "I'm Sovereign, your main operator. I handle quick assistant requests directly and coordinate the heavier work when needed."
        if normalized in {"what can you do", "what can you do?"}:
            return self._describe_capabilities()
        if normalized in {"can u make a file or nah", "can you make a file or nah", "can you create files?"}:
            return "Yes. I can create local workspace files when you actually want me to."
        if is_name_statement(message):
            name = extract_name_value(message)
            if name:
                return f"Noted. I'll remember that your name is {name}."
            return "Noted."
        if is_forget_name_statement(message):
            return "Okay. I forgot your name."
        contact = parse_explicit_contact_statement(message)
        if contact is not None:
            return f"Got it. I'll use {contact.alias} for future email drafts and guarded sends."
        if is_explicit_memory_statement(normalized):
            return "Noted. I'll remember that."
        if "manus" in normalized:
            snapshot = self.capability_catalog.snapshot_for("manus_agent")
            if snapshot is None or not snapshot.is_live:
                return "Manus is listed as a future premium agent, but it is not configured or enabled here yet."
        if "browser use" in normalized or "browser-use" in normalized:
            return self._describe_browser_capability()
        return None

    def _answer_from_fast_local_context(
        self,
        user_message: str,
        context: ConversationContext,
    ) -> str | None:
        normalized = user_message.lower().strip()
        runtime = context.runtime_snapshot
        trace = current_request_trace()
        if self._is_memory_follow_up_request(normalized):
            if trace is not None:
                trace.set_path("assistant_memory_fast_path")
            return self._describe_memory_follow_up(normalized, runtime)
        if (
            is_user_memory_question(normalized)
            or is_project_memory_question(normalized)
            or is_memory_lookup(normalized)
        ):
            if trace is not None:
                trace.set_path("assistant_memory_fast_path")
            return self._answer_specific_memory_query(user_message, runtime) or (
                self._describe_user_memory(runtime)
                if is_user_memory_question(normalized)
                else self._describe_project_memory(runtime)
                if is_project_memory_question(normalized)
                else None
            )
        return None

    def _quick_local_memory_reply_without_context(self, user_message: str) -> str | None:
        normalized = user_message.lower().strip()
        trace = current_request_trace()
        if self._is_memory_follow_up_request(normalized):
            if trace is not None:
                trace.set_path("assistant_memory_fast_path")
                trace.record_memory_read("recent_conversation_turns")
                scope = self._memory_follow_up_scope(normalized)
                if scope == "project":
                    trace.record_memory_read("list_facts:project")
                elif scope == "all":
                    trace.record_memory_read("list_facts:user")
                    trace.record_memory_read("list_facts:project")
                    trace.record_memory_read("list_facts:operational")
                else:
                    trace.record_memory_read("list_facts:user")
            return self._describe_memory_follow_up_direct(normalized)
        if is_user_memory_question(normalized):
            if trace is not None:
                trace.set_path("assistant_memory_fast_path")
                trace.record_memory_read("list_facts:user")
            facts = self.operator_context.memory_store.list_facts("user")[:4]
            memories = self._dedupe_phrases(self._memory_fragments([fact.value for fact in facts]))[:4]
            if memories:
                return f"I remember {self._join_phrases(memories)}."
            return "I don't have much personal context stored yet."
        if is_project_memory_question(normalized):
            if trace is not None:
                trace.set_path("assistant_memory_fast_path")
                trace.record_memory_read("list_facts:project")
            facts = self.operator_context.memory_store.list_facts("project")[:4]
            memories = self._dedupe_phrases(self._memory_fragments([fact.value for fact in facts]))[:4]
            if memories:
                return f"I remember {self._join_phrases(memories)}."
            return "I know the high-level shape of Project Sovereign, but I don't have much project-specific memory saved yet."
        if is_memory_lookup(normalized):
            if trace is not None:
                trace.set_path("assistant_memory_fast_path")
            return self._answer_specific_memory_query(
                user_message,
                self.operator_context.build_runtime_snapshot(
                    focus_text=user_message,
                    context_profile="memory",
                ),
            )
        return None

    def _serialize_context(self, context: ConversationContext) -> dict[str, object]:
        return {
            "context_profile": context.context_profile,
            "recent_tasks": [self._serialize_task(task) for task in context.recent_tasks],
            "recent_created_files": context.recent_created_files,
            "workspace_entries": context.workspace_entries,
            "slack_configured": context.slack_configured,
            "recent_task_summaries": context.recent_task_summaries,
            "runtime_snapshot": {
                "model_label": context.runtime_snapshot.model_label,
                "live_tools": context.runtime_snapshot.live_tools,
                "not_yet_live_tools": context.runtime_snapshot.scaffolded_tools,
                "active_tasks": context.runtime_snapshot.active_tasks,
                "recent_actions": context.runtime_snapshot.recent_actions,
                "open_loops": context.runtime_snapshot.open_loops,
                "pending_reminders": context.runtime_snapshot.pending_reminders,
                "delivered_reminders_today": context.runtime_snapshot.delivered_reminders_today,
                "failed_reminders": context.runtime_snapshot.failed_reminders,
                "user_memory": context.runtime_snapshot.user_memory,
                "project_memory": context.runtime_snapshot.project_memory,
                "operational_memory": context.runtime_snapshot.operational_memory,
                "assistant_recall": {
                    "user_preferences": context.runtime_snapshot.assistant_recall.user_preferences,
                    "project_context": context.runtime_snapshot.assistant_recall.project_context,
                    "active_open_loops": context.runtime_snapshot.assistant_recall.active_open_loops,
                    "recent_memory": context.runtime_snapshot.assistant_recall.recent_memory,
                },
            },
            "assistant_context": {
                "identity_name": SOVEREIGN_SYSTEM_CONTEXT.identity_name,
                "capabilities": list(SOVEREIGN_SYSTEM_CONTEXT.capabilities),
                "current_tools": list(SOVEREIGN_SYSTEM_CONTEXT.current_tools),
                "constraints": list(SOVEREIGN_SYSTEM_CONTEXT.constraints),
            },
        }

    def _answer_capability_question(
        self,
        message: str,
        context: ConversationContext,
        runtime: RuntimeSnapshot,
    ) -> str:
        if "slack bot" in message and "running" in message:
            if context.slack_configured:
                return "Slack is configured, but I can't confirm from current session context whether the Socket Mode process is running right now."
            return "Slack isn't configured right now, so the bot is not ready to run."
        if self._looks_like_next_build_question(message):
            return self._describe_capability_next_build()
        if self._looks_like_agents_question(message):
            return self._describe_agent_capabilities()
        if self._looks_like_connected_question(message):
            return self._describe_current_connections()
        if self._looks_like_browser_activation_question(message):
            return self._describe_activation_requirements("browser_execution")
        if "browser" in message:
            return self._describe_browser_capability()
        if "codex" in message:
            return self._describe_codex_capability()
        if "email" in message or "gmail" in message:
            return self._describe_email_capability()
        if "calendar" in message or "task" in message or "tasks" in message:
            return self._describe_scheduling_capability()
        if self._looks_like_capability_path_question(message, "reminder"):
            return self._describe_capability_path("reminder_scheduler", "reminders")
        if self._looks_like_capability_path_question(message, "email") or (
            "what part" in message and "email" in message
        ):
            return self._describe_capability_path("email_delivery", "email")
        if self._looks_like_capability_path_question(message, "browser"):
            return self._describe_capability_path("browser_execution", "browser work")
        if self._looks_like_model_question(message):
            return self._describe_model(runtime)
        if self._looks_like_tools_question(message):
            return self._describe_tools(runtime)
        if self._looks_like_scaffolded_capability_question(message):
            return self._describe_not_yet_live_capabilities(runtime)
        if self._looks_like_live_capability_question(message):
            return self._describe_live_capabilities(runtime)
        return self._describe_capabilities()

    def _answer_specific_memory_query(
        self,
        user_message: str,
        runtime: RuntimeSnapshot,
    ) -> str | None:
        message = user_message.lower().strip()
        broad_memory_prompts = (
            "what do you remember about me",
            "what do you know about me",
            "what do you remember about this project",
            "what do you remember about project sovereign",
        )
        if any(prompt in message for prompt in broad_memory_prompts):
            return None
        if "what did i tell you before" in message or "what did i tell you earlier" in message:
            return self._describe_recent_user_context(runtime)
        if "current priority" in message:
            return self._describe_current_priority(runtime)
        if "why are you recommending memory first" in message:
            return self._describe_memory_first_reason(runtime)
        if "what preference did i tell you earlier" in message:
            return self._describe_preference_memory()
        if "what did i say two chats ago" in message:
            return self._describe_recent_user_turn(offset=2)
        if not self._looks_like_memory_lookup(message):
            return None
        if "project sovereign" in message or "this project" in message or "priority for sovereign" in message:
            return self._describe_specific_project_memory(
                user_message,
                missing="I don't have a matching Project Sovereign detail stored right now.",
            )
        if "favorite color" in message:
            missing = "I don't have your favorite color stored."
        elif "where did i" in message or "where is my" in message:
            missing = "I don't have that location stored right now."
        else:
            missing = "I don't have a matching personal memory stored for that right now."
        return self._describe_specific_user_memory(
            user_message,
            missing=missing,
        )

    def _is_memory_follow_up_request(self, message: str) -> bool:
        if not is_memory_follow_up_phrase(message):
            return False
        recent_turns = self.operator_context.recent_conversation_turns(limit=4)
        if len(recent_turns) < 3:
            return False
        assistant_index = -1
        user_index = -2
        if recent_turns[-1][0] == "user" and " ".join(recent_turns[-1][1].lower().split()) == message:
            assistant_index = -2
            user_index = -3
        if len(recent_turns) < abs(user_index):
            return False
        last_role, last_content = recent_turns[assistant_index]
        previous_role, previous_content = recent_turns[user_index]
        if last_role != "assistant" or previous_role != "user":
            return False
        return self._looks_like_memory_reply(last_content) and self._looks_like_memory_prompt(previous_content)

    def _looks_like_memory_prompt(self, text: str) -> bool:
        normalized = text.lower().strip()
        return any(
            (
                is_user_memory_question(normalized),
                is_project_memory_question(normalized),
                is_memory_lookup(normalized),
                is_memory_follow_up_phrase(normalized),
            )
        )

    def _looks_like_memory_reply(self, text: str) -> bool:
        normalized = text.lower().strip()
        reply_markers = (
            "i remember ",
            "that's all i currently have",
            "i only have one saved detail",
            "i don't have much personal context stored yet",
            "i don't have much saved about you yet",
            "i don't have any saved memory",
            "i know the high-level shape of project sovereign",
        )
        return any(marker in normalized for marker in reply_markers)

    def _memory_follow_up_scope(self, message: str) -> str:
        normalized = message.lower().strip()
        if "in memory" in normalized or "all you have" in normalized:
            return "all"
        recent_turns = self.operator_context.recent_conversation_turns(limit=4)
        if len(recent_turns) < 3:
            return "user"
        user_index = -2
        if recent_turns[-1][0] == "user" and " ".join(recent_turns[-1][1].lower().split()) == normalized:
            user_index = -3
        if len(recent_turns) < abs(user_index):
            return "user"
        previous_user = recent_turns[user_index][1].lower().strip()
        if is_project_memory_question(previous_user) or "project sovereign" in previous_user or "this project" in previous_user:
            return "project"
        if "in memory" in previous_user or "all you have" in previous_user:
            return "all"
        return "user"

    def _describe_memory_follow_up(self, message: str, runtime: RuntimeSnapshot) -> str:
        scope = self._memory_follow_up_scope(message)
        if scope == "project":
            points = self._project_memory_points(runtime)
            if not points:
                return "That's all I currently have saved for Project Sovereign right now."
            return (
                f"I don't have anything beyond that saved for Project Sovereign right now. "
                f"I currently have {self._join_phrases(points)}."
            )
        if scope == "all":
            points = self._all_memory_points(runtime)
            if not points:
                return "That's all I currently have saved in local memory right now."
            return (
                f"That's all I currently have saved in local memory right now: {self._join_phrases(points)}."
            )
        points = self._broad_user_memory(runtime)
        if not points:
            return "That's all I currently have saved about you right now."
        return (
            f"I don't have anything beyond that saved about you right now. "
            f"I currently have {self._join_phrases(points)}."
        )

    def _describe_memory_follow_up_direct(self, message: str) -> str:
        scope = self._memory_follow_up_scope(message)
        if scope == "project":
            points = self._store_project_memory_points()
            if not points:
                return "That's all I currently have saved for Project Sovereign right now."
            return (
                f"I don't have anything beyond that saved for Project Sovereign right now. "
                f"I currently have {self._join_phrases(points)}."
            )
        if scope == "all":
            points = self._store_all_memory_points()
            if not points:
                return "That's all I currently have saved in local memory right now."
            return (
                f"That's all I currently have saved in local memory right now: {self._join_phrases(points)}."
            )
        points = self._store_user_memory_points()
        if not points:
            return "That's all I currently have saved about you right now."
        return (
            f"I don't have anything beyond that saved about you right now. "
            f"I currently have {self._join_phrases(points)}."
        )

    def _answer_reminder_question(self, user_message: str, runtime: RuntimeSnapshot) -> str:
        message = user_message.lower().strip()
        if self._looks_like_pending_reminders_question(message):
            return self._describe_pending_reminders()
        if self._looks_like_recurring_reminders_question(message):
            return self._describe_pending_reminders(recurring_only=True)
        if self._looks_like_delivered_reminders_question(message):
            return self._describe_fired_reminders(runtime)
        if self._looks_like_last_delivery_question(message):
            return self._describe_last_delivery(runtime)
        if self._looks_like_reminder_health_question(message):
            return self._describe_reminder_health(runtime)
        if self._looks_like_proactive_notification_question(message):
            return self._describe_proactive_notifications(runtime)
        if self._looks_like_soft_reminder_request(message):
            return self._handle_reminder_request(user_message, runtime)
        return self._describe_pending_reminders()

    def _serialize_task(self, task: Task) -> dict[str, object]:
        return {
            "goal": task.goal,
            "status": task.status.value,
            "summary": task.summary,
            "request_mode": task.request_mode.value,
            "escalation_level": task.escalation_level.value,
            "objective_state": task.objective_state.model_dump() if task.objective_state else None,
            "results": [
                {
                    "agent": result.agent,
                    "status": result.status.value,
                    "summary": result.summary,
                    "tool_name": result.tool_name,
                    "blockers": result.blockers,
                    "next_actions": result.next_actions,
                    "evidence": [item.model_dump() for item in result.evidence],
                }
                for result in task.results
            ],
        }

    def _recent_created_files(self, tasks: list[Task]) -> list[str]:
        seen: set[str] = set()
        created_files: list[str] = []
        for task in tasks:
            for result in task.results:
                for evidence in result.evidence:
                    if not isinstance(evidence, FileEvidence) or evidence.operation != "write":
                        continue
                    formatted = self._format_path(evidence.file_path)
                    if not formatted or formatted in seen:
                        continue
                    seen.add(formatted)
                    created_files.append(formatted)

        created_items_dir = self.workspace_root / "created_items"
        if created_items_dir.exists():
            for path in sorted(
                (item for item in created_items_dir.rglob("*") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                formatted = self._format_path(str(path))
                if not formatted or formatted in seen:
                    continue
                seen.add(formatted)
                created_files.append(formatted)
                if len(created_files) >= 8:
                    break
        return created_files[:8]

    def _workspace_entries(self) -> list[str]:
        if not self.workspace_root.exists():
            return []
        try:
            return sorted(item.name for item in self.workspace_root.iterdir())[:12]
        except OSError:
            return []

    def _summarize_latest_task(self, task: Task, *, include_lead: bool = True) -> str:
        actions = [self._describe_result(result) for result in task.results if result.status == AgentExecutionStatus.COMPLETED]
        actions = [action for action in actions if action]
        blocked_result = next(
            (result for result in task.results if result.status == AgentExecutionStatus.BLOCKED),
            None,
        )

        if blocked_result is not None:
            prefix = "The last task" if include_lead else "the last task"
            if actions:
                return (
                    f"{prefix} was about {task.goal}. I {self._join_phrases(actions[:3])}, but I'm still blocked on "
                    f"{self._describe_blocker(blocked_result)}."
                )
            return f"{prefix} was about {task.goal}, and it's blocked on {self._describe_blocker(blocked_result)}."

        if actions:
            prefix = "The last thing I handled" if include_lead else "the last thing I handled"
            return f"{prefix} was {task.goal}. I {self._join_phrases(actions[:4])}."

        if task.summary:
            return task.summary if include_lead else task.summary[0].lower() + task.summary[1:]
        return (
            f"The last task was {task.goal}."
            if include_lead
            else f"the last task was {task.goal}."
        )

    def _describe_recent_files(self, recent_created_files: list[str]) -> str:
        if recent_created_files:
            files = ", ".join(recent_created_files[:6])
            return f"The most recent files I created were {files}."
        return "I don't have any recent created files to point to right now."

    def _recent_task_summaries(self, tasks: list[Task]) -> list[str]:
        summaries: list[str] = []
        for task in tasks:
            if task.summary:
                summaries.append(task.summary)
                continue
            summaries.append(self._summarize_latest_task(task))
        return summaries[:3]

    def _describe_result(self, result: AgentResult) -> str:
        if result.agent == "memory_agent":
            return ""

        file_evidence = next((item for item in result.evidence if isinstance(item, FileEvidence)), None)
        if file_evidence is not None:
            target = self._format_path(file_evidence.file_path)
            if file_evidence.operation == "write" and target:
                return f"created {target}"
            if file_evidence.operation == "read" and target:
                return f"read {target}"
            if file_evidence.operation == "list":
                return f"checked {target or 'the requested directory'}"

        tool_evidence = next((item for item in result.evidence if isinstance(item, ToolEvidence)), None)
        if tool_evidence is not None and tool_evidence.tool_name == "runtime_tool":
            command = str(tool_evidence.payload.get("command", "")).strip()
            if command:
                return f"ran `{command}`"
        if tool_evidence is not None and tool_evidence.tool_name == "web_search_tool":
            provider = str(tool_evidence.payload.get("provider", "")).strip()
            sources = tool_evidence.payload.get("sources", [])
            source_count = len(sources) if isinstance(sources, list) else 0
            if provider and source_count:
                return f"researched it with {provider} and captured {source_count} source(s)"
            return "checked the relevant research constraints"

        lowered = result.summary.rstrip(".")
        if lowered:
            return lowered[0].lower() + lowered[1:]
        return ""

    def _describe_blocker(self, result: AgentResult) -> str:
        if result.blockers:
            return result.blockers[0].rstrip(".")
        return result.summary.rstrip(".")

    def _join_phrases(self, phrases: list[str]) -> str:
        cleaned: list[str] = []
        seen: set[str] = set()
        for phrase in phrases:
            normalized = phrase.strip().rstrip(".")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        if not cleaned:
            return ""
        if len(cleaned) == 1:
            return cleaned[0]
        if len(cleaned) == 2:
            return f"{cleaned[0]} and {cleaned[1]}"
        return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"

    def _format_path(self, raw_path: str | None) -> str | None:
        if not raw_path:
            return None
        candidate = Path(raw_path)
        try:
            relative = candidate.relative_to(self.workspace_root)
            return f"`{relative.as_posix()}`"
        except ValueError:
            return f"`{candidate.name}`"

    def _looks_like_last_task_question(self, message: str) -> bool:
        return (
            "what did you just do" in message
            or "what happened last task" in message
            or "what was the last task" in message
            or "what did you do" in message
        )

    def _looks_like_social_acknowledgement(self, message: str) -> bool:
        return is_thanks_message(message) or self._is_short_social_message(
            message,
            ("thanks", "thank you", "thx"),
        )

    def _looks_like_greeting(self, message: str) -> bool:
        if is_greeting_message(message):
            return True
        if self._is_short_social_message(message, ("hello", "hi", "hey", "yo", "hello there", "hey there")):
            return True
        return message in {"good morning", "good afternoon", "good evening", "what's up", "whats up"}

    def _looks_like_active_work_question(self, message: str) -> bool:
        return "what are you working on" in message or "what are we working on" in message

    def _looks_like_continuity_question(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in (
                "what were we focused on before",
                "what were we doing before",
                "where were we at",
                "what were we focused on",
            )
        )

    def _looks_like_model_question(self, message: str) -> bool:
        return "what model are you using" in message or "which model are you using" in message

    def _looks_like_tools_question(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in (
                "what tools do you have",
                "which tools do you have",
                "what tools are live",
                "what do you have access to",
            )
        )

    def _looks_like_agents_question(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in (
                "what agents do you have",
                "what subagents do you have",
                "which agents do you have",
                "do you have agents",
                "do you have subagents",
            )
        )

    def _looks_like_connected_question(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in (
                "what is currently connected",
                "what's currently connected",
                "what integrations are connected",
                "what is connected",
                "what's connected",
            )
        )

    def _looks_like_next_build_question(self, message: str) -> bool:
        return "what should we build next" in message or "what should i build next" in message

    def _looks_like_scaffolded_capability_question(self, message: str) -> bool:
        return "what is scaffolded" in message or "what's scaffolded" in message

    def _looks_like_live_capability_question(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in ("what can you do right now", "what can you actually do right now", "what is live right now")
        )

    def _looks_like_browser_activation_question(self, message: str) -> bool:
        return "turn on browser automation" in message or "enable browser automation" in message

    def _looks_like_capabilities_question(self, message: str) -> bool:
        return message == "help" or any(
            phrase in message
            for phrase in (
                "what can you do",
                "how can you help",
                "what are you able to do",
                "what are you able to help with",
                "what can you help me with",
                "what can you help with",
            )
        )

    def _looks_like_capability_question(self, message: str) -> bool:
        return (
            self._looks_like_model_question(message)
            or self._looks_like_tools_question(message)
            or self._looks_like_agents_question(message)
            or self._looks_like_connected_question(message)
            or self._looks_like_next_build_question(message)
            or self._looks_like_scaffolded_capability_question(message)
            or self._looks_like_live_capability_question(message)
            or self._looks_like_browser_activation_question(message)
            or self._looks_like_capabilities_question(message)
            or message.startswith(("can you browse", "could you browse", "can you open websites"))
            or any(
                phrase in message
                for phrase in (
                    "can you use the browser",
                    "can you use browser use",
                    "can you use codex",
                    "can you send emails",
                    "can you send email",
                    "can you use gmail",
                    "can you see my calendar",
                    "can you see my tasks",
                    "do you have codex",
                    "do you have gmail",
                    "do you have calendar",
                    "do you have tasks",
                )
            )
            or self._looks_like_capability_path_question(message, "reminder")
            or self._looks_like_capability_path_question(message, "email")
            or self._looks_like_capability_path_question(message, "browser")
            or ("slack bot" in message and "running" in message)
        )

    def _looks_like_capability_path_question(self, message: str, subject: str) -> bool:
        return (
            ("which agent" in message or "what part handles" in message or "what handles" in message)
            and subject in message
        )

    def _looks_like_identity_question(self, message: str) -> bool:
        return "who are you" in message or message in {
            "what are you",
            "what are you?",
            "what exactly are you",
            "what exactly are you?",
        }

    def _looks_like_project_identity_question(self, message: str) -> bool:
        return "what is this project" in message or "remind me what this project is" in message

    def _looks_like_user_memory_question(self, message: str) -> bool:
        return is_user_memory_question(message)

    def _looks_like_project_memory_question(self, message: str) -> bool:
        return is_project_memory_question(message)

    def _looks_like_memory_lookup(self, message: str) -> bool:
        return is_memory_lookup(message)

    def _looks_like_pending_reminders_question(self, message: str) -> bool:
        return "what reminders do i have" in message or "do i have any reminders" in message

    def _looks_like_recurring_reminders_question(self, message: str) -> bool:
        return "what recurring reminders" in message or "which recurring reminders" in message

    def _looks_like_delivered_reminders_question(self, message: str) -> bool:
        return "what reminders have fired" in message or "what reminders fired today" in message

    def _looks_like_last_delivery_question(self, message: str) -> bool:
        return "did you remind me already" in message or "did you remind me" in message

    def _looks_like_reminder_health_question(self, message: str) -> bool:
        return "is reminder delivery working" in message or "are reminders working" in message

    def _looks_like_proactive_notification_question(self, message: str) -> bool:
        return "can you proactively notify me" in message or "can you send me a slack message later" in message

    def _looks_like_reminder_question(self, message: str) -> bool:
        return "reminder" in message or "remind me" in message

    def _looks_like_calendar_question(self, message: str) -> bool:
        return looks_like_calendar_read_request(message)

    def _looks_like_soft_reminder_request(self, message: str) -> bool:
        if "remind me" not in message:
            return False
        if "every day" in message or "daily" in message or "every " in message or "each " in message:
            return True
        return "later" in message and not any(
            phrase in message
            for phrase in (
                "did you remind me",
                "what reminders do i have",
                "what reminders have fired",
                "remind me what",
                "remind me who",
                "remind me why",
            )
        )

    def _contains_any_phrase(self, message: str, phrases: tuple[str, ...]) -> bool:
        normalized = self._normalize_phrase_text(message)
        return any(f" {self._normalize_phrase_text(phrase).strip()} " in normalized for phrase in phrases)

    def _normalize_phrase_text(self, value: str) -> str:
        translation = str.maketrans({char: " " for char in ".,!?;:\n\r\t"})
        compact = " ".join(value.translate(translation).split())
        return f" {compact} "

    def _is_short_social_message(self, message: str, phrases: tuple[str, ...]) -> bool:
        normalized = self._normalize_phrase_text(message).strip()
        if len(normalized.split()) > 4:
            return False
        return any(normalized == self._normalize_phrase_text(phrase).strip() for phrase in phrases)

    def _describe_identity(self, runtime: RuntimeSnapshot) -> str:
        if runtime.llm_ready:
            return (
                f"I'm {SOVEREIGN_SYSTEM_CONTEXT.identity_name}, your main operator for this project. "
                "I keep track of context and memory, help with day-to-day requests, and stay honest about what's actually live."
            )
        return (
            f"I'm {SOVEREIGN_SYSTEM_CONTEXT.identity_name}, your main operator for this project. "
            "This runtime is on the local fallback path, so I'll keep things simple, use memory and context when I can, and be direct about what's live."
        )

    def _describe_project(self, runtime: RuntimeSnapshot) -> str:
        memory = runtime.assistant_recall.project_context[:2] or runtime.project_memory[:2]
        if memory:
            joined = " ".join(item.rstrip(".") + "." for item in self._memory_fragments(memory))
            return joined
        return f"{SOVEREIGN_SYSTEM_CONTEXT.identity_name} is {SOVEREIGN_SYSTEM_CONTEXT.identity_summary}"

    def _describe_capabilities(self) -> str:
        runtime = self.operator_context.build_runtime_snapshot()
        if runtime.assistant_recall.has_brief_preference():
            return self._describe_live_capabilities(runtime)
        context = self.capability_catalog.ceo_context()
        groups = context.status_groups()
        live = [
            snapshot.display_name
            for snapshot in groups.get("live", [])
            if snapshot.capability_id not in {"assistant_direct"}
        ][:6]
        not_live = [
            snapshot.display_name
            for snapshot in (
                groups.get("configured_but_disabled", [])
                + groups.get("unavailable", [])
                + groups.get("scaffolded", [])
                + groups.get("planned", [])
            )
        ][:4]
        base = (
            "be the main CEO-style operator: answer questions, remember useful context, plan and delegate work, "
            "run bounded browser tasks, use the live tools that are connected, and review evidence before calling work done"
        )
        if live:
            base += f". Live right now: {self._join_phrases(live)}"
        if not_live:
            base += f". I will be honest that {self._join_phrases(not_live)} still need setup, enablement, or future work"
        return f"Right now I can {base}."

    def _acknowledge_preference(self, message: str) -> str:
        if "concise" in message or "brief" in message:
            return "I'll keep it concise."
        if "more detail" in message:
            return "I'll add more detail when it helps."
        if "natural" in message or "direct" in message:
            return "I'll keep it natural and direct."
        return "I'll keep that in mind."

    def _describe_model(self, runtime: RuntimeSnapshot) -> str:
        if runtime.llm_ready:
            return f"I'm currently using {runtime.model_label} for reasoning."
        return "No external LLM is configured right now, so the model path is not configured and I'm answering from the local fallback path in this runtime."

    def _describe_tools(self, runtime: RuntimeSnapshot) -> str:
        live_tools = self._tool_phrases(runtime, limit=4)
        not_live = self._top_unavailable_capabilities(runtime, limit=3)
        if live_tools and not_live:
            return (
                f"Live right now I have {self._join_phrases(live_tools)}. "
                f"Not live yet: {self._join_phrases(not_live)}."
            )
        if live_tools:
            return f"Live right now I have {self._join_phrases(live_tools)}."
        if not_live:
            return f"I don't have a live tool path right now. The main not-yet-live areas are {self._join_phrases(not_live)}."
        return "I don't have a tool summary loaded right now."

    def _describe_browser_capability(self) -> str:
        browser = self.capability_catalog.snapshot_for("browser_execution") or self.capability_catalog.snapshot_for("playwright_browser")
        browser_use = self.capability_catalog.snapshot_for("browser_use_browser")
        browser_status = self._plain_capability_state(browser, "the local browser path")
        visible = (
            "Visible browser mode is on."
            if settings.browser_visible or settings.browser_show_window or not settings.browser_headless
            else "It normally runs headless unless visible browser mode is enabled."
        )
        browser_use_status = self._plain_capability_state(browser_use, "Browser Use")
        return (
            f"For browser work, {browser_status}. I use it for specific URLs, page inspection, summaries, and evidence capture. "
            f"{visible} {browser_use_status}; I only use Browser Use for safe multi-step browser workflows when it is actually enabled. "
            "I still block on login, CAPTCHA, 2FA, payment, or sensitive forms unless you complete the human step."
        )

    def _describe_codex_capability(self) -> str:
        codex = self.capability_catalog.snapshot_for("codex_cli")
        status = self._plain_capability_state(codex, "the Codex coding lane")
        setup = self._setup_phrase(codex)
        if codex and codex.status == "live":
            return (
                f"Yes. {status}. I delegate serious coding work there when a task needs implementation, debugging, refactoring, or tests. "
                "Codex still has to return changed files, diff or command evidence, and test output before I treat the work as done."
            )
        return (
            f"Codex is part of the system, but {status}. {setup} "
            "Until it is enabled, I can still handle smaller workspace file and runtime tasks through the local coding tools."
        )

    def _describe_email_capability(self) -> str:
        gmail = self.capability_catalog.snapshot_for("gmail") or self.capability_catalog.snapshot_for("email_delivery")
        status = self._plain_capability_state(gmail, "Gmail/email")
        setup = self._setup_phrase(gmail)
        if gmail and gmail.status == "live":
            return (
                f"Yes. {status}. I can read/search/summarize and draft through the Communications Agent. "
                "Sending, deleting, archiving, forwarding, or bulk mailbox changes require explicit confirmation and provider evidence."
            )
        return (
            f"Email is owned by the Communications Agent, but {status}. {setup} "
            "I can remember explicit contact aliases you give me, but I will not pretend to send or read Gmail until OAuth access is live."
        )

    def _describe_scheduling_capability(self) -> str:
        calendar = self.capability_catalog.snapshot_for("google_calendar")
        tasks = self.capability_catalog.snapshot_for("google_tasks")
        reminders = self.capability_catalog.snapshot_for("reminder_scheduler")
        parts = [
            self._plain_capability_state(calendar, "Google Calendar"),
            self._plain_capability_state(tasks, "Google Tasks"),
            self._plain_capability_state(reminders, "reminders"),
        ]
        setup = self._join_phrases(
            [
                self._setup_phrase(calendar, include_ready=False),
                self._setup_phrase(tasks, include_ready=False),
                self._setup_phrase(reminders, include_ready=False),
            ]
        )
        if not setup:
            setup = "Read/list actions can run when their provider is live; writes and destructive changes keep confirmation gates."
        return (
            f"Scheduling is handled by the Scheduling Agent: {self._join_phrases(parts)}. "
            f"{setup} Calendar events, reminder records, and task changes need concrete provider evidence before I call them complete."
        )

    def _describe_agent_capabilities(self) -> str:
        context = self.capability_catalog.ceo_context()
        visible_agents = [
            agent
            for agent in context.agents
            if agent["name"]
            in {
                "supervisor",
                "planning_agent",
                "research_agent",
                "browser_agent",
                "coding_agent",
                "codex_cli_agent",
                "personal_ops_agent",
                "communications_agent",
                "scheduling_agent",
                "memory_agent",
                "reviewer_agent",
                "verifier_agent",
            }
        ]
        lines = [
            f"{agent['display_name']} ({agent['status']})"
            for agent in visible_agents
        ]
        return (
            f"I have one main CEO operator, with these lanes underneath: {self._join_phrases(lines)}. "
            "Research handles source-backed lookup, Browser handles page/web workflows, Scheduling handles calendar/tasks/reminders, "
            "Communications handles Gmail and outbound messages, Codex/Coding handles code work, and Reviewer/Verifier check evidence."
        )

    def _describe_current_connections(self) -> str:
        context = self.capability_catalog.ceo_context()
        groups = context.status_groups()
        live = [
            snapshot.display_name
            for snapshot in groups.get("live", [])
            if snapshot.capability_id not in {"assistant_direct"}
        ][:8]
        disabled = [snapshot.display_name for snapshot in groups.get("configured_but_disabled", [])][:4]
        needs_setup = [snapshot.display_name for snapshot in groups.get("unavailable", [])][:4]
        planned = [snapshot.display_name for snapshot in groups.get("planned", [])][:4]
        sections = []
        if live:
            sections.append(f"Live now: {self._join_phrases(live)}")
        if disabled:
            sections.append(f"Configured but off: {self._join_phrases(disabled)}")
        if needs_setup:
            sections.append(f"Needs setup: {self._join_phrases(needs_setup)}")
        else:
            sections.append("Needs setup: none currently reported")
        if planned:
            sections.append(f"Planned/not callable yet: {self._join_phrases(planned)}")
        if not sections:
            return "I do not have a connection summary loaded right now."
        return ". ".join(sections) + "."

    def _describe_capability_next_build(self) -> str:
        calendar = self.capability_catalog.snapshot_for("google_calendar")
        tasks = self.capability_catalog.snapshot_for("google_tasks")
        gmail = self.capability_catalog.snapshot_for("gmail")
        browser_use = self.capability_catalog.snapshot_for("browser_use_browser")
        if calendar and calendar.status != "live":
            return (
                "The next best build is finishing the Scheduling Agent's Google Calendar readiness: OAuth setup, read/write evidence, "
                "and clean confirmations for updates/deletes. That gives the life-assistant layer a real daily-use backbone."
            )
        if tasks and tasks.status != "live":
            return "Next, wire Google Tasks fully into Scheduling so calendar, reminders, and to-dos agree in one life-ops lane."
        if gmail and gmail.status != "live":
            return "Next, finish Gmail readiness for the Communications Agent, keeping sends and mailbox changes behind confirmation."
        if browser_use and browser_use.status != "live":
            return "Next, enable Browser Use as a Browser Agent escalation path with normalized evidence for safe multi-step browser work."
        return "Next, I would strengthen the reviewer/verifier loop and dashboard visibility so every delegated result has clearer evidence."

    def _plain_capability_state(self, snapshot, label: str) -> str:
        if snapshot is None:
            return f"{label} is not recorded in my capability map yet"
        if snapshot.status == "live":
            return f"{label} is live"
        if snapshot.status == "configured_but_disabled":
            return f"{label} is configured but turned off"
        if snapshot.status == "scaffolded":
            return f"{label} is partly built but not fully live"
        if snapshot.status == "unavailable":
            return f"{label} needs setup"
        if snapshot.status == "planned":
            return f"{label} is planned, not live"
        return f"{label} is {snapshot.status.replace('_', ' ')}"

    def _setup_phrase(self, snapshot, *, include_ready: bool = True) -> str:
        if snapshot is None:
            return "I need a capability record before I can report setup accurately."
        if snapshot.status == "live":
            return "It is ready." if include_ready else ""
        missing = list(dict.fromkeys(snapshot.missing_config or []))
        if not missing and snapshot.status == "configured_but_disabled":
            missing = [f"{snapshot.display_name} enabled"]
        if not missing:
            missing = list(dict.fromkeys(snapshot.config_requirements or []))
        if not missing:
            return "The blocker is that it is not enabled for execution yet."
        readable = [self._human_setup_name(item) for item in missing[:4]]
        return f"Setup needed: {self._join_phrases(readable)}."

    def _human_setup_name(self, value: str) -> str:
        mapping = {
            "BROWSER_ENABLED": "browser execution enabled",
            "PLAYWRIGHT_PACKAGE": "Playwright installed",
            "PLAYWRIGHT_BROWSER_BINARY": "a Playwright browser binary",
            "BROWSER_USE_ENABLED": "Browser Use enabled",
            "BROWSER_USE_API_KEY": "Browser Use credentials",
            "BROWSER_USE_SDK": "the Browser Use SDK",
            "CODEX_CLI_ENABLED": "Codex CLI enabled",
            "CODEX_CLI_COMMAND": "a Codex CLI command",
            "OPENROUTER_API_KEY": "an OpenRouter key from the secrets/environment layer",
            "SEARCH_PROVIDER": "the Gemini search provider setting",
            "GMAIL_ENABLED": "Gmail enabled",
            "GMAIL_CREDENTIALS_PATH": "Gmail OAuth credentials",
            "GMAIL_TOKEN_PATH": "saved Gmail access",
            "GMAIL_DEPS": "Google Gmail dependencies",
            "GOOGLE_CALENDAR_ENABLED": "Google Calendar enabled",
            "GOOGLE_CALENDAR_CREDENTIALS_PATH": "Google Calendar OAuth credentials",
            "GOOGLE_CALENDAR_TOKEN_PATH": "saved Google Calendar access",
            "GOOGLE_CALENDAR_DEPS": "Google Calendar dependencies",
            "GOOGLE_TASKS_ENABLED": "Google Tasks enabled",
            "GOOGLE_TASKS_CREDENTIALS_PATH": "Google Tasks OAuth credentials",
            "GOOGLE_TASKS_TOKEN_PATH": "saved Google Tasks access",
            "GOOGLE_TASKS_DEPS": "Google Tasks dependencies",
            "SCHEDULER_BACKEND": "the scheduler backend",
            "APSCHEDULER_PACKAGE": "APScheduler installed",
            "REMINDERS_ENABLED": "reminders enabled",
            "SLACK_BOT_TOKEN": "a Slack bot token in the secrets/environment layer",
            "SLACK_APP_TOKEN": "a Slack app token in the secrets/environment layer",
            "SLACK_ENABLED": "Slack enabled",
        }
        return mapping.get(value.upper(), value.replace("_", " ").lower())

    def _describe_live_capabilities(self, runtime: RuntimeSnapshot) -> str:
        if runtime.assistant_recall.has_brief_preference():
            live = [
                "answer questions",
                "keep track of context",
                "work with files",
                "run local commands",
            ]
            live.extend(self._tool_phrases(runtime, limit=2))
            return f"Right now I can {self._join_phrases(self._dedupe_phrases(live))}."
        live = [
            "answer questions and talk things through",
            "remember useful context from recent work",
            "work with files",
            "run local commands",
        ]
        live.extend(self._tool_phrases(runtime, limit=3))
        live = self._dedupe_phrases(live)
        unavailable = self._top_unavailable_capabilities(runtime, limit=2)
        if unavailable:
            return (
                f"Right now I can {self._join_phrases(live)}. "
                f"I won't fake things like {self._join_phrases(unavailable)} until they're actually live."
            )
        return f"Right now I can {self._join_phrases(live)}."

    def _describe_not_yet_live_capabilities(self, runtime: RuntimeSnapshot) -> str:
        non_live = self._top_unavailable_capabilities(runtime, limit=5)
        if not non_live:
            return "I don't have any not-yet-live capability metadata loaded right now."
        return (
            f"The main things that still aren't live are {self._join_phrases(non_live)}. "
            "I'll keep being explicit instead of pretending those paths already work."
        )

    def _describe_capability_path(self, capability_name: str, label: str) -> str:
        snapshot = self.capability_catalog.snapshot_for(capability_name)
        if snapshot is None:
            return "I don't have a capability record for that yet."
        agent = (snapshot.owner_agent or "the CEO operator").replace("_", " ").title()
        if snapshot.status == "live":
            return (
                f"{label.title()} is handled by {agent}, and that capability is live here. "
                "I still require concrete evidence before saying the work is complete."
            )
        return (
            f"{label.title()} is handled by {agent}, but it is {snapshot.plain_status()} here. "
            f"{self._setup_phrase(snapshot)}"
        )

    def _describe_activation_requirements(self, capability_name: str) -> str:
        snapshot = self.capability_catalog.snapshot_for(capability_name)
        if snapshot is None:
            return "I don't have activation metadata for that capability yet."
        requirements = snapshot.config_requirements + snapshot.missing_config
        if not requirements:
            return (
                f"{snapshot.display_name} is currently {snapshot.plain_status()}. "
                "The remaining work is mainly any site-specific access or workflow support that still needs to be added."
            )
        joined = self._join_phrases([self._human_setup_name(item) for item in dict.fromkeys(requirements)])
        return (
            f"To move {snapshot.display_name} forward, I need {joined}. "
            "Once those pieces are ready, the browser connection can run live work without pretending unsupported cases are complete."
        )

    def _describe_active_work(self, runtime: RuntimeSnapshot) -> str:
        if runtime.active_tasks:
            return f"I'm currently working on {self._join_phrases(runtime.active_tasks[:3])}."
        if runtime.assistant_recall.active_open_loops:
            return (
                "I'm not actively running a task right now, but I'm still tracking "
                f"{self._join_phrases(runtime.assistant_recall.active_open_loops[:3])}."
            )
        return "I'm not actively working on anything right now."

    def _describe_user_memory(self, runtime: RuntimeSnapshot) -> str:
        memories = self._broad_user_memory(runtime)
        if memories:
            if len(memories) == 1:
                return f"I only have one saved detail about you right now: {memories[0]}."
            return f"I remember {self._join_phrases(memories)}."
        return "I don't have much saved about you yet. I'll keep useful stable details as we go."

    def _describe_project_memory(self, runtime: RuntimeSnapshot) -> str:
        project_memory = self._project_memory_points(runtime)
        if project_memory:
            if len(project_memory) == 1:
                return f"I only have one saved Project Sovereign detail right now: {project_memory[0]}."
            return f"I remember {self._join_phrases(project_memory)}."
        return "I know the high-level shape of Project Sovereign, but I don't have much project-specific memory saved yet."

    def _describe_specific_user_memory(self, query: str, *, missing: str) -> str:
        facts = self.operator_context.recall_facts(
            query,
            layers=("user",),
            limit=2,
        )
        if facts:
            return facts[0].value
        return missing

    def _describe_specific_project_memory(self, query: str, *, missing: str) -> str:
        facts = self.operator_context.recall_facts(
            query,
            layers=("project",),
            limit=2,
        )
        if facts:
            return facts[0].value
        return missing

    def _describe_preference_memory(self) -> str:
        facts = self.operator_context.recall_facts(
            "user preference response style",
            layers=("user",),
            limit=3,
            include_categories=("preference",),
        )
        if facts:
            return f"You told me {self._strip_sentence_period(facts[0].value)}."
        return "I don't have a preference from you stored yet."

    def _describe_recent_user_turn(self, *, offset: int) -> str:
        turns = self.operator_context.recent_user_turns(limit=max(offset + 2, 5))
        if len(turns) < offset:
            return "I don't have enough preserved conversation history to answer that exactly."
        target = turns[-offset]
        return f'{offset} user messages ago, you said: "{target}".'

    def _memory_fragments(self, items: list[str]) -> list[str]:
        fragments: list[str] = []
        for item in items:
            normalized = item.strip().rstrip(".")
            if not normalized:
                continue
            if normalized.startswith(("You ", "Your ", "The ", "For this project")):
                fragments.append(normalized[0].lower() + normalized[1:])
                continue
            fragments.append(normalized)
        return fragments

    def _describe_next_work(self, runtime: RuntimeSnapshot) -> str:
        priorities = (
            runtime.assistant_recall.active_open_loops[:3]
            or runtime.assistant_recall.recent_memory[:3]
            or runtime.operational_memory[:3]
            or runtime.assistant_recall.project_context[:2]
        )
        if priorities:
            lead = (
                "Your current priority seems to be"
                if runtime.assistant_recall.active_open_loops or runtime.assistant_recall.project_context
                else "The clearest next thing I see is"
            )
            return f"{lead} {self._join_phrases(priorities)}."
        return "I don't have a strong unfinished-work summary yet. That should improve once I have more real task history."

    def _respond_to_planning_discussion(self, runtime: RuntimeSnapshot) -> str:
        priorities = (
            runtime.assistant_recall.active_open_loops[:2]
            or runtime.operational_memory[:2]
            or runtime.assistant_recall.project_context[:2]
        )
        if priorities:
            memory_prefix = ""
            if runtime.assistant_recall.project_context:
                preferred_context = next(
                    (
                        item
                        for item in runtime.assistant_recall.project_context
                        if any(keyword in item.lower() for keyword in ("priority", "focus", "memory"))
                    ),
                    runtime.assistant_recall.project_context[0],
                )
                memory_prefix = f"I remember {preferred_context.rstrip('.')}, so "
            return (
                f"{memory_prefix}let's keep this conversational for now. The clearest next areas I see are {self._join_phrases(priorities)}. "
                "I can help narrow that into the next step before we execute anything."
            )
        return (
            "Let's keep it conversational for now. Tell me the goal, what's blocked, or the options you're weighing, "
            "and I'll help map the next step."
        )

    def _describe_continue(self, runtime: RuntimeSnapshot) -> str:
        if runtime.active_tasks:
            return f"I'd continue with {runtime.active_tasks[0]}."
        if runtime.assistant_recall.active_open_loops:
            return f"The clearest thread to pick back up is {runtime.assistant_recall.active_open_loops[0]}."
        return "I don't have an active thread to continue yet. Give me a goal and I'll pick it up."

    def _describe_continuity(self, runtime: RuntimeSnapshot) -> str:
        priorities = runtime.assistant_recall.active_open_loops[:2] or runtime.assistant_recall.recent_memory[:2]
        if priorities:
            return f"We were mainly focused on {self._join_phrases(priorities)}."
        if runtime.assistant_recall.project_context:
            return f"The strongest project context I have is {self._join_phrases(self._project_memory_points(runtime)[:2])}."
        return "I don't have a strong thread to pick back up yet."

    def _describe_memory_first_reason(self, runtime: RuntimeSnapshot) -> str:
        reasons = runtime.assistant_recall.project_context[:2] or runtime.project_memory[:2]
        if reasons:
            return (
                f"I'm recommending memory first because I remember {self._join_phrases(reasons)}. "
                "That improves continuity before we broaden the rest of the system."
            )
        return "I'm recommending memory first because continuity and recall need to feel real before more expansion matters."

    def _handle_reminder_request(self, user_message: str, runtime: RuntimeSnapshot) -> str:
        del runtime
        recurring = parse_recurring_reminder_request(
            user_message,
            timezone_name=settings.scheduler_timezone,
        )
        if recurring is not None:
            if recurring.follow_up_question:
                return recurring.follow_up_question
            if recurring.schedule is not None and recurring.summary is not None:
                return (
                    "That looks like a real recurring reminder request. "
                    "Send it as an action message in Slack and I can schedule it for real."
                )

        reminder = self.operator_context.reminder_summary_from_message(user_message)
        summary = reminder or "follow up on this"
        self.operator_context.remember_open_loop(summary, source="conversation")
        return (
            f"I've noted that and I'll keep it in mind here: {summary}. "
            "I can't promise a scheduled reminder from this conversation path yet because the scheduler isn't live here, so send it as an action request and I'll use the live reminder path."
        )

    def _describe_current_priority(self, runtime: RuntimeSnapshot) -> str:
        continuity_runtime = self.operator_context.build_runtime_snapshot(context_profile="continuity")
        priorities = (
            continuity_runtime.assistant_recall.active_open_loops[:2]
            or self._project_memory_points(continuity_runtime)[:2]
            or continuity_runtime.operational_memory[:2]
        )
        if priorities:
            return f"Your current priority seems to be {self._join_phrases(priorities)}."
        return "I don't have a confident current priority stored for you yet."

    def _describe_recent_user_context(self, runtime: RuntimeSnapshot) -> str:
        memories = self._broad_user_memory(runtime)
        if memories:
            return f"You told me {self._join_phrases(memories)}."
        turns = self.operator_context.recent_user_turns(limit=3)
        if turns:
            return f'Recently you said: "{turns[-1]}"'
        return "I don't have much preserved personal context yet."

    def _broad_user_memory(self, runtime: RuntimeSnapshot) -> list[str]:
        items = (
            runtime.assistant_recall.user_preferences[:2]
            + runtime.user_memory[:4]
            + runtime.assistant_recall.recent_memory[:2]
        )
        return self._dedupe_phrases(self._memory_fragments(items))[:4]

    def _project_memory_points(self, runtime: RuntimeSnapshot) -> list[str]:
        items = runtime.assistant_recall.project_context[:4] + runtime.project_memory[:4]
        return self._dedupe_phrases(self._memory_fragments(items))[:4]

    def _all_memory_points(self, runtime: RuntimeSnapshot) -> list[str]:
        items = (
            runtime.assistant_recall.user_preferences[:2]
            + runtime.user_memory[:4]
            + runtime.project_memory[:3]
            + runtime.assistant_recall.recent_memory[:2]
        )
        return self._dedupe_phrases(self._memory_fragments(items))[:8]

    def _store_user_memory_points(self) -> list[str]:
        user_facts = self.operator_context.memory_store.list_facts("user")[:6]
        return self._dedupe_phrases(self._memory_fragments([fact.value for fact in user_facts]))[:6]

    def _store_project_memory_points(self) -> list[str]:
        project_facts = self.operator_context.memory_store.list_facts("project")[:6]
        return self._dedupe_phrases(self._memory_fragments([fact.value for fact in project_facts]))[:6]

    def _store_all_memory_points(self) -> list[str]:
        operational_facts = [
            fact
            for fact in self.operator_context.memory_store.list_facts("operational")
            if fact.category not in {"active_task", "recent_result", "task_context", "current_goal"}
        ][:4]
        items = (
            [fact.value for fact in self.operator_context.memory_store.list_facts("user")[:6]]
            + [fact.value for fact in self.operator_context.memory_store.list_facts("project")[:6]]
            + [fact.value for fact in operational_facts]
        )
        return self._dedupe_phrases(self._memory_fragments(items))[:8]

    def _tool_phrases(self, runtime: RuntimeSnapshot, *, limit: int) -> list[str]:
        phrases = [self._tool_phrase(item) for item in runtime.live_tools]
        phrases = [phrase for phrase in phrases if phrase]
        return self._dedupe_phrases(phrases)[:limit]

    def _top_unavailable_capabilities(self, runtime: RuntimeSnapshot, *, limit: int) -> list[str]:
        phrases: list[str] = []
        for item in runtime.scaffolded_tools + runtime.configured_tools + runtime.planned_tools:
            phrase = self._capability_label(item)
            if phrase:
                phrases.append(phrase)
        return self._dedupe_phrases(phrases)[:limit]

    def _tool_phrase(self, item: str) -> str | None:
        label = self._capability_label(item)
        mapping = {
            "workspace file work": "work with files in the workspace",
            "local runtime commands": "run local commands when needed",
            "one-time reminders": "schedule one-time reminders",
            "slack transport": "talk to you in Slack",
            "slack outbound delivery": "send reminder follow-ups back in Slack",
        }
        return mapping.get(label, label)

    def _capability_label(self, item: str) -> str:
        cleaned = re.sub(r"\s*\([^)]*\)\s*:.*$", "", item).strip()
        lowered = cleaned.lower()
        if "file_tool" in lowered or "workspace-scoped file" in lowered:
            return "workspace file work"
        if "runtime_tool" in lowered or "runtime command" in lowered:
            return "local runtime commands"
        if "reminder" in lowered:
            return "one-time reminders"
        if "slack" in lowered and "delivery" in lowered:
            return "slack outbound delivery"
        if "slack" in lowered and "transport" in lowered:
            return "slack transport"
        if "browser" in lowered:
            return "browser automation"
        if "email" in lowered:
            return "email sending"
        if "calendar" in lowered:
            return "calendar actions"
        return cleaned.split(":", 1)[0].replace("_", " ").strip()

    def _dedupe_phrases(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for item in items:
            normalized = item.strip().rstrip(".")
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            deduped.append(normalized)
        return deduped

    def _strip_sentence_period(self, text: str) -> str:
        return text.strip().rstrip(".")

    def _contains_backend_jargon(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in (
                "task_status",
                "request_mode",
                "planner_mode",
                "execution state",
                "route_",
                "langgraph",
                "subtask",
                "orchestration graph",
                "evaluator",
                "cost=",
                "risk=",
                "planner ",
                "blocked thread",
                "resume_target",
                "pending_action",
            )
        )

    def _describe_pending_reminders(self, recurring_only: bool = False) -> str:
        reminders = self.reminder_service.list_active_reminders(recurring_only=recurring_only)
        if reminders:
            for reminder in reversed(reminders[:4]):
                self.operator_context.register_actionable_object(
                    object_type="reminder",
                    object_id=reminder.reminder_id,
                    summary=reminder.summary,
                    source="reminder_scheduler",
                    confidence=0.9,
                )
            count = len(reminders)
            lines = [self._describe_reminder_record(item) for item in reminders[:4]]
            label = "recurring reminder" if recurring_only else "pending reminder"
            plural = "s" if count != 1 else ""
            return f"You have {count} {label}{plural}: {self._join_phrases(lines)}."
        return "You don't have any pending reminders right now."

    def _describe_fired_reminders(self, runtime: RuntimeSnapshot) -> str:
        if runtime.delivered_reminders_today:
            return f"Today's delivered reminders were {self._join_phrases(runtime.delivered_reminders_today[:4])}."
        return "I haven't delivered any reminders today."

    def _describe_last_delivery(self, runtime: RuntimeSnapshot) -> str:
        if runtime.delivered_reminders_today:
            return f"Yes. The latest delivered reminder was {runtime.delivered_reminders_today[0]}."
        if runtime.pending_reminders:
            return f"Not yet. The next pending reminder is {runtime.pending_reminders[0]}."
        if runtime.failed_reminders:
            return f"I haven't completed the latest reminder delivery successfully. The most recent failure was {runtime.failed_reminders[0]}."
        return "I don't have any reminder deliveries recorded yet."

    def _answer_calendar_question(self, user_message: str) -> str:
        query = self.scheduling_agent.interpret_calendar_query(user_message)
        if query is None:
            return "I can check today, tomorrow, this week, or your next calendar event once Google Calendar is configured."
        agent_result = self.scheduling_agent.read_calendar(user_message, subtask_id="conversation-calendar-read")
        if agent_result.status != AgentExecutionStatus.COMPLETED:
            blocker = agent_result.blockers[0] if agent_result.blockers else "calendar access isn't configured in this runtime yet"
            return f"I'm blocked until Google Calendar is connected. {self._humanize_calendar_blocker(blocker)}."
        payload = agent_result.evidence[0].payload if agent_result.evidence else {}
        raw_events = payload.get("events", []) if isinstance(payload, dict) else []
        if not raw_events:
            return f"Your calendar is clear {query.label}."
        selected_events = raw_events[:1] if query.mode == "next" else raw_events[:4]
        events = [self._describe_calendar_event(item) for item in selected_events]
        if query.mode == "next":
            return f"Your next calendar event is {events[0]}."
        return f"On your calendar {query.label}, you have {self._join_phrases(events)}."

    def _describe_reminder_health(self, runtime: RuntimeSnapshot) -> str:
        reminder_snapshot = self.capability_catalog.snapshot_for("reminder_scheduler")
        outbound_snapshot = self.capability_catalog.snapshot_for("slack_outbound_delivery")
        if reminder_snapshot and reminder_snapshot.status == "live" and outbound_snapshot and outbound_snapshot.status == "live":
            if runtime.failed_reminders:
                return f"Reminder delivery is live, but I do have failures to watch: {self._join_phrases(runtime.failed_reminders[:2])}."
            return "Reminder scheduling and outbound Slack delivery are both live right now."
        blockers = []
        if reminder_snapshot and reminder_snapshot.missing_config:
            blockers.extend(reminder_snapshot.missing_config)
        if outbound_snapshot and outbound_snapshot.missing_config:
            blockers.extend(outbound_snapshot.missing_config)
        if blockers:
            return f"Not fully. I still need {', '.join(dict.fromkeys(blockers))} before I can promise reminder delivery."
        return "Not fully. The reminder path still needs the scheduler and outbound delivery capabilities to be live together."

    def _describe_proactive_notifications(self, runtime: RuntimeSnapshot) -> str:
        outbound_snapshot = self.capability_catalog.snapshot_for("slack_outbound_delivery")
        reminder_snapshot = self.capability_catalog.snapshot_for("reminder_scheduler")
        if outbound_snapshot and outbound_snapshot.status == "live" and reminder_snapshot and reminder_snapshot.status == "live":
            return "Yes. In this runtime I can schedule one-time reminders and send them back to you proactively in Slack."
        return self._describe_reminder_health(runtime)

    def _describe_reminder_record(self, reminder) -> str:
        if reminder.schedule_kind == "recurring":
            description = reminder.recurrence_description or "its recurring schedule"
            return f"{reminder.summary} ({description})"
        return f"{reminder.summary} ({self._format_human_timestamp(reminder.deliver_at)})"

    def _describe_calendar_event(self, event) -> str:
        if isinstance(event, dict):
            start = datetime.fromisoformat(str(event["start"])).astimezone().strftime("%I:%M %p").lstrip("0")
            location_value = str(event.get("location") or "").strip()
            location = f" at {location_value}" if location_value else ""
            return f"{event['title']} at {start}{location}"
        start = event.start.astimezone().strftime("%I:%M %p").lstrip("0")
        location = f" at {event.location}" if getattr(event, "location", None) else ""
        return f"{event.title} at {start}{location}"

    def _humanize_calendar_blocker(self, text: str) -> str:
        cleaned = " ".join(text.strip().rstrip(".").split())
        replacements = {
            "GOOGLE_CALENDAR_TOKEN_PATH": "saved Google Calendar access",
            "GOOGLE_CALENDAR_CREDENTIALS_PATH": "Google Calendar credentials",
            "CALENDAR_REFRESH_TOKEN": "saved Google Calendar access",
            "CALENDAR_CLIENT_ID": "Google Calendar credentials",
            "CALENDAR_CLIENT_SECRET": "Google Calendar credentials",
            "GOOGLE_CALENDAR_ENABLED is false": "Google Calendar is not enabled",
            "OAuth": "Google sign-in",
            "oauth": "Google sign-in",
            "token file": "saved calendar access",
            "runtime": "workspace",
            "adapter": "connection",
            "provider": "calendar connection",
        }
        for source, target in replacements.items():
            cleaned = re.sub(source, target, cleaned, flags=re.IGNORECASE)
        return cleaned.rstrip(".") + "."

    def _format_human_timestamp(self, value: str | None) -> str:
        if not value:
            return "an unknown time"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.astimezone().strftime("%B %d at %I:%M %p").replace(" 0", " ").lstrip("0")

    def _fallback_with_recent_context(self, latest_task: Task) -> str:
        return (
            "I can help with that. If you're continuing the last thread, "
            f"{self._summarize_latest_task(latest_task, include_lead=False)}"
        )

    def _apply_response_preferences(self, reply: str, runtime: RuntimeSnapshot) -> str:
        if not runtime.assistant_recall.has_brief_preference():
            return reply
        compact = " ".join(reply.split())
        if any(marker in compact.lower() for marker in ("isn't live", "can't promise", "blocked")):
            return compact
        if len(compact) <= 140:
            return compact
        sentences = re.split(r"(?<=[.!?])\s+", compact)
        shortened = " ".join(sentences[:1]).strip()
        if len(shortened) > 170:
            parts = shortened.rstrip(".").split(", ")
            shortened = ", ".join(parts[:3]).strip() + "."
        return shortened or compact

    def _looks_like_simple_math(self, value: str) -> bool:
        compact = value.strip()
        if len(compact) > 40 or not compact:
            return False
        return bool(re.fullmatch(r"[\d\s\.\+\-\*\/%\(\)xX=]+", compact))

    def _looks_like_preference_statement(self, message: str) -> bool:
        preference_markers = ("please keep", "please be", "i prefer", "do not", "don't")
        style_markers = ("concise", "brief", "direct", "natural", "more detail", "tone")
        return any(marker in message for marker in preference_markers) and any(
            marker in message for marker in style_markers
        )

    def _looks_like_planning_discussion(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in (
                "help me plan",
                "plan the next step",
                "what should i do next",
                "what should we do next",
                "what should we work on next",
                "help me think",
                "brainstorm",
                "talk through",
                "walk me through",
                "create a plan for",
                "make a plan for",
            )
        )

    def _looks_like_next_work_question(self, message: str) -> bool:
        return any(
            phrase in message
            for phrase in ("what still needs work", "what's next for sovereign", "what is next for sovereign")
        )

    def _evaluate_simple_math(self, value: str) -> str:
        expression = value.strip().rstrip("=")
        expression = expression.replace("x", "*").replace("X", "*")
        try:
            result = eval(expression, {"__builtins__": {}}, {})
        except Exception:
            return "I couldn't confidently evaluate that."
        return str(result)

    def _context_profile_for_message(self, user_message: str) -> str:
        message = user_message.lower().strip()
        if self._looks_like_greeting(message) or self._looks_like_social_acknowledgement(message):
            return "minimal"
        if (
            self._looks_like_user_memory_question(message)
            or self._looks_like_project_memory_question(message)
            or self._looks_like_memory_lookup(message)
            or self._is_memory_follow_up_request(message)
        ):
            return "memory"
        if (
            self._looks_like_continuity_question(message)
            or self._looks_like_active_work_question(message)
            or self._looks_like_last_task_question(message)
            or message == "continue"
        ):
            return "continuity"
        return "task"

    def _select_recent_tasks(self, user_message: str, *, context_profile: str) -> list[Task]:
        if context_profile in {"minimal", "memory"}:
            return []
        tasks = self.task_store.list_tasks()
        if context_profile == "continuity":
            focus_terms = set(re.findall(r"[a-z0-9]+", user_message.lower()))
            ranked = sorted(
                tasks,
                key=lambda task: (
                    sum(1 for term in focus_terms if term in task.goal.lower()),
                    task.updated_at,
                ),
                reverse=True,
            )
            return ranked[:3]
        return tasks[:3]
