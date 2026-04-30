"""Operator continuity, runtime awareness, and proactive memory capture."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from agents.catalog import AgentCatalog, build_agent_catalog
from app.config import settings
from core.assistant_fast_path import (
    extract_name_value,
    is_greeting_message,
    is_explicit_memory_statement,
    is_forget_name_statement,
    is_thanks_message,
    is_memory_lookup,
    is_name_statement,
    is_obvious_assistant_fast_path,
    is_project_memory_question,
    is_short_personal_fact_statement,
    is_user_memory_question,
)
from core.interaction_context import get_interaction_context
from core.models import AgentExecutionStatus, AgentResult, Task, TaskStatus, utcnow
from core.model_routing import ModelRequestContext
from core.prompt_library import get_prompt_library
from core.request_trace import current_request_trace
from core.state import TaskStateStore, task_state_store
from core.system_context import SOVEREIGN_SYSTEM_CONTEXT
from integrations.openrouter_client import OpenRouterClient
from integrations.reminders.parsing import normalize_reminder_summary_text, parse_one_time_reminder_request
from memory.contacts import parse_explicit_contact_statement
from memory.contracts import PersonalOpsStore
from memory.memory_store import MemoryFact, MemoryStore, memory_store
from memory.personal_ops_store import JsonPersonalOpsStore
from memory.prompt_context import CompiledPromptContext
from memory.provider import MemoryBackend
from memory.retrieval import MemoryRetriever
from memory.safety import MEMORY_SECRET_PATTERN, looks_secret_like
from tools.capability_manifest import CapabilityCatalog, build_capability_catalog
from tools.registry import ToolRegistry, build_default_tool_registry


@dataclass
class PendingQuestion:
    """Transient continuation state for one assistant clarification."""

    original_user_intent: str
    missing_field: str
    expected_answer_type: str
    resume_target: str | None = None
    tool_or_agent: str | None = None
    pending_task_id: str | None = None
    objective_id: str | None = None
    question: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_prompt_line(self) -> str:
        target = f"; resume_target={self.resume_target}" if self.resume_target else ""
        tool = f"; tool_or_agent={self.tool_or_agent}" if self.tool_or_agent else ""
        task = f"; pending_task_id={self.pending_task_id}" if self.pending_task_id else ""
        objective = f"; objective_id={self.objective_id}" if self.objective_id else ""
        question = f"; question={self.question}" if self.question else ""
        return (
            f"original_intent={self.original_user_intent}; missing_field={self.missing_field}; "
            f"expected_answer_type={self.expected_answer_type}{target}{tool}{task}{objective}{question}"
        )


@dataclass
class ActionableObject:
    """Short-term referent the LLM can use for pronouns like it/that."""

    object_type: str
    summary: str
    object_id: str | None = None
    source: str | None = None
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())
    confidence: float = 0.8
    metadata: dict[str, object] = field(default_factory=dict)

    def to_prompt_line(self) -> str:
        object_id = f"; id={self.object_id}" if self.object_id else ""
        source = f"; source={self.source}" if self.source else ""
        return (
            f"type={self.object_type}{object_id}; summary={self.summary}{source}; "
            f"confidence={self.confidence:.2f}; timestamp={self.timestamp}"
        )


@dataclass
class ShortTermInteractionState:
    """Session-scoped state that gives the LLM continuity without becoming routing logic."""

    session_key: str
    last_assistant_question: str | None = None
    original_user_intent: str | None = None
    pending_action: dict[str, object] | None = None
    pending_question: PendingQuestion | None = None
    pending_task_id: str | None = None
    objective_id: str | None = None
    missing_slots: dict[str, object] = field(default_factory=dict)
    supplied_slots: dict[str, object] = field(default_factory=dict)
    resume_target: str | None = None
    pending_referent: ActionableObject | None = None
    pending_confirmation: dict[str, object] | None = None
    last_actionable_objects: list[ActionableObject] = field(default_factory=list)
    current_datetime: str = ""
    timezone: str = ""
    timezone_offset: str = ""
    lifecycle_state: str = "active"
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())
    recent_turn_summary: str | None = None

    def to_prompt_lines(self) -> list[str]:
        lines = [
            f"session_key: {self.session_key}",
            f"current_datetime: {self.current_datetime}",
            f"timezone: {self.timezone}",
            f"timezone_offset: {self.timezone_offset}",
            f"lifecycle_state: {self.lifecycle_state}",
        ]
        if self.last_assistant_question:
            lines.append(f"last_assistant_question: {self.last_assistant_question}")
        if self.original_user_intent:
            lines.append(f"original_user_intent: {self.original_user_intent}")
        if self.pending_task_id:
            lines.append(f"pending_task_id: {self.pending_task_id}")
        if self.objective_id:
            lines.append(f"objective_id: {self.objective_id}")
        if self.pending_question:
            lines.append(f"pending_question: {self.pending_question.to_prompt_line()}")
        if self.pending_action:
            lines.append(f"pending_action: {json.dumps(self.pending_action, ensure_ascii=True, default=str)}")
        if self.missing_slots:
            lines.append(f"missing_slots: {json.dumps(self.missing_slots, ensure_ascii=True, default=str)}")
        if self.supplied_slots:
            lines.append(f"supplied_slots: {json.dumps(self.supplied_slots, ensure_ascii=True, default=str)}")
        if self.resume_target:
            lines.append(f"resume_target: {self.resume_target}")
        if self.pending_confirmation:
            lines.append(
                f"pending_confirmation: {json.dumps(self.pending_confirmation, ensure_ascii=True, default=str)}"
            )
        if self.pending_referent:
            lines.append(f"pending_referent: {self.pending_referent.to_prompt_line()}")
        if self.last_actionable_objects:
            lines.append("last_actionable_objects:")
            lines.extend(f"- {item.to_prompt_line()}" for item in self.last_actionable_objects[:5])
        if self.recent_turn_summary:
            lines.append(f"recent_turn_summary: {self.recent_turn_summary}")
        return lines


@dataclass
class AssistantRecall:
    """Compact, high-signal memory block used to shape operator behavior."""

    user_preferences: list[str]
    project_context: list[str]
    active_open_loops: list[str]
    recent_memory: list[str]

    def to_prompt_lines(self) -> list[str]:
        return [
            "relevant_user_preferences:",
            *[f"- {item}" for item in self.user_preferences],
            "relevant_project_context:",
            *[f"- {item}" for item in self.project_context],
            "active_open_loops:",
            *[f"- {item}" for item in self.active_open_loops],
            "recent_relevant_memory:",
            *[f"- {item}" for item in self.recent_memory],
        ]

    def has_brief_preference(self) -> bool:
        return any(
            keyword in item.lower()
            for item in self.user_preferences
            for keyword in ("brief", "concise", "short")
        )


@dataclass
class RuntimeSnapshot:
    """Summarized runtime state for prompts and user-facing answers."""

    model_label: str
    llm_ready: bool
    live_tools: list[str]
    scaffolded_tools: list[str]
    configured_tools: list[str]
    planned_tools: list[str]
    active_tasks: list[str]
    recent_actions: list[str]
    open_loops: list[str]
    pending_reminders: list[str]
    delivered_reminders_today: list[str]
    failed_reminders: list[str]
    user_memory: list[str]
    project_memory: list[str]
    operational_memory: list[str]
    agent_roles: list[str]
    assistant_recall: AssistantRecall
    current_datetime: str
    timezone: str
    timezone_offset: str
    short_term_state: ShortTermInteractionState
    compiled_context: CompiledPromptContext = field(default_factory=CompiledPromptContext)
    context_profile: str = "task"

    def to_prompt_block(self) -> str:
        sections = [
            f"current_datetime: {self.current_datetime}",
            f"timezone: {self.timezone}",
            f"timezone_offset: {self.timezone_offset}",
            f"runtime_model: {self.model_label}",
            f"llm_ready: {self.llm_ready}",
            self.compiled_context.to_prompt_block(),
            "live_tools:",
            *[f"- {item}" for item in self.live_tools],
            "scaffolded_tools:",
            *[f"- {item}" for item in self.scaffolded_tools],
            "configured_but_disabled_tools:",
            *[f"- {item}" for item in self.configured_tools],
            "planned_tools:",
            *[f"- {item}" for item in self.planned_tools],
            "agent_roles:",
            *[f"- {item}" for item in self.agent_roles],
            "active_tasks:",
            *[f"- {item}" for item in self.active_tasks],
            "recent_actions:",
            *[f"- {item}" for item in self.recent_actions],
            "open_loops:",
            *[f"- {item}" for item in self.open_loops],
            "pending_reminders:",
            *[f"- {item}" for item in self.pending_reminders],
            "delivered_reminders_today:",
            *[f"- {item}" for item in self.delivered_reminders_today],
            "failed_reminders:",
            *[f"- {item}" for item in self.failed_reminders],
            "user_memory:",
            *[f"- {item}" for item in self.user_memory],
            "project_memory:",
            *[f"- {item}" for item in self.project_memory],
            "operational_memory:",
            *[f"- {item}" for item in self.operational_memory],
            "assistant_recall:",
            *self.assistant_recall.to_prompt_lines(),
            "short_term_interaction_state:",
            *self.short_term_state.to_prompt_lines(),
        ]
        return "\n".join(sections)


class OperatorContextService:
    """Shared runtime and memory coordination for the main operator."""

    short_term_ttl = timedelta(minutes=45)
    actionable_object_ttl = timedelta(hours=6)

    secret_pattern = MEMORY_SECRET_PATTERN

    def __init__(
        self,
        *,
        openrouter_client: OpenRouterClient | None = None,
        task_store: TaskStateStore | None = None,
        memory_store_instance: MemoryBackend | None = None,
        personal_ops_store_instance: PersonalOpsStore | None = None,
        tool_registry: ToolRegistry | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        agent_catalog: AgentCatalog | None = None,
    ) -> None:
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.task_store = task_store or task_state_store
        self.memory_store = memory_store_instance or memory_store
        self.personal_ops_store = personal_ops_store_instance or getattr(self.memory_store, "personal_ops", None)
        if self.personal_ops_store is None:
            self.personal_ops_store = JsonPersonalOpsStore()
        self.tool_registry = tool_registry or build_default_tool_registry()
        self.agent_catalog = agent_catalog or build_agent_catalog()
        self.capability_catalog = capability_catalog or build_capability_catalog(
            tool_registry=self.tool_registry,
            agent_catalog=self.agent_catalog,
        )
        self.memory_retriever = MemoryRetriever(self.memory_store)
        self.pending_confirmations: dict[tuple[str, str], dict[str, object]] = {}
        self.short_term_states: dict[str, ShortTermInteractionState] = {}

    def set_pending_confirmation(self, key: str, payload: dict[str, object], *, session_key: str | None = None) -> None:
        """Store transient confirmation context outside durable memory."""

        self.cleanup_short_term_state()
        scoped_key = self._scoped_transient_key(key, session_key=session_key)
        stored = dict(payload)
        stored.setdefault("_confirmation_key", key)
        stored.setdefault("_session_key", scoped_key[0])
        stored.setdefault("_created_at", utcnow().isoformat())
        self.pending_confirmations[scoped_key] = stored
        state = self.get_short_term_state(session_key=scoped_key[0])
        state.pending_confirmation = self._redact_confirmation_for_state(key, stored)
        state.lifecycle_state = "active"
        state.updated_at = utcnow().isoformat()

    def pop_pending_confirmation(self, key: str, *, session_key: str | None = None) -> dict[str, object] | None:
        """Consume a transient pending confirmation if one exists."""

        self.cleanup_short_term_state()
        scoped_key = self._scoped_transient_key(key, session_key=session_key)
        pending = self.pending_confirmations.pop(scoped_key, None)
        if pending is not None:
            state = self.get_short_term_state(session_key=scoped_key[0])
            state.pending_confirmation = None
            state.updated_at = utcnow().isoformat()
        return pending

    def get_pending_confirmation(self, key: str, *, session_key: str | None = None) -> dict[str, object] | None:
        """Return transient pending confirmation context without exposing secrets."""

        self.cleanup_short_term_state()
        return self.pending_confirmations.get(self._scoped_transient_key(key, session_key=session_key))

    def record_user_message(self, message: str, *, allow_llm_capture: bool = True) -> None:
        normalized = " ".join(message.split())
        if not normalized:
            return
        self._refresh_short_term_clock()
        self.memory_store.prune_transient_memories()
        trace = current_request_trace()
        if self._looks_secret_like(normalized):
            self.memory_store.record_action(
                "Skipped storing a secret-like user message in durable memory.",
                status="skipped",
                kind="memory_safety",
            )
            if trace is not None:
                trace.record_memory_write("memory_safety:skipped_secret")
            return
        contact = parse_explicit_contact_statement(normalized)
        if contact is not None:
            try:
                self.personal_ops_store.upsert_contact(alias=contact.alias, email=contact.email)
                self.memory_store.record_action(
                    f"Saved contact alias {contact.alias}.",
                    status="completed",
                    kind="contact_memory",
                )
                if trace is not None:
                    trace.record_memory_write("personal_ops:contact")
            except (AttributeError, ValueError):
                if trace is not None:
                    trace.record_memory_write("personal_ops:contact_skipped")
            return
        if self._should_record_session_turn(normalized):
            self.memory_store.record_turn("user", normalized)
            if trace is not None:
                trace.record_memory_write("session_turn:user")
        elif trace is not None:
            trace.record_memory_write("session_turn:skipped_trivial")

        if self._apply_forget_memory_command(normalized):
            return

        if not self._should_attempt_memory_capture(normalized):
            return

        if self._prefer_local_memory_capture(normalized) or not allow_llm_capture:
            extracted = self._extract_memories_locally(normalized)
        else:
            extracted = self._extract_memories_with_llm(normalized)
            if not extracted:
                extracted = self._extract_memories_locally(normalized)

        for fact in extracted:
            if fact.confidence < 0.45:
                continue
            if not self._is_safe_useful_fact(fact):
                if trace is not None:
                    trace.record_memory_write("fact:skipped_low_quality_or_secret")
                continue
            self.memory_store.upsert_fact(
                layer=fact.layer,
                category=fact.category,
                key=fact.key,
                value=fact.value,
                confidence=fact.confidence,
                source=fact.source,
            )
            if trace is not None:
                trace.record_memory_write(f"fact:{fact.layer}:{fact.category}")
            if fact.layer == "operational" and fact.category in {"follow_up", "open_loop"}:
                self.memory_store.upsert_open_loop(
                    key=fact.key,
                    summary=fact.value,
                    status="open",
                    source=fact.source,
                )

        reminder_summary = self._extract_reminder_summary(normalized)
        if reminder_summary:
            self.memory_store.upsert_open_loop(
                key=reminder_summary,
                summary=reminder_summary,
                status="open",
                source="user_request",
            )
            if trace is not None:
                trace.record_memory_write("open_loop:user_request")

    def record_assistant_reply(self, reply: str) -> None:
        self._capture_assistant_short_term_reply(reply)
        trace = current_request_trace()
        if self._should_record_session_turn(reply):
            self.memory_store.record_turn("assistant", reply)
            if trace is not None:
                trace.record_memory_write("session_turn:assistant")
        elif trace is not None:
            trace.record_memory_write("session_turn:skipped_trivial")

    def _should_record_session_turn(self, text: str) -> bool:
        normalized = " ".join(text.lower().strip().split())
        if not normalized:
            return False
        if is_greeting_message(normalized) or is_thanks_message(normalized):
            return False
        if normalized in {
            "how are you?",
            "how are you",
            "hi. what can i help with?",
            "i'm doing well and ready to help.",
            "noted.",
            "okay.",
        }:
            return False
        return True

    def remember_open_loop(self, summary: str, *, source: str = "conversation") -> None:
        normalized = " ".join(summary.split())
        if not normalized:
            return
        self.memory_store.upsert_open_loop(
            key=normalized,
            summary=normalized,
            status="open",
            source=source,
        )

    def reminder_summary_from_message(self, message: str) -> str | None:
        return self._extract_reminder_summary(message)

    def task_started(self, task: Task) -> None:
        self.memory_store.set_active_task(
            task_id=task.id,
            goal=task.goal,
            status=task.status,
            summary=task.summary,
        )
        self.memory_store.record_action(
            f"Started work on: {task.goal}",
            status=task.status.value,
            kind="task",
            task_id=task.id,
        )
        self.memory_store.upsert_fact(
            layer="operational",
            category="active_task",
            key=f"task:{task.id}",
            value=task.goal,
            confidence=0.9,
            source="supervisor",
        )

    def task_progress(self, task: Task, result: AgentResult) -> None:
        self.memory_store.record_action(
            result.summary,
            status=result.status.value,
            kind=result.agent,
            task_id=task.id,
        )
        if result.status == AgentExecutionStatus.BLOCKED:
            blocker = result.blockers[0] if result.blockers else result.summary
            self.memory_store.upsert_open_loop(
                key=f"task:{task.id}:blocker",
                summary=f"{task.goal}: {blocker}",
                status="blocked",
                source=result.agent,
            )

    def task_finished(self, task: Task) -> None:
        self.memory_store.set_active_task(
            task_id=task.id,
            goal=task.goal,
            status=task.status,
            summary=task.summary,
        )
        if task.status in {TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.FAILED}:
            self.memory_store.remove_active_task(task.id)

        if task.summary:
            self.memory_store.record_action(
                task.summary,
                status=task.status.value,
                kind="task_summary",
                task_id=task.id,
            )

        # Clean up transient task facts that should not persist
        key = f"task:{task.id}"
        self.memory_store.delete_fact(layer="operational", category="active_task", key=key)
        key = f"task:{task.id}:result"
        self.memory_store.delete_fact(layer="operational", category="recent_result", key=key)
        key = f"task-context:{task.id}"
        self.memory_store.delete_fact(layer="operational", category="task_context", key=key)
        key = f"task-goal:{task.id}"
        self.memory_store.delete_fact(layer="project", category="current_goal", key=key)

        # Close open loops if task completed successfully
        if task.status == TaskStatus.COMPLETED:
            self.memory_store.close_open_loop(f"task:{task.id}:blocker")
        elif task.status == TaskStatus.BLOCKED:
            self.memory_store.upsert_open_loop(
                key=f"task:{task.id}:blocked",
                summary=task.summary or f"{task.goal} is blocked.",
                status="blocked",
                source="supervisor",
            )

    def build_runtime_snapshot(
        self,
        *,
        focus_text: str | None = None,
        context_profile: str = "task",
    ) -> RuntimeSnapshot:
        self.memory_store.prune_transient_memories()
        short_term_state = self.get_short_term_state()
        trace = current_request_trace()
        if trace is not None:
            trace.record_memory_read(f"runtime_snapshot:{context_profile}")
        snapshot = self.memory_store.snapshot()
        describe_model_strategy = getattr(self.openrouter_client, "describe_model_strategy", None)
        model_label = (
            describe_model_strategy()
            if self.openrouter_client.is_configured() and callable(describe_model_strategy)
            else f"OpenRouter via {settings.openrouter_model}"
            if self.openrouter_client.is_configured()
            else "LLM provider not configured"
        )
        live_tools = self._live_tools()
        active_tasks = self._active_task_summaries(
            snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        recent_actions = self._recent_action_summaries(
            snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        open_loops = self._select_relevant_open_loops(
            snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        pending_reminders = [
            self._format_reminder_runtime_line(item)
            for item in snapshot.reminders
            if item.status == "pending"
        ][:5]
        delivered_today = [
            self._format_reminder_runtime_line(item)
            for item in snapshot.reminders
            if (item.status == "delivered" and self._is_today(item.delivered_at))
            or (item.schedule_kind == "recurring" and self._is_today(item.last_delivered_at))
        ][:5]
        failed_reminders = [
            self._format_reminder_runtime_line(item)
            for item in snapshot.reminders
            if item.status == "failed"
        ][:5]
        user_memory = [fact.value for fact in self.memory_store.list_facts("user")[:5]]
        project_memory = [fact.value for fact in self.memory_store.list_facts("project")[:6]]
        operational_memory = [
            fact.value
            for fact in self.memory_store.list_facts("operational")
            if fact.category not in {"active_task", "recent_result", "task_context"}
        ][:6]
        recall = self.build_assistant_recall(
            snapshot=snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        compiled_context = self.compile_prompt_context(
            snapshot=snapshot,
            short_term_state=short_term_state,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        grouped_lines = self.capability_catalog.grouped_lines()
        if context_profile == "minimal":
            pending_reminders = []
            delivered_today = []
            failed_reminders = []
        if context_profile in {"minimal", "memory"}:
            active_tasks = []
            recent_actions = []
        if context_profile == "memory":
            open_loops = []
            operational_memory = []
        return RuntimeSnapshot(
            model_label=model_label,
            llm_ready=self.openrouter_client.is_configured(),
            live_tools=live_tools,
            scaffolded_tools=grouped_lines.get("scaffolded", []) + [
                "durable secret storage is not implemented yet, so secrets should stay out of normal memory",
            ],
            configured_tools=grouped_lines.get("configured_but_disabled", []),
            planned_tools=grouped_lines.get("planned", []),
            active_tasks=active_tasks,
            recent_actions=recent_actions,
            open_loops=open_loops,
            pending_reminders=pending_reminders,
            delivered_reminders_today=delivered_today,
            failed_reminders=failed_reminders,
            user_memory=user_memory,
            project_memory=project_memory or self._default_project_memory(),
            operational_memory=operational_memory,
            agent_roles=self.agent_catalog.user_visible_lines(),
            assistant_recall=recall,
            current_datetime=short_term_state.current_datetime,
            timezone=short_term_state.timezone,
            timezone_offset=short_term_state.timezone_offset,
            short_term_state=short_term_state,
            compiled_context=compiled_context,
            context_profile=context_profile,
        )

    def get_short_term_state(self, *, session_key: str | None = None) -> ShortTermInteractionState:
        self.cleanup_short_term_state()
        key = session_key or self._current_session_key()
        state = self.short_term_states.get(key)
        if state is None:
            state = ShortTermInteractionState(session_key=key)
            self.short_term_states[key] = state
        now = self._runtime_now()
        state.current_datetime = now.isoformat()
        state.timezone = self._runtime_timezone_name()
        state.timezone_offset = self._format_timezone_offset(now)
        if not state.recent_turn_summary:
            turns = self.recent_conversation_turns(limit=4)
            if turns:
                state.recent_turn_summary = " | ".join(f"{role}: {content}" for role, content in turns[-4:])
        return state

    def set_pending_question(
        self,
        *,
        original_user_intent: str,
        missing_field: str,
        expected_answer_type: str,
        resume_target: str | None = None,
        tool_or_agent: str | None = None,
        pending_task_id: str | None = None,
        objective_id: str | None = None,
        question: str | None = None,
        missing_slots: dict[str, object] | None = None,
        supplied_slots: dict[str, object] | None = None,
    ) -> None:
        state = self.get_short_term_state()
        state.last_assistant_question = question or state.last_assistant_question
        state.original_user_intent = original_user_intent
        state.pending_task_id = pending_task_id
        state.objective_id = objective_id
        state.missing_slots = missing_slots or {missing_field: None}
        state.supplied_slots = supplied_slots or {}
        state.resume_target = resume_target
        state.lifecycle_state = "active"
        state.updated_at = utcnow().isoformat()
        state.pending_question = PendingQuestion(
            original_user_intent=original_user_intent,
            missing_field=missing_field,
            expected_answer_type=expected_answer_type,
            resume_target=resume_target,
            tool_or_agent=tool_or_agent,
            pending_task_id=pending_task_id,
            objective_id=objective_id,
            question=question,
        )

    def resume_pending_question_if_answer(self, message: str) -> str | None:
        state = self.get_short_term_state()
        pending = state.pending_question
        if pending is None or state.lifecycle_state != "active":
            return None
        if not self._looks_like_answer_to_pending_question(message, pending):
            return None
        resumed = pending.original_user_intent
        supplied_slots = dict(state.supplied_slots)
        supplied_slots[pending.missing_field] = message
        state.pending_question = None
        state.last_assistant_question = None
        state.supplied_slots = supplied_slots
        state.pending_action = {
            "kind": "resumed_pending_question",
            "answer": message,
            "original_user_intent": resumed,
            "missing_field": pending.missing_field,
            "resume_target": pending.resume_target,
            "tool_or_agent": pending.tool_or_agent,
            "pending_task_id": pending.pending_task_id,
            "objective_id": pending.objective_id,
            "missing_slots": dict(state.missing_slots),
            "supplied_slots": supplied_slots,
        }
        state.recent_turn_summary = (
            f"User answered pending {pending.missing_field} question with: {message}. "
            f"Resume: {resumed}"
        )
        state.updated_at = utcnow().isoformat()
        return resumed

    def register_actionable_object(
        self,
        *,
        object_type: str,
        summary: str,
        object_id: str | None = None,
        source: str | None = None,
        confidence: float = 0.8,
        metadata: dict[str, object] | None = None,
    ) -> ActionableObject:
        state = self.get_short_term_state()
        normalized_type = self._normalize_actionable_type(object_type)
        item = ActionableObject(
            object_type=normalized_type,
            summary=" ".join(summary.split()),
            object_id=object_id,
            source=source,
            confidence=confidence,
            metadata=dict(metadata or {}),
        )
        existing = [
            candidate
            for candidate in state.last_actionable_objects
            if not (
                candidate.object_type == item.object_type
                and candidate.object_id == item.object_id
                and candidate.summary.lower() == item.summary.lower()
            )
        ]
        state.last_actionable_objects = [item, *existing][:8]
        state.pending_referent = item
        state.updated_at = utcnow().isoformat()
        state.lifecycle_state = "active"
        return item

    def resolve_recent_referent(
        self,
        *,
        object_type: str | None = None,
        pronoun_text: str | None = None,
        confidence_threshold: float = 0.55,
    ) -> ActionableObject | None:
        candidates = self.resolve_recent_referents(
            object_type=object_type,
            pronoun_text=pronoun_text,
            confidence_threshold=confidence_threshold,
        )
        return candidates[0] if len(candidates) == 1 else None

    def resolve_recent_referents(
        self,
        *,
        object_type: str | None = None,
        pronoun_text: str | None = None,
        confidence_threshold: float = 0.55,
    ) -> list[ActionableObject]:
        state = self.get_short_term_state()
        candidates = [
            item
            for item in state.last_actionable_objects
            if item.confidence >= confidence_threshold and self._timestamp_age(item.timestamp) <= self.actionable_object_ttl
        ]
        if object_type is not None:
            normalized_type = self._normalize_actionable_type(object_type)
            candidates = [item for item in candidates if item.object_type == normalized_type]
        if not candidates:
            return []
        lowered = " ".join((pronoun_text or "").lower().split())
        ordinal_index = self._referent_ordinal_index(lowered)
        if ordinal_index is not None:
            ordered = list(candidates)
            if ordinal_index == -1:
                return [ordered[0]]
            return [ordered[ordinal_index]] if ordinal_index < len(ordered) else []
        if any(token in f" {lowered} " for token in (" it ", " that ", " this ")):
            if len(candidates) == 1:
                return [candidates[0]]
            best = candidates[0]
            second = candidates[1]
            if best.confidence - second.confidence >= 0.2:
                return [best]
            return candidates[:3]
        return [candidates[0]]

    def consume_short_term_state(self, *, session_key: str | None = None) -> None:
        state = self.get_short_term_state(session_key=session_key)
        state.pending_action = None
        state.pending_question = None
        state.pending_confirmation = None
        state.pending_task_id = None
        state.objective_id = None
        state.missing_slots = {}
        state.supplied_slots = {}
        state.resume_target = None
        state.last_assistant_question = None
        state.lifecycle_state = "consumed"
        state.updated_at = utcnow().isoformat()

    def fail_short_term_state(self, *, session_key: str | None = None) -> None:
        state = self.get_short_term_state(session_key=session_key)
        state.lifecycle_state = "failed"
        state.updated_at = utcnow().isoformat()

    def cleanup_short_term_state(self) -> None:
        now = utcnow()
        for key, state in list(self.short_term_states.items()):
            if self._timestamp_age(state.updated_at, now=now) <= self.short_term_ttl:
                state.last_actionable_objects = [
                    item
                    for item in state.last_actionable_objects
                    if self._timestamp_age(item.timestamp, now=now) <= self.actionable_object_ttl
                ]
                continue
            state.pending_action = None
            state.pending_question = None
            state.pending_confirmation = None
            state.pending_referent = None
            state.last_actionable_objects = []
            state.missing_slots = {}
            state.supplied_slots = {}
            state.resume_target = None
            state.lifecycle_state = "expired"
            state.updated_at = now.isoformat()
            for confirmation_key in list(self.pending_confirmations):
                if confirmation_key[0] == key:
                    self.pending_confirmations.pop(confirmation_key, None)

    def build_assistant_recall(
        self,
        *,
        snapshot=None,
        focus_text: str | None = None,
        context_profile: str = "task",
    ) -> AssistantRecall:
        current_snapshot = snapshot or self.memory_store.snapshot()
        focus = self._build_memory_focus_text(current_snapshot, focus_text)
        ranked_matches = self.recall_facts(
            focus,
            layers=self._assistant_recall_layers(context_profile),
            limit=8,
            exclude_categories=self._assistant_recall_excluded_categories(
                focus_text=focus_text,
                context_profile=context_profile,
            ),
        )
        top_preferences = [fact.value for fact in self.memory_store.list_facts("user", category="preference")[:3]]
        top_project_facts = [
            fact.value
            for fact in self.memory_store.list_facts("project")
            if fact.category != "current_goal"
        ][:4]
        preferences = self._dedupe_strings(
            [
                fact.value
                for fact in ranked_matches
                if fact.layer == "user" or fact.category == "preference"
            ]
            + top_preferences,
            limit=3,
        )
        project_context = self._dedupe_strings(
            [
                fact.value
                for fact in ranked_matches
                if fact.layer == "project"
            ]
            + top_project_facts
            or self._default_project_memory(),
            limit=4,
        )
        open_loops = self._select_relevant_open_loops(
            current_snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        recent_memory = self._dedupe_strings(
            [
                fact.value
                for fact in ranked_matches
                if fact.value not in preferences and fact.value not in project_context
            ],
            limit=4,
        )
        if context_profile in {"minimal", "memory"}:
            open_loops = []
        if context_profile == "minimal":
            recent_memory = []
        return AssistantRecall(
            user_preferences=preferences,
            project_context=project_context,
            active_open_loops=open_loops,
            recent_memory=recent_memory,
        )

    def compile_prompt_context(
        self,
        *,
        snapshot=None,
        short_term_state: ShortTermInteractionState | None = None,
        focus_text: str | None = None,
        context_profile: str = "task",
    ) -> CompiledPromptContext:
        """Build the Memory Platform v2 context split by lifetime and owner."""

        current_snapshot = snapshot or self.memory_store.snapshot()
        current_short_term = short_term_state or self.get_short_term_state()
        if context_profile == "minimal":
            return CompiledPromptContext(
                short_term_state=current_short_term.to_prompt_lines(),
            )

        core_memory = self._compile_core_memory()
        retrieved_memory = self._compile_retrieved_memory(
            current_snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        personal_ops_state = self._compile_personal_ops_state(focus_text or "")
        operational_state = self._compile_operational_state(
            current_snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )
        return CompiledPromptContext(
            core_memory=core_memory,
            retrieved_memory=retrieved_memory,
            personal_ops_state=personal_ops_state,
            operational_state=operational_state,
            short_term_state=current_short_term.to_prompt_lines(),
        )

    def _compile_core_memory(self) -> list[str]:
        user_facts = [
            fact.value
            for fact in self.memory_store.list_facts("user")
            if fact.category in {"identity", "preference", "practical_detail", "personal_fact"}
        ][:4]
        project_facts = [
            fact.value
            for fact in self.memory_store.list_facts("project")
            if fact.category not in {"current_goal", "task_context", "recent_result"}
        ][:4]
        return self._dedupe_strings(user_facts + project_facts + self._default_project_memory(), limit=8)

    def _compile_retrieved_memory(
        self,
        snapshot,
        *,
        focus_text: str | None,
        context_profile: str,
    ) -> list[str]:
        if context_profile == "minimal":
            return []
        focus = self._build_memory_focus_text(snapshot, focus_text)
        layers = ("user", "project") if context_profile == "memory" else ("user", "project", "operational")
        facts = self.recall_facts(
            focus,
            layers=layers,
            limit=6,
            exclude_categories=("active_task", "recent_result", "task_context")
            if context_profile == "memory"
            else ("task_context",),
        )
        return self._dedupe_strings([fact.value for fact in facts], limit=6)

    def _compile_operational_state(
        self,
        snapshot,
        *,
        focus_text: str | None,
        context_profile: str,
    ) -> list[str]:
        if context_profile in {"minimal", "memory"}:
            return []
        lines: list[str] = []
        lines.extend(f"active_task: {item}" for item in self._active_task_summaries(
            snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        ))
        lines.extend(f"open_loop: {item}" for item in self._select_relevant_open_loops(
            snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        ))
        lines.extend(f"recent_action: {item}" for item in self._recent_action_summaries(
            snapshot,
            focus_text=focus_text,
            context_profile=context_profile,
        )[:4])
        for reminder in snapshot.reminders:
            if reminder.status in {"pending", "failed"}:
                lines.append(f"reminder: {self._format_reminder_runtime_line(reminder)}")
            if len(lines) >= 10:
                break
        return self._dedupe_strings(lines, limit=10)

    def _compile_personal_ops_state(self, focus_text: str) -> list[str]:
        if not self._should_include_personal_ops_state(focus_text):
            return []
        try:
            snapshot = self.personal_ops_store.snapshot()
        except Exception:
            return []

        focus_terms = set(re.findall(r"[a-z0-9]+", focus_text.lower()))
        list_lines: list[str] = []
        if any(phrase in focus_text.lower() for phrase in ("what lists", "all lists", "my lists")):
            list_lines.extend(f"list: {record.name} ({len(record.items)} items)" for record in snapshot.lists[:8])
        else:
            for record in snapshot.lists:
                name_terms = set(re.findall(r"[a-z0-9]+", record.name.lower()))
                item_terms = set()
                for item in record.items[:8]:
                    item_terms.update(re.findall(r"[a-z0-9]+", item.text.lower()))
                if not focus_terms.intersection(name_terms | item_terms):
                    continue
                items = ", ".join(item.text for item in record.items[:8])
                suffix = f": {items}" if items else ": empty"
                list_lines.append(f"list: {record.name}{suffix}")

        routine_lines = []
        if "routine" in focus_text.lower() or "routines" in focus_text.lower():
            routine_lines = [
                f"routine: {item.title}; status={item.status}; execution_live={item.execution_live}"
                for item in snapshot.proactive_routines[:5]
            ]
        return self._dedupe_strings(list_lines + routine_lines, limit=8)

    def _should_include_personal_ops_state(self, focus_text: str) -> bool:
        lowered = focus_text.lower()
        if not lowered:
            return False
        if any(token in lowered for token in ("list", "lists", "note", "notes", "routine", "routines")):
            return True
        try:
            list_names = [record.name.lower() for record in self.personal_ops_store.list_lists()]
        except Exception:
            return False
        return any(name and name in lowered for name in list_names)

    def prompt_context_block(self) -> str:
        runtime = self.build_runtime_snapshot()
        return "\n".join(
            [
                SOVEREIGN_SYSTEM_CONTEXT.to_prompt_block(),
                get_prompt_library().read_many(["instructions/scheduling_agent.md"]),
                self.capability_catalog.summary_block(),
                self.capability_catalog.policy_block(),
                "runtime_state:",
                runtime.to_prompt_block(),
            ]
        )

    def _scoped_transient_key(self, key: str, *, session_key: str | None = None) -> tuple[str, str]:
        return (session_key or self._current_session_key(), key)

    def _redact_confirmation_for_state(self, key: str, payload: dict[str, object]) -> dict[str, object]:
        redacted: dict[str, object] = {"key": key, "session_key": payload.get("_session_key")}
        for field in ("action", "event_id", "title", "updates", "message_id", "query", "to", "subject"):
            if field in payload:
                redacted[field] = payload[field]
        return redacted

    def _current_session_key(self) -> str:
        interaction = get_interaction_context()
        if interaction is None:
            return "local:no-channel:no-user"
        return ":".join(
            [
                interaction.source or "local",
                interaction.channel_id or "no-channel",
                interaction.user_id or "no-user",
            ]
        )

    def _runtime_timezone_name(self) -> str:
        return settings.scheduler_timezone or "America/New_York"

    def _runtime_now(self) -> datetime:
        try:
            tz = ZoneInfo(self._runtime_timezone_name())
        except Exception:
            tz = datetime.now().astimezone().tzinfo or timezone.utc
        return datetime.now(tz)

    def _format_timezone_offset(self, value: datetime) -> str:
        offset = value.utcoffset() or timedelta(0)
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        hours, minutes = divmod(total_minutes, 60)
        return f"{sign}{hours:02d}:{minutes:02d}"

    def _refresh_short_term_clock(self) -> None:
        state = self.get_short_term_state()
        now = self._runtime_now()
        state.current_datetime = now.isoformat()
        state.timezone = self._runtime_timezone_name()
        state.timezone_offset = self._format_timezone_offset(now)
        state.updated_at = utcnow().isoformat()

    def _capture_assistant_short_term_reply(self, reply: str) -> None:
        normalized = " ".join(reply.split())
        if not normalized:
            return
        state = self.get_short_term_state()
        state.recent_turn_summary = normalized[:500]
        state.updated_at = utcnow().isoformat()
        if normalized.endswith("?"):
            state.last_assistant_question = normalized

    def _looks_like_answer_to_pending_question(self, message: str, pending: PendingQuestion) -> bool:
        normalized = " ".join(message.lower().strip().split())
        if not normalized:
            return False
        if normalized in {"yes", "yep", "yeah", "confirm", "confirmed", "no", "nope"}:
            return True
        expected = pending.expected_answer_type.lower()
        missing = pending.missing_field.lower()
        if any(token in expected or token in missing for token in ("date", "time", "datetime")):
            if normalized.endswith("?") or normalized.startswith(
                ("what ", "who ", "why ", "how ", "when ", "where ", "can you ", "could you ")
            ):
                return False
            return bool(
                re.search(
                    r"\b(today|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                    r"january|february|march|april|may|june|july|august|september|october|november|december|"
                    r"\d{1,2}\s*(?:am|pm)|\d{1,2}[:/]\d{1,2}|\d{4})\b",
                    normalized,
                )
            )
        if expected in {"text", "string", "description"} or "field" in missing:
            if normalized.startswith(
                (
                    "i might ",
                    "i may ",
                    "maybe ",
                    "can you ",
                    "could you ",
                    "would you ",
                    "what ",
                    "who ",
                    "why ",
                    "how ",
                    "when ",
                    "where ",
                    "remind me ",
                    "open ",
                    "build ",
                    "refactor ",
                    "fix ",
                    "write ",
                    "create ",
                    "hi",
                    "hello",
                    "hey",
                )
            ):
                return False
            if normalized.endswith("?"):
                return False
            return len(normalized.split()) <= 20
        return len(normalized.split()) <= 12

    def _normalize_actionable_type(self, object_type: str) -> str:
        normalized = object_type.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "calendar": "calendar_event",
            "event": "calendar_event",
            "calendar_event": "calendar_event",
            "email": "gmail_message",
            "gmail": "gmail_message",
            "gmail_message": "gmail_message",
            "message": "gmail_message",
            "file": "file_output",
            "file_output": "file_output",
            "browser": "browser",
            "reminder": "reminder",
        }
        return aliases.get(normalized, normalized)

    def _referent_ordinal_index(self, lowered: str) -> int | None:
        padded = f" {lowered} "
        if " last one " in padded or " the last " in padded:
            return -1
        ordinals = {
            " first ": 0,
            " 1st ": 0,
            " second ": 1,
            " 2nd ": 1,
            " third ": 2,
            " 3rd ": 2,
        }
        for token, index in ordinals.items():
            if token in padded:
                return index
        return None

    def _timestamp_age(self, value: str, *, now: datetime | None = None) -> timedelta:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return timedelta.max
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or utcnow()
        return max(current - parsed.astimezone(timezone.utc), timedelta(0))

    def _live_tools(self) -> list[str]:
        live, _ = self.capability_catalog.user_visible_lines()
        return live

    def _active_task_summaries(
        self,
        snapshot,
        *,
        focus_text: str | None = None,
        context_profile: str = "task",
    ) -> list[str]:
        if context_profile in {"minimal", "memory"}:
            return []
        if snapshot.active_tasks:
            ranked_tasks = self._rank_text_items(
                snapshot.active_tasks,
                focus_text=focus_text,
                context_profile=context_profile,
                text_getter=lambda item: item.goal,
            )
            summaries: list[str] = []
            for item in ranked_tasks[:5]:
                objective_state = getattr(item, "objective_state", None)
                if objective_state is not None:
                    summaries.append(
                        f"{item.goal} ({item.status}; {objective_state.escalation_level.value}; stage={objective_state.stage.value})"
                    )
                else:
                    summaries.append(f"{item.goal} ({item.status})")
            return summaries
        live_tasks = [
            task for task in self.task_store.list_tasks() if task.status not in {TaskStatus.COMPLETED}
        ]
        live_tasks = self._rank_text_items(
            live_tasks,
            focus_text=focus_text,
            context_profile=context_profile,
            text_getter=lambda task: task.goal,
        )
        summaries: list[str] = []
        for task in live_tasks[:5]:
            if task.objective_state is not None:
                summaries.append(
                    f"{task.goal} ({task.status.value}; {task.objective_state.escalation_level.value}; stage={task.objective_state.stage.value})"
                )
            else:
                summaries.append(f"{task.goal} ({task.status.value})")
        return summaries

    def _default_project_memory(self) -> list[str]:
        return [
            "Project Sovereign should feel like one CEO-style operator backed by hidden subagents.",
            "It should be LLM-driven rather than dominated by hardcoded routing logic.",
            "Memory should capture project state, user preferences, prior work, and open loops automatically.",
            "Slack is the first-class interface, while unfinished capabilities should be described honestly.",
        ]

    def _build_memory_focus_text(self, snapshot, focus_text: str | None) -> str:
        if focus_text:
            return focus_text
        recent_turns = [turn.content for turn in snapshot.session_turns[-2:] if turn.role == "user"]
        if recent_turns:
            return " ".join(recent_turns)
        if snapshot.open_loops:
            return " ".join(item.summary for item in snapshot.open_loops[:2])
        return "Project Sovereign user preferences project priorities open loops"

    def recall_facts(
        self,
        query: str,
        *,
        layers: tuple[str, ...] = ("user", "project", "operational"),
        limit: int = 5,
        include_categories: tuple[str, ...] | None = None,
        exclude_categories: tuple[str, ...] = (),
    ) -> list[MemoryFact]:
        trace = current_request_trace()
        if trace is not None:
            trace.record_memory_read("recall_facts")
        facts = self.memory_store.search_facts(query, layers=layers)
        filtered: list[MemoryFact] = []
        include = set(include_categories or ())
        exclude = set(exclude_categories)
        for fact in facts:
            if include and fact.category not in include:
                continue
            if fact.category in exclude:
                continue
            filtered.append(fact)
            if len(filtered) >= limit:
                break
        return filtered

    def recent_user_turns(self, *, limit: int = 5) -> list[str]:
        trace = current_request_trace()
        if trace is not None:
            trace.record_memory_read("recent_user_turns")
        snapshot = self.memory_store.snapshot()
        turns = [turn.content for turn in snapshot.session_turns if turn.role == "user"]
        return turns[-limit:]

    def recent_conversation_turns(self, *, limit: int = 6) -> list[tuple[str, str]]:
        trace = current_request_trace()
        if trace is not None:
            trace.record_memory_read("recent_conversation_turns")
        snapshot = self.memory_store.snapshot()
        turns = snapshot.session_turns[-limit:]
        return [(turn.role, turn.content) for turn in turns]

    def _select_relevant_open_loops(
        self,
        snapshot,
        *,
        focus_text: str | None = None,
        context_profile: str = "task",
    ) -> list[str]:
        if context_profile in {"minimal", "memory"}:
            return []
        active_loops = [item for item in snapshot.open_loops if item.status != "closed"]
        if not active_loops:
            return []
        focus_terms = set(re.findall(r"[a-z0-9]+", (focus_text or "").lower()))
        ranked = sorted(
            active_loops,
            key=lambda item: (
                self._open_loop_priority(
                    item,
                    focus_terms=focus_terms,
                    context_profile=context_profile,
                ),
            ),
            reverse=True,
        )
        filtered = [
            item.summary
            for item in ranked
            if self._should_include_open_loop(
                item,
                focus_terms=focus_terms,
                context_profile=context_profile,
            )
        ]
        return self._dedupe_strings(filtered, limit=3)

    def _recent_action_summaries(
        self,
        snapshot,
        *,
        focus_text: str | None = None,
        context_profile: str = "task",
    ) -> list[str]:
        if context_profile in {"minimal", "memory"}:
            return []
        actions = list(snapshot.recent_actions)[::-1]
        if context_profile == "continuity":
            actions = self._rank_text_items(
                actions,
                focus_text=focus_text,
                context_profile=context_profile,
                text_getter=lambda item: item.summary,
            )
        return [item.summary for item in actions[:5]]

    def _rank_text_items(
        self,
        items: list[object],
        *,
        focus_text: str | None,
        context_profile: str,
        text_getter,
    ) -> list[object]:
        if context_profile == "task" or not focus_text:
            return list(items)
        focus_terms = set(re.findall(r"[a-z0-9]+", focus_text.lower()))
        ranked = sorted(
            items,
            key=lambda item: (
                self._term_overlap(text_getter(item), focus_terms),
                getattr(item, "updated_at", getattr(item, "created_at", "")),
            ),
            reverse=True,
        )
        if any(self._term_overlap(text_getter(item), focus_terms) for item in ranked):
            return ranked
        return list(items)

    def _term_overlap(self, text: str, focus_terms: set[str]) -> int:
        lowered = text.lower()
        return sum(1 for term in focus_terms if term in lowered)

    def _open_loop_priority(
        self,
        item,
        *,
        focus_terms: set[str],
        context_profile: str,
    ) -> tuple[bool, int, str]:
        return (
            item.status == "blocked",
            self._term_overlap(item.summary, focus_terms),
            item.updated_at,
        )

    def _should_include_open_loop(
        self,
        item,
        *,
        focus_terms: set[str],
        context_profile: str,
    ) -> bool:
        if context_profile == "task":
            return True
        overlap = self._term_overlap(item.summary, focus_terms)
        if overlap > 0:
            return True
        if context_profile == "continuity":
            return self._timestamp_age_hours(item.updated_at) <= 72
        return False

    def _timestamp_age_hours(self, value: str) -> float:
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            return 10_000.0
        return max((utcnow() - timestamp).total_seconds() / 3600.0, 0.0)

    def _dedupe_strings(self, items: list[str], *, limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = " ".join(item.split())
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            seen.add(key)
            cleaned.append(normalized)
            if len(cleaned) >= limit:
                break
        return cleaned

    def _format_reminder_runtime_line(self, reminder) -> str:
        summary = reminder.summary.rstrip(".")
        if reminder.schedule_kind == "recurring":
            delivered_suffix = ""
            if reminder.last_delivered_at:
                delivered_suffix = f"; last sent at {self._format_timestamp(reminder.last_delivered_at)}"
            schedule_text = reminder.recurrence_description or "recurring"
            return (
                f"{summary} ({schedule_text}; next at {self._format_timestamp(reminder.deliver_at)}"
                f"{delivered_suffix})"
            )
        if reminder.status == "delivered" and reminder.delivered_at:
            return f"{summary} (delivered at {self._format_timestamp(reminder.delivered_at)})"
        if reminder.status == "failed" and reminder.failed_at:
            failure = f"; {reminder.failure_reason}" if reminder.failure_reason else ""
            return f"{summary} (failed at {self._format_timestamp(reminder.failed_at)}{failure})"
        return f"{summary} (scheduled for {self._format_timestamp(reminder.deliver_at)})"

    def _format_timestamp(self, value: str | None) -> str:
        if not value:
            return "an unknown time"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.astimezone().strftime("%I:%M %p").lstrip("0")

    def _is_today(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed.astimezone().date() == utcnow().astimezone().date()

    def _extract_memories_with_llm(self, message: str) -> list[MemoryFact]:
        if not self.openrouter_client.is_configured():
            return []

        prompt = (
            f"{get_prompt_library().read_many(['instructions/operator_identity.md', 'instructions/memory_agent.md'])}\n"
            "Extract only durable, useful memory from this user message.\n"
            "Do not store credentials, tokens, passwords, API keys, or raw secrets.\n"
            "Return strict JSON with the shape "
            '{"facts":[{"layer":"user","category":"preference","key":"...","value":"...","confidence":0.81}]}.'
            "\n"
            "Valid layers: user, project, operational.\n"
            "Capture only stable preferences, persistent project priorities, open loops, or meaningful context.\n"
            f"Message: {message}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt="You extract concise durable memory for an AI operator. Return only JSON.",
                label="memory_extract",
                context=ModelRequestContext(
                    intent_label="memory",
                    request_mode="answer",
                    selected_lane="memory",
                    selected_agent="memory_agent",
                    task_complexity="low",
                    risk_level="low",
                    requires_tool_use=False,
                    requires_review=False,
                    evidence_quality="high",
                    user_visible_latency_sensitivity="high",
                    cost_sensitivity="high",
                ),
            )
            payload = json.loads(response)
            facts_payload = payload.get("facts", [])
            if not isinstance(facts_payload, list):
                return []
            facts: list[MemoryFact] = []
            for item in facts_payload[:8]:
                layer = str(item.get("layer", "")).strip().lower()
                category = str(item.get("category", "")).strip().lower() or "context"
                key = str(item.get("key", "")).strip()
                value = str(item.get("value", "")).strip()
                try:
                    confidence = float(item.get("confidence", 0.5))
                except (TypeError, ValueError):
                    confidence = 0.5
                if layer not in {"user", "project", "operational"}:
                    continue
                if not key or not value or self._looks_secret_like(value):
                    continue
                facts.append(
                    MemoryFact(
                        layer=layer,
                        category=category,
                        key=key,
                        value=value,
                        confidence=confidence,
                        source="llm_extraction",
                    )
                )
            return facts
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return []

    def _extract_memories_locally(self, message: str) -> list[MemoryFact]:
        normalized = " ".join(message.split())
        lowered = normalized.lower()
        facts: list[MemoryFact] = []
        name_fact = self._extract_name_fact(normalized, lowered)
        if name_fact is not None:
            facts.append(name_fact)
        style_fact = self._extract_response_style_fact(normalized, lowered)
        if style_fact is not None:
            facts.append(style_fact)

        parking_fact = self._extract_parking_fact(normalized, lowered)
        if parking_fact is not None:
            facts.append(parking_fact)

        project_identity_fact = self._extract_project_identity_fact(normalized, lowered)
        if project_identity_fact is not None:
            facts.append(project_identity_fact)

        priority_fact = self._extract_priority_fact(normalized, lowered)
        if priority_fact is not None:
            facts.append(priority_fact)

        if any(token in lowered for token in ("working on", "current goal", "next goal", "focus on")):
            facts.append(
                MemoryFact(
                    layer="operational",
                    category="goal",
                    key="operational:current_goal",
                    value=self._clean_memory_prefix(normalized).rstrip("."),
                    confidence=0.68,
                    source="heuristic",
                )
            )

        open_loop_fact = self._extract_open_loop_fact(normalized, lowered)
        if open_loop_fact is not None:
            facts.append(open_loop_fact)

        if any(token in lowered for token in ("remind me", "later", "follow up", "check this")):
            facts.append(
                MemoryFact(
                    layer="operational",
                    category="follow_up",
                    key=f"follow_up:{hash(lowered)}",
                    value=self._clean_memory_prefix(normalized).rstrip("."),
                    confidence=0.73,
                    source="heuristic",
                )
            )
        generic_explicit_fact = self._extract_explicit_memory_fact(normalized, lowered, facts)
        if generic_explicit_fact is not None:
            facts.append(generic_explicit_fact)
        return facts

    def _extract_reminder_summary(self, message: str) -> str | None:
        lowered = message.lower()
        if "remind me" not in lowered:
            return None
        parsed = parse_one_time_reminder_request(
            message,
            timezone_name=settings.scheduler_timezone,
        )
        if parsed is not None:
            return parsed.summary
        reminder = re.sub(
            r"^.*?remind me(?:\s+later)?(?:\s+(?:to|that|about))?\s+",
            "",
            lowered,
        ).strip(" .")
        cleaned = normalize_reminder_summary_text(reminder)
        if cleaned:
            return cleaned
        return "follow up on the user's earlier request"

    def _looks_secret_like(self, text: str) -> bool:
        return looks_secret_like(text)

    def _is_safe_useful_fact(self, fact: MemoryFact) -> bool:
        if fact.layer not in {"user", "project", "operational"}:
            return False
        if self._looks_secret_like(f"{fact.key} {fact.value}"):
            return False
        normalized = " ".join(fact.value.lower().split())
        if not normalized or len(normalized) < 8:
            return False
        junk_markers = {
            "hi",
            "hello",
            "thanks",
            "thank you",
            "okay",
            "ok",
            "lol",
        }
        if normalized.strip(" .!?") in junk_markers:
            return False
        useful_categories = {
            "identity",
            "preference",
            "personal_fact",
            "practical_detail",
            "current_priority",
            "priority",
            "decision",
            "constraint",
            "goal",
            "current_goal",
            "follow_up",
            "open_loop",
            "explicit_memory",
        }
        if fact.category in useful_categories:
            return True
        return fact.layer == "project" and any(
            token in normalized
            for token in ("project sovereign", "sovereign", "operator", "memory", "priority", "architecture")
        )

    def _should_attempt_memory_capture(self, message: str) -> bool:
        lowered = message.lower().strip()
        if "remind me" in lowered or "follow up" in lowered:
            return True
        if lowered.endswith("?"):
            return False
        if is_name_statement(lowered) or is_short_personal_fact_statement(lowered):
            return True
        capture_markers = (
            "i prefer",
            "please keep",
            "please be",
            "do not",
            "don't",
            "remember that",
            "remember where",
            "remember i ",
            "remember my ",
            "remember this project",
            "update that",
            "actually, i ",
            "actually i ",
            "i parked",
            "project sovereign should",
            "project sovereign needs",
            "we are working on",
            "we're working on",
            "focus on",
            "priority",
            "prioritize",
            "project priority",
            "sovereign priority",
            "next priority",
            "current goal",
            "next goal",
            "still need to",
            "still needs",
            "open loop",
            "blocked on",
        )
        return any(marker in lowered for marker in capture_markers)

    def _prefer_local_memory_capture(self, message: str) -> bool:
        lowered = message.lower().strip()
        return (
            is_name_statement(lowered)
            or is_forget_name_statement(lowered)
            or is_explicit_memory_statement(lowered)
            or is_short_personal_fact_statement(lowered)
            or is_user_memory_question(lowered)
            or is_project_memory_question(lowered)
            or is_memory_lookup(lowered)
            or is_obvious_assistant_fast_path(lowered)
        )

    def _apply_forget_memory_command(self, message: str) -> bool:
        lowered = message.lower().strip()
        if not is_forget_name_statement(lowered):
            return False
        self.memory_store.delete_fact(layer="user", key="user:name", category="identity")
        trace = current_request_trace()
        if trace is not None:
            trace.record_memory_write("delete_fact:user:user:name")
        return True

    def _assistant_recall_layers(self, context_profile: str) -> tuple[str, ...]:
        if context_profile == "memory":
            return ("user", "project")
        if context_profile == "continuity":
            return ("project", "operational")
        return ("user", "project", "operational")

    def _assistant_recall_excluded_categories(
        self,
        *,
        focus_text: str | None,
        context_profile: str,
    ) -> tuple[str, ...]:
        if context_profile == "memory":
            return ("active_task", "recent_result", "task_context", "current_goal")
        if context_profile == "continuity":
            lowered = (focus_text or "").lower()
            if any(token in lowered for token in ("task", "working", "doing", "last")):
                return ("recent_result", "task_context")
            return ("active_task", "recent_result", "task_context", "current_goal")
        return ("task_context",)

    def _extract_response_style_fact(self, normalized: str, lowered: str) -> MemoryFact | None:
        if any(token in lowered for token in ("concise", "brief", "short")):
            return MemoryFact(
                layer="user",
                category="preference",
                key="user:response_style",
                value="You prefer concise answers.",
                confidence=0.82,
                source="heuristic",
            )
        if any(token in lowered for token in ("more detailed", "more detail", "go deeper")):
            value = "For this project, you want more detailed answers."
            if "for this project" not in lowered and "project sovereign" not in lowered:
                value = "You want more detailed answers when the work needs it."
            return MemoryFact(
                layer="user",
                category="preference",
                key="user:response_style",
                value=value,
                confidence=0.82,
                source="heuristic",
            )
        if "natural" in lowered or "direct" in lowered or "less jargon" in lowered:
            return MemoryFact(
                layer="user",
                category="preference",
                key="user:response_tone",
                value="You want responses to stay natural and direct.",
                confidence=0.74,
                source="heuristic",
            )
        return None

    def _extract_name_fact(self, normalized: str, lowered: str) -> MemoryFact | None:
        del lowered
        name = extract_name_value(normalized)
        if not name:
            return None
        return MemoryFact(
            layer="user",
            category="identity",
            key="user:name",
            value=f"Your name is {name}.",
            confidence=0.96,
            source="heuristic",
        )

    def _extract_parking_fact(self, normalized: str, lowered: str) -> MemoryFact | None:
        parking_patterns = (
            r"(?:remember that|update that:?|actually,?|i remember that)?\s*i (?:actually )?parked (.+)",
            r"(?:remember that|update that:?|actually,?)\s*the car is parked (.+)",
        )
        for pattern in parking_patterns:
            match = re.search(pattern, lowered)
            if not match:
                continue
            detail = match.group(1).strip(" .")
            if not detail or detail in {"where i parked", "parked"}:
                return None
            if not detail.startswith("on ") and not detail.startswith("near ") and not detail.startswith("at "):
                detail = f"at {detail}"
            return MemoryFact(
                layer="user",
                category="practical_detail",
                key="user:parking_location",
                value=f"You parked {detail}.",
                confidence=0.88,
                source="heuristic",
            )
        return None

    def _extract_project_identity_fact(self, normalized: str, lowered: str) -> MemoryFact | None:
        if ("project sovereign" in lowered or "this project" in lowered) and any(
            token in lowered for token in ("should feel like", "should be", "needs to feel like", "must feel like")
        ):
            return MemoryFact(
                layer="project",
                category="identity",
                key="project:operator_feel",
                value=self._clean_memory_prefix(normalized).rstrip(".") + ".",
                confidence=0.8,
                source="heuristic",
            )
        return None

    def _extract_priority_fact(self, normalized: str, lowered: str) -> MemoryFact | None:
        priority_patterns = (
            r"(?:remember that )?(?:my|the)?\s*project priority(?: for (?:this project|project sovereign|sovereign))?\s+is ([^.!?]+)",
            r"(?:remember that )?(?:project sovereign|sovereign)(?:'s)?\s+(?:next |current )?priority\s+is ([^.!?]+)",
            r"([^.!?]+?)\s+is\s+the\s+next\s+priority\s+(?:in|for)\s+(?:project sovereign|sovereign)(?:\s+before\s+[^.!?]+)?",
            r"(?:remember that )?my next priority is ([^.!?]+)",
            r"(?:remember that )?(?:the )?current priority(?: for (?:this project|project sovereign))? is ([^.!?]+)",
            r"(?:update that:?|actually,?)\s*(?:the )?priority(?: is)? ([^.!?]+)",
        )
        for pattern in priority_patterns:
            match = re.search(pattern, lowered)
            if not match:
                continue
            detail = match.group(1).strip(" .")
            if not detail:
                return None
            if " before " in lowered and "next priority" in lowered and "project sovereign" in lowered:
                before_suffix = normalized.lower().split(" before ", 1)[1].strip(" .")
                detail = f"{detail} before {before_suffix}"
            return MemoryFact(
                layer="project",
                category="current_priority",
                key="project:current_priority",
                value=f"The current project priority is {detail}.",
                confidence=0.84,
                source="heuristic",
            )
        return None

    def _extract_open_loop_fact(self, normalized: str, lowered: str) -> MemoryFact | None:
        patterns = (
            r"(?:remember that )?we still need to ([^.!?]+)",
            r"(?:remember that )?(?:project sovereign|sovereign) still needs ([^.!?]+)",
            r"(?:remember that )?(?:the )?open loop is ([^.!?]+)",
            r"(?:remember that )?(?:we are|we're) blocked on ([^.!?]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if not match:
                continue
            detail = match.group(1).strip(" .")
            if not detail or self._looks_secret_like(detail):
                return None
            summary = self._clean_memory_prefix(normalized).rstrip(".")
            return MemoryFact(
                layer="operational",
                category="open_loop",
                key=f"open_loop:{abs(hash(detail))}",
                value=summary,
                confidence=0.78,
                source="heuristic",
            )
        return None

    def _extract_explicit_memory_fact(
        self,
        normalized: str,
        lowered: str,
        existing_facts: list[MemoryFact],
    ) -> MemoryFact | None:
        if existing_facts or not lowered.startswith("remember that "):
            return None
        detail = normalized[len("remember that ") :].strip().rstrip(".")
        if not detail or len(detail.split()) > 14 or self._looks_secret_like(detail):
            return None
        if any(token in detail.lower() for token in ("project sovereign", "sovereign", "this project")):
            return MemoryFact(
                layer="project",
                category="explicit_memory",
                key=f"project:explicit:{abs(hash(detail.lower()))}",
                value=detail.rstrip(".") + ".",
                confidence=0.74,
                source="heuristic",
            )
        return MemoryFact(
            layer="user",
            category="explicit_memory",
            key=f"user:explicit:{abs(hash(detail.lower()))}",
            value=f"You told me: {detail}.",
            confidence=0.72,
            source="heuristic",
        )

    def _clean_memory_prefix(self, text: str) -> str:
        cleaned = re.sub(
            r"^(remember that|remember|update that:?|actually,?|keep that in mind)\s+",
            "",
            text.strip(),
            flags=re.IGNORECASE,
        )
        return " ".join(cleaned.split())


operator_context = OperatorContextService()
