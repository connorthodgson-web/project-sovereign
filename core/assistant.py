"""Assistant-layer request interpretation and user-facing response composition."""

from __future__ import annotations

import json
from pathlib import Path
import re

import httpx

from app.config import settings
from core.assistant_fast_path import (
    is_explicit_memory_statement,
    is_forget_name_statement,
    is_greeting_message,
    is_memory_follow_up_phrase,
    is_memory_lookup,
    is_name_statement,
    is_obvious_assistant_fast_path,
    is_project_memory_question,
    is_short_personal_fact_statement,
    is_thanks_message,
    is_user_memory_question,
)
from agents.scheduling_agent import looks_like_calendar_read_request, looks_like_google_tasks_request
from core.browser_requests import extract_obvious_browser_request
from core.conversation import ConversationalHandler
from core.context_assembly import ContextAssembler
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    AssistantDecision,
    ChatResponse,
    ExecutionEscalation,
    FileEvidence,
    GoalEvaluation,
    ObjectiveStage,
    RequestMode,
    Task,
    TaskOutcome,
    TaskStatus,
    ToolEvidence,
)
from core.operator_context import OperatorContextService, operator_context
from core.personal_ops_intent import looks_like_personal_ops_request
from core.request_trace import current_request_trace
from core.state import TaskStateStore
from core.system_context import SOVEREIGN_SYSTEM_CONTEXT
from core.model_routing import ModelRequestContext
from integrations.openrouter_client import OpenRouterClient
from memory.contacts import parse_explicit_contact_statement
from tools.tool_policy import build_tool_cost_policy


class AssistantLayer:
    """Thin assistant layer that decides handling mode and shapes replies."""

    def __init__(
        self,
        *,
        openrouter_client: OpenRouterClient | None = None,
        conversational_handler: ConversationalHandler | None = None,
        task_store: TaskStateStore | None = None,
        operator_context_service: OperatorContextService | None = None,
        context_assembler: ContextAssembler | None = None,
    ) -> None:
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.operator_context = operator_context_service or operator_context
        self.context_assembler = context_assembler or ContextAssembler(
            operator_context_service=self.operator_context
        )
        self.conversational_handler = conversational_handler or ConversationalHandler(
            openrouter_client=self.openrouter_client,
            task_store=task_store,
            operator_context_service=self.operator_context,
            context_assembler=self.context_assembler,
        )

    def decide(self, user_message: str) -> AssistantDecision:
        pending_browser_decision = self._pending_browser_continuation_decision(user_message)
        if pending_browser_decision is not None:
            return pending_browser_decision
        guardrail_decision = self._guardrail_decision(user_message)
        if guardrail_decision is not None:
            return guardrail_decision
        fast_path_decision = self._trivial_assistant_fast_path_decision(user_message)
        if fast_path_decision is not None:
            trace = current_request_trace()
            if trace is not None:
                trace.set_path("assistant_fast_path")
            return fast_path_decision
        obvious_fast_path = self._obvious_assistant_fast_path_decision(user_message)
        if obvious_fast_path is not None:
            trace = current_request_trace()
            if trace is not None:
                trace.set_path("assistant_fast_path")
            return obvious_fast_path
        message = user_message.lower().strip()
        normalized = self._normalize_phrase_text(message)
        capability_question = self._capability_question_decision(message, normalized)
        if capability_question is not None:
            return capability_question
        ambiguous_decision = self._ambiguous_request_decision(message, normalized)
        if ambiguous_decision is not None:
            return ambiguous_decision
        browser_file_decision = self._browser_file_objective_decision(message)
        if browser_file_decision is not None:
            return browser_file_decision
        llm_decision = self._decide_with_llm(user_message)
        if llm_decision is not None:
            return self._normalize_llm_decision(llm_decision)
        return self._decide_deterministically(user_message)

    def decide_without_llm(self, user_message: str) -> AssistantDecision:
        """Return a fast local mode decision for transport-level UX choices."""
        return self._decide_transport_locally(user_message)

    def _pending_browser_continuation_decision(self, user_message: str) -> AssistantDecision | None:
        normalized = " ".join(user_message.lower().strip().split())
        if normalized not in {"continue", "done", "ready", "try again", "retry"}:
            return None
        state = self.operator_context.get_short_term_state()
        pending = state.pending_question
        if pending is None or pending.resume_target != "browser_continuation":
            return None
        return AssistantDecision(
            mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
            reasoning="The user is resuming a pending browser human-in-loop step.",
            should_use_tools=True,
            intent_label="browser_continuation",
        )

    def build_answer_response(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        return self.conversational_handler.handle(user_message, decision)

    def compose_task_response(
        self,
        task: Task,
        decision: AssistantDecision,
        outcome: TaskOutcome,
        evaluation: GoalEvaluation,
        evaluation_mode: str,
    ) -> str:
        reply = None
        if not self._should_force_deterministic_browser_reply(task):
            reply = self._compose_with_llm(task, decision, outcome, evaluation, evaluation_mode)
        if reply is not None:
            return reply
        return self._compose_deterministically(task, decision, outcome)

    def _decide_with_llm(self, user_message: str) -> AssistantDecision | None:
        if not self.openrouter_client.is_configured():
            return None

        prompt = (
            f"{self.context_assembler.build('operator', user_message=user_message).to_prompt_block()}\n"
            "You are Sovereign's front-door CEO assistant. Decide how to handle the user's request as the main brain, not as a keyword router.\n"
            "Choose exactly one mode and one escalation level.\n"
            "Modes:\n"
            "- ANSWER: direct conversational reply, no execution loop\n"
            "- ACT: one small action or simple tool use\n"
            "- EXECUTE: task or objective that needs planning/execution/review\n"
            "Escalation levels:\n"
            "- conversational_advice: thinking, brainstorming, explanation, recommendation, discussion\n"
            "- single_action: one concrete action with minimal scaffolding\n"
            "- bounded_task_execution: a limited multi-step task with a clear contained deliverable\n"
            "- objective_completion: a goal the operator should keep owning until done or clearly blocked\n"
            "Return strict JSON with the shape "
            '{"mode":"ANSWER","escalation_level":"conversational_advice","reasoning":"...","should_use_tools":false,"requires_minimal_follow_up":false}.\n'
            "Behavior guidance:\n"
            "- Prefer conversational_advice when the user is chatting, asking a question, brainstorming, or asking for help thinking.\n"
            "- Prefer single_action for one small concrete action like a reminder, file creation, or single command.\n"
            "- Prefer bounded_task_execution for research, comparison, or a contained multi-step task.\n"
            "- Prefer objective_completion when the user wants you to own the objective, keep going, or finish it unless blocked.\n"
            "- If the user asks what you can do, who you are, or asks for explanation/advice, answer directly.\n"
            "- If the user asks for a real reminder, calendar lookup, file action, browser action, or message/email action, choose ACT unless it clearly needs planning.\n"
            "- If the request requires building, refactoring, research, debugging, or several coordinated steps, choose EXECUTE.\n"
            "- If a missing detail blocks responsible action, choose ANSWER with requires_minimal_follow_up true and provide follow_up_prompt.\n"
            "- When unclear, stay conservative and choose conversational_advice.\n"
            "Examples:\n"
            '- "hi" -> {"mode":"ANSWER","escalation_level":"conversational_advice"}\n'
            '- "what can you do?" -> {"mode":"ANSWER","escalation_level":"conversational_advice"}\n'
            '- "remind me to study at 7" -> {"mode":"ACT","escalation_level":"single_action","should_use_tools":true,"intent_label":"reminder_action"}\n'
            '- "help me plan the next step" -> {"mode":"ANSWER","escalation_level":"conversational_advice"}\n'
            '- "remind me in 5 mins to call mom" -> {"mode":"ACT","escalation_level":"single_action"}\n'
            '- "create a file called test.txt" -> {"mode":"ACT","escalation_level":"single_action"}\n'
            '- "build me a script" -> {"mode":"EXECUTE","escalation_level":"bounded_task_execution","should_use_tools":true}\n'
            '- "research browser automation options and summarize tradeoffs" -> {"mode":"EXECUTE","escalation_level":"bounded_task_execution"}\n'
            '- "build the reminder system and keep going until it works or you are blocked" -> {"mode":"EXECUTE","escalation_level":"objective_completion"}\n'
            f"User message: {user_message}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are the request interpretation layer for Project Sovereign. "
                    "Return only valid JSON."
                ),
                label="assistant_decision",
                context=ModelRequestContext(
                    intent_label="assistant_decision",
                    request_mode="answer",
                    selected_lane="assistant",
                    selected_agent="assistant_agent",
                    task_complexity="low",
                    risk_level="low",
                    requires_tool_use=False,
                    requires_review=False,
                    evidence_quality="unknown",
                    user_visible_latency_sensitivity="high",
                    cost_sensitivity="high",
                ),
            )
            payload = json.loads(response)
            mode = str(payload.get("mode", "")).strip().upper()
            escalation_level = str(payload.get("escalation_level", "")).strip().lower()
            reasoning = str(payload.get("reasoning", "")).strip()
            should_use_tools = bool(payload.get("should_use_tools", False))
            requires_minimal_follow_up = bool(payload.get("requires_minimal_follow_up", False))
            intent_label = str(payload.get("intent_label", "")).strip().lower() or "assistant"
            follow_up_prompt = str(payload.get("follow_up_prompt", "")).strip() or None
            if mode not in {"ANSWER", "ACT", "EXECUTE"} or not reasoning:
                return None
            if not escalation_level:
                resolved_escalation = self._default_escalation_for_mode(mode)
            else:
                try:
                    resolved_escalation = ExecutionEscalation(escalation_level)
                except ValueError:
                    return None
            return AssistantDecision(
                mode=RequestMode(mode.lower()),
                escalation_level=resolved_escalation,
                reasoning=reasoning,
                should_use_tools=should_use_tools,
                requires_minimal_follow_up=requires_minimal_follow_up,
                intent_label=intent_label,
                follow_up_prompt=follow_up_prompt,
            )
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def _guardrail_decision(self, user_message: str) -> AssistantDecision | None:
        message = user_message.lower().strip()
        if not message:
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Empty or malformed messages should stay on the safe conversational path.",
                should_use_tools=False,
                intent_label="empty",
            )
        if (
            self.operator_context.get_pending_confirmation("calendar_action")
            or self.operator_context.get_pending_confirmation("gmail_action")
        ) and message in {"yes", "confirm", "confirmed", "do it", "go ahead"}:
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="The user confirmed a pending guarded action.",
                should_use_tools=True,
                intent_label="confirmation_action",
            )
        if self._looks_like_google_tasks_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Google Tasks actions are owned by the Scheduling Agent action path.",
                should_use_tools=True,
                intent_label="google_tasks",
            )
        if self._looks_like_referent_scheduling_action(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="A scheduling update refers to a recent calendar event or reminder.",
                should_use_tools=True,
                intent_label="scheduling_referent",
            )
        browser_file_decision = self._browser_file_objective_decision(message)
        if browser_file_decision is not None:
            return browser_file_decision
        if self._looks_like_direct_browser_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="A direct browser request with a concrete URL should use the bounded browser path.",
                should_use_tools=True,
                intent_label="browser_action",
            )
        if self._looks_like_explicit_reminder_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Explicit reminder scheduling should use the Scheduling Agent action path.",
                should_use_tools=True,
                intent_label="reminder_action",
            )
        if self._looks_like_simple_math(message):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Very small utility prompts can stay local if needed.",
                should_use_tools=False,
                intent_label="utility",
            )
        premium_policy_decision = build_tool_cost_policy().assess(user_message)
        if premium_policy_decision.blocked and premium_policy_decision.required_capability_ids:
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning=premium_policy_decision.blocker
                or "The requested premium capability is unavailable.",
                should_use_tools=False,
                intent_label="capability",
            )
        return None

    def _normalize_llm_decision(self, decision: AssistantDecision) -> AssistantDecision:
        if decision.mode == RequestMode.ANSWER:
            decision.escalation_level = ExecutionEscalation.CONVERSATIONAL_ADVICE
            decision.should_use_tools = False
        elif decision.mode == RequestMode.ACT:
            decision.escalation_level = ExecutionEscalation.SINGLE_ACTION
            decision.should_use_tools = True
        else:
            decision.should_use_tools = True
            if decision.escalation_level == ExecutionEscalation.CONVERSATIONAL_ADVICE:
                decision.escalation_level = ExecutionEscalation.BOUNDED_TASK_EXECUTION
        return decision

    def _decide_deterministically(self, user_message: str) -> AssistantDecision:
        message = user_message.lower()
        normalized = self._normalize_phrase_text(message)
        fast_path = self._obvious_assistant_fast_path_decision(user_message)
        if fast_path is not None:
            return fast_path

        if self._is_short_social_message(normalized, ("hello", "hi", "hey", "thanks")):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Short social prompts stay on the conversational fallback path.",
                should_use_tools=False,
                intent_label="chat",
            )
        if self._looks_like_preference_statement(message):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Preference-setting should remain conversational when the LLM is unavailable.",
                should_use_tools=False,
                intent_label="preference",
            )
        capability_question = self._capability_question_decision(message, normalized)
        if capability_question is not None:
            return capability_question
        if self._looks_like_explicit_reminder_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Explicit reminder scheduling keeps the live reminder path available during fallback.",
                should_use_tools=True,
                intent_label="reminder_action",
            )
        if looks_like_calendar_read_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Supported calendar lookup should go through the scheduling agent.",
                should_use_tools=True,
                intent_label="calendar_read",
            )
        if self._looks_like_google_tasks_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Google Tasks actions should go through the scheduling agent.",
                should_use_tools=True,
                intent_label="google_tasks",
            )
        if self._looks_like_referent_scheduling_action(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="A scheduling update refers to a recent calendar event or reminder.",
                should_use_tools=True,
                intent_label="scheduling_referent",
            )
        browser_file_decision = self._browser_file_objective_decision(message)
        if browser_file_decision is not None:
            return browser_file_decision
        if self._looks_like_direct_browser_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="A direct browser request with a concrete URL should use the bounded browser path.",
                should_use_tools=True,
                intent_label="browser_action",
            )
        if self._looks_like_email_operations_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Gmail/email operations are owned by the Communications Agent.",
                should_use_tools=True,
                intent_label="communications_email",
            )
        if self._looks_like_personal_ops_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Personal list/note and routine setup requests are owned by Personal Ops.",
                should_use_tools=True,
                intent_label="personal_ops",
            )
        if self._looks_like_bounded_coding_artifact_request(message):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
                reasoning="Bounded script-building requests should create and verify a real artifact.",
                should_use_tools=True,
                intent_label="coding_artifact",
            )
        if self._looks_like_objective_completion_request(message):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.OBJECTIVE_COMPLETION,
                reasoning="Objective-ownership language should stay on the execution path even without the LLM.",
                should_use_tools=True,
                intent_label="objective_execution",
            )
        if self._looks_like_meta_or_memory_request(message, normalized):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Continuity, memory, and capability questions should stay conversational on fallback.",
                should_use_tools=False,
                intent_label="assistant",
            )
        if self._looks_like_source_backed_research_request(message):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
                reasoning="Fresh information, comparison, documentation, or source-backed research needs the Research Agent.",
                should_use_tools=True,
                intent_label="research",
            )
        if self._looks_like_small_action(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="A direct one-step action request can stay lightweight on fallback.",
                should_use_tools=True,
                intent_label="single_action",
            )
        if self._looks_like_execution_request(message, normalized):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
                reasoning="A clear multi-step request should stay on the execution loop during fallback.",
                should_use_tools=True,
                intent_label="bounded_execution",
            )
        if self._looks_like_assistant_question(message):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Question-shaped requests default to conversational handling when they do not clearly ask for action.",
                should_use_tools=False,
                intent_label="assistant",
            )
        return AssistantDecision(
            mode=RequestMode.ANSWER,
            escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
            reasoning="When fallback intent is unclear, stay conversational instead of pre-shaping execution.",
            should_use_tools=False,
            intent_label="assistant",
        )

    def _decide_transport_locally(self, user_message: str) -> AssistantDecision:
        message = user_message.lower().strip()
        normalized = self._normalize_phrase_text(message)
        if not message:
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Empty messages should not trigger progress.",
                should_use_tools=False,
                intent_label="empty",
            )
        personal_ops_decision = self._personal_ops_decision(user_message)
        if personal_ops_decision is not None:
            return personal_ops_decision
        fast_path = self._obvious_assistant_fast_path_decision(user_message)
        if fast_path is not None:
            return fast_path
        capability_question = self._capability_question_decision(message, normalized)
        if capability_question is not None:
            return capability_question
        ambiguous_decision = self._ambiguous_request_decision(message, normalized)
        if ambiguous_decision is not None:
            return ambiguous_decision
        browser_file_decision = self._browser_file_objective_decision(message)
        if browser_file_decision is not None:
            return browser_file_decision
        if self._looks_like_direct_browser_request(user_message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Direct browser requests with concrete URLs should use the bounded browser fast lane.",
                should_use_tools=True,
                intent_label="browser_action",
            )
        if self._looks_like_explicit_reminder_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Reminder requests are explicit enough to show progress.",
                should_use_tools=True,
                intent_label="reminder_action",
            )
        if looks_like_calendar_read_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Supported calendar lookup should go through the scheduling agent.",
                should_use_tools=True,
                intent_label="calendar_read",
            )
        if self._looks_like_google_tasks_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Google Tasks actions should go through the scheduling agent.",
                should_use_tools=True,
                intent_label="google_tasks",
            )
        if self._looks_like_referent_scheduling_action(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="A scheduling update refers to a recent calendar event or reminder.",
                should_use_tools=True,
                intent_label="scheduling_referent",
            )
        if self._looks_like_personal_ops_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Personal list/note and routine setup requests should use the Personal Ops lane.",
                should_use_tools=True,
                intent_label="personal_ops",
            )
        if self._looks_like_email_operations_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Transport preview treats Gmail/email operations as Communications Agent work.",
                should_use_tools=True,
                intent_label="communications_email",
            )
        if self._looks_like_bounded_coding_artifact_request(message):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
                reasoning="This coding request should create and verify a real workspace artifact.",
                should_use_tools=True,
                intent_label="coding_artifact",
            )
        if self._looks_like_small_action(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="This is an explicit small action request.",
                should_use_tools=True,
                intent_label="single_action",
            )
        if self._looks_like_objective_completion_request(message):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.OBJECTIVE_COMPLETION,
                reasoning="This looks like a goal that should stay owned until done or blocked.",
                should_use_tools=True,
                intent_label="objective_execution",
            )
        if self._looks_like_execution_request(message, normalized):
            return AssistantDecision(
                mode=RequestMode.EXECUTE,
                escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
                reasoning="This looks like a clear multi-step execution goal.",
                should_use_tools=True,
                intent_label="bounded_execution",
            )
        if self._looks_like_assistant_question(message):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Transport preview treats assistant-style questions as conversational.",
                should_use_tools=False,
                intent_label="assistant",
            )
        return AssistantDecision(
            mode=RequestMode.ANSWER,
            escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
            reasoning="When transport-side classification is unclear, stay quiet and conversational.",
            should_use_tools=False,
            intent_label="assistant",
        )

    def _override_llm_for_guardrailed_action(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> AssistantDecision | None:
        message = user_message.lower().strip()
        if decision.mode == RequestMode.ANSWER and self._looks_like_explicit_reminder_request(message):
            return AssistantDecision(
                mode=RequestMode.ACT,
                escalation_level=ExecutionEscalation.SINGLE_ACTION,
                reasoning="Explicit reminder scheduling keeps the real reminder path available even if the classifier drifts conversationally.",
                should_use_tools=True,
                intent_label="reminder_action",
            )
        return None

    def _override_llm_for_email_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> AssistantDecision | None:
        message = user_message.lower().strip()
        if not self._looks_like_email_operations_request(message):
            return None
        if decision.mode != RequestMode.ANSWER and decision.should_use_tools:
            return None
        return AssistantDecision(
            mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
            reasoning="Gmail/email operations should use the Communications Agent even if classification drifts conversationally.",
            should_use_tools=True,
            requires_minimal_follow_up=False,
            intent_label="communications_email",
        )

    def _override_llm_for_capability_question(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> AssistantDecision | None:
        message = user_message.lower().strip()
        normalized = self._normalize_phrase_text(message)
        if decision.mode == RequestMode.ANSWER and not decision.should_use_tools:
            return None
        return self._capability_question_decision(message, normalized)

    def _override_llm_for_ambiguous_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> AssistantDecision | None:
        message = user_message.lower().strip()
        normalized = self._normalize_phrase_text(message)
        if decision.requires_minimal_follow_up and decision.mode == RequestMode.ANSWER:
            return None
        return self._ambiguous_request_decision(message, normalized)

    def _override_llm_for_browser_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> AssistantDecision | None:
        browser_file_decision = self._browser_file_objective_decision(user_message)
        if browser_file_decision is not None:
            return browser_file_decision
        if not self._looks_like_direct_browser_request(user_message.lower()):
            return None
        if decision.mode != RequestMode.ANSWER and decision.should_use_tools and not decision.requires_minimal_follow_up:
            return None
        return AssistantDecision(
            mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
            reasoning=(
                "A direct browser request with a concrete URL should execute through the live browser path, "
                "not stay in conversational answer mode."
            ),
            should_use_tools=True,
            requires_minimal_follow_up=False,
            intent_label="browser_action",
        )

    def _override_llm_for_personal_ops_request(
        self,
        user_message: str,
        decision: AssistantDecision,
    ) -> AssistantDecision | None:
        if not self._looks_like_personal_ops_request(user_message):
            return None
        if decision.mode != RequestMode.ANSWER and decision.should_use_tools:
            return None
        return AssistantDecision(
            mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
            reasoning="Personal list/note and proactive routine setup requests should delegate to Personal Ops.",
            should_use_tools=True,
            requires_minimal_follow_up=False,
            intent_label="personal_ops",
        )

    def _personal_ops_decision(self, user_message: str) -> AssistantDecision | None:
        if not self._looks_like_personal_ops_request(user_message):
            return None
        return AssistantDecision(
            mode=RequestMode.ACT,
            escalation_level=ExecutionEscalation.SINGLE_ACTION,
            reasoning="Personal list/note and proactive routine setup requests are owned by Personal Ops.",
            should_use_tools=True,
            requires_minimal_follow_up=False,
            intent_label="personal_ops",
        )

    def _compose_with_llm(
        self,
        task: Task,
        decision: AssistantDecision,
        outcome: TaskOutcome,
        evaluation: GoalEvaluation,
        evaluation_mode: str,
    ) -> str | None:
        if not self.openrouter_client.is_configured():
            return None

        prompt = (
            f"{self.context_assembler.build('operator', goal=task.goal).to_prompt_block()}\n"
            "Write the final user-facing reply for Project Sovereign's CEO assistant.\n"
            "Sound like one intelligent operator coordinating the work.\n"
            "Rules:\n"
            "- natural, direct, concise\n"
            "- do not mention internal labels such as task_status, request_mode, planner_mode, routes, lanes, traces, subtasks, agents, graphs, or evaluators\n"
            "- mention execution details only as user-relevant evidence, such as a created file, command result, browser finding, or exact blocker\n"
            "- never imply completion without evidence\n"
            "- if blocked, explain the blocker in human terms and say the minimum next thing needed\n"
            "Private execution context for honesty only, not wording:\n"
            f"- handling mode: {decision.mode.value}\n"
            f"- ownership level: {decision.escalation_level.value}\n"
            f"- current outcome: {task.status.value}\n"
            f"- evaluation source: {evaluation_mode}\n"
            f"User goal: {task.goal}\n"
            f"Outcome: {outcome.model_dump()}\n"
            f"Evaluation: {evaluation.model_dump()}\n"
            f"Structured results: {json.dumps(self._serialize_results(task.results), ensure_ascii=True)}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are the final response composer for a tool-using executive assistant. "
                    "Return only the message that should be sent to the user."
                ),
                label="assistant_compose",
                context=ModelRequestContext(
                    intent_label=decision.intent_label or "assistant_compose",
                    request_mode=decision.mode.value,
                    selected_lane="finalize",
                    selected_agent="assistant_agent",
                    task_complexity="medium" if task.request_mode == RequestMode.ACT else "high",
                    risk_level="high" if task.status == TaskStatus.BLOCKED else "medium",
                    requires_tool_use=bool(task.results),
                    requires_review=evaluation.needs_review or task.escalation_level != ExecutionEscalation.SINGLE_ACTION,
                    verifier_failed=not evaluation.satisfied and not evaluation.blocked and task.status != TaskStatus.COMPLETED,
                    reviewer_rejected=any(
                        result.agent == "reviewer_agent" and result.status == AgentExecutionStatus.BLOCKED
                        for result in task.results
                    ),
                    replan_count=0,
                    evidence_quality="medium" if task.results else "low",
                    user_visible_latency_sensitivity="medium",
                    cost_sensitivity="medium",
                ),
            )
            cleaned = response.strip()
            if self._contains_backend_jargon(cleaned):
                return None
            return cleaned or None
        except (RuntimeError, httpx.HTTPError):
            return None

    def _compose_deterministically(
        self,
        task: Task,
        decision: AssistantDecision,
        outcome: TaskOutcome,
    ) -> str:
        browser_reply = self._browser_reply_from_task(task)
        if browser_reply is not None:
            return browser_reply

        completed_actions = [
            self._describe_result(result)
            for result in task.results
            if result.status == AgentExecutionStatus.COMPLETED
        ]
        meaningful_actions = [action for action in completed_actions if action]
        blocked_result = next(
            (result for result in task.results if result.status == AgentExecutionStatus.BLOCKED),
            None,
        )
        pending_note = self._pending_note(task)

        if task.status == TaskStatus.BLOCKED and blocked_result is not None:
            completed_prefix = ""
            if meaningful_actions:
                completed_prefix = f"I already {self._join_phrases(meaningful_actions[:3])}. "
            return f"{completed_prefix}{self._assistant_friendly_blocked_reply(blocked_result)}".strip()
        if task.status == TaskStatus.BLOCKED and task.objective_state and task.objective_state.blocked_reasons:
            return f"I worked through the request, but I'm blocked because {self._humanize_blocker(task.objective_state.blocked_reasons[0])}."

        if decision.mode == RequestMode.ACT:
            if meaningful_actions:
                return f"I {self._join_phrases(meaningful_actions[:3])}."
            if task.status == TaskStatus.BLOCKED and blocked_result is not None:
                return self._assistant_friendly_blocked_reply(blocked_result)
            if pending_note:
                next_step = self._first_next_step(task.results)
                if next_step:
                    return self._assistant_friendly_incomplete_reply(task, next_step=next_step)
                return f"I started on that, but {self._lowercase_first(pending_note).rstrip('.')}."
            return "I started on that."

        if decision.mode == RequestMode.EXECUTE:
            if meaningful_actions:
                reply = f"I worked through it and {self._join_phrases(meaningful_actions[:4])}."
            else:
                reply = "I worked through the request."
            if decision.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION:
                if task.status == TaskStatus.COMPLETED:
                    return f"{reply} It's done."
                if pending_note:
                    next_step = self._first_next_step(task.results)
                    if next_step:
                        return f"{reply} {self._assistant_friendly_incomplete_reply(task, next_step=next_step, include_lead=False)}"
                    return f"{reply} I'm still keeping it open, and it's not done yet."
                return f"{reply} I'm keeping it open until I have enough evidence to call it done."
            if pending_note:
                next_step = self._first_next_step(task.results)
                if next_step:
                    reply = f"{reply} {self._assistant_friendly_incomplete_reply(task, next_step=next_step, include_lead=False)}"
                else:
                    reply = f"{reply} {pending_note}"
            elif outcome.completed and task.status == TaskStatus.COMPLETED:
                reply = f"{reply} It's complete."
            return reply

        if meaningful_actions:
            return f"I {self._join_phrases(meaningful_actions[:2])}."
        return "I handled it."

    def _browser_reply_from_task(self, task: Task) -> str | None:
        if any(result.tool_name == "file_tool" for result in task.results):
            return None
        browser_result = next(
            (
                result
                for result in reversed(task.results)
                if result.tool_name == "browser_tool" and result.evidence
            ),
            None,
        )
        if browser_result is None:
            return None
        tool_evidence = next((item for item in browser_result.evidence if isinstance(item, ToolEvidence)), None)
        if tool_evidence is None:
            return None
        browser_task = tool_evidence.payload.get("browser_task", {})
        synthesis_result = str(browser_task.get("synthesis_result") or browser_result.summary).strip()
        if not synthesis_result:
            return None
        if browser_result.status == AgentExecutionStatus.BLOCKED and browser_result.blockers:
            return synthesis_result
        return synthesis_result

    def _serialize_results(self, results: list[AgentResult]) -> list[dict[str, object]]:
        return [
            {
                "agent": result.agent,
                "status": result.status.value,
                "summary": result.summary,
                "blockers": result.blockers,
                "next_actions": result.next_actions,
                "evidence": [item.model_dump() for item in result.evidence],
            }
            for result in results
        ]

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
                return f"checked {target or 'the requested workspace directory'}"

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
        if tool_evidence is not None and tool_evidence.tool_name == "coding_artifact":
            created_files = tool_evidence.payload.get("created_files", [])
            run_payload = tool_evidence.payload.get("run", {})
            target = None
            if isinstance(created_files, list) and created_files:
                first_file = created_files[0]
                if isinstance(first_file, dict):
                    target = self._format_path(str(first_file.get("file_path") or first_file.get("actual_path") or ""))
            if isinstance(run_payload, dict) and run_payload.get("stdout_preview"):
                return f"created {target or 'the requested file'} and verified it by running it"
            return f"created {target or 'the requested file'}"
        if tool_evidence is not None and tool_evidence.tool_name == "codex_cli":
            changed_files = tool_evidence.payload.get("changed_files", [])
            tests_run = tool_evidence.payload.get("tests_run", [])
            file_count = len(changed_files) if isinstance(changed_files, list) else 0
            test_count = len(tests_run) if isinstance(tests_run, list) else 0
            if file_count and test_count:
                return f"updated {file_count} file(s) and captured verification results"
            if file_count:
                return f"updated {file_count} file(s)"
            return "worked through the coding task"
        if tool_evidence is not None and tool_evidence.tool_name == "browser_tool":
            title = str(tool_evidence.payload.get("title", "")).strip()
            final_url = str(tool_evidence.payload.get("final_url", "")).strip()
            if title and final_url:
                return f"opened {final_url} and captured evidence for {title}"
            if final_url:
                return f"opened {final_url} and captured browser evidence"

        if result.agent == "reviewer_agent":
            return self._reviewer_phrase(result)

        lowered = result.summary.rstrip(".")
        if lowered:
            lowered = lowered[0].lower() + lowered[1:]
        return lowered

    def _reviewer_phrase(self, result: AgentResult) -> str:
        file_evidence = next((item for item in result.evidence if isinstance(item, FileEvidence)), None)
        if file_evidence is not None:
            target = self._format_path(file_evidence.file_path)
            if file_evidence.operation == "write" and target:
                return f"verified that {target} was created"
            if file_evidence.operation == "read" and target:
                return f"verified the contents of {target}"
            if file_evidence.operation == "list":
                return "verified the directory listing"

        tool_evidence = next((item for item in result.evidence if isinstance(item, ToolEvidence)), None)
        if tool_evidence is not None and tool_evidence.tool_name == "runtime_tool":
            command = str(tool_evidence.payload.get("command", "")).strip()
            if command:
                return f"verified the result of `{command}`"
        if tool_evidence is not None and tool_evidence.tool_name == "coding_artifact":
            return "verified the generated file and run output"
        if tool_evidence is not None and tool_evidence.tool_name == "codex_cli":
            return "verified the code changes and captured evidence"
        if tool_evidence is not None and tool_evidence.tool_name == "browser_tool":
            title = str(tool_evidence.payload.get("title", "")).strip()
            if title:
                return f"verified the browser evidence for {title}"
            return "verified the browser evidence"
        if tool_evidence is not None and tool_evidence.tool_name == "web_search_tool":
            return "verified the source-backed research evidence"
        return "reviewed the result"

    def _describe_blocker(self, result: AgentResult) -> str:
        if result.blockers:
            return result.blockers[0].rstrip(".")
        return result.summary.rstrip(".")

    def _describe_next_step(self, result: AgentResult) -> str:
        if result.next_actions:
            return f"To keep going, I need {self._next_step_clause(result.next_actions[0])}."
        return "To keep going, I need a clearer next step from you."

    def _assistant_friendly_blocked_reply(self, result: AgentResult) -> str:
        blocker_text = " ".join(
            part
            for part in [
                result.agent,
                result.tool_name or "",
                result.summary,
                *result.blockers,
                *result.next_actions,
            ]
            if part
        ).lower()
        if any(token in blocker_text for token in ("email", "messaging", "calendar")):
            return (
                "I'm blocked because this workspace is not connected to real email, messaging, or calendar delivery yet. "
                "I need that connection set up before I can do it for real."
            )
        if "web_search_tool" in blocker_text or "source-backed search" in blocker_text or "source-backed research" in blocker_text:
            if any(token in blocker_text for token in ("not configured", "api_key", "openrouter", "search_provider")):
                return (
                    "I'm blocked because live research search is not connected yet. "
                    "I need the search provider key set in the environment before I can research this for real."
                )
            blocker = self._describe_blocker(result)
            next_step = self._describe_next_step(result)
            return f"I'm blocked on the research because {self._humanize_blocker(blocker)}. {next_step}"
        if "browser" in blocker_text:
            if any(
                token in blocker_text
                for token in ("disabled", "not wired", "adapter", "missing config", "browser_enabled", "not connected")
            ):
                return (
                    "I'm blocked because live browser access is not available here. "
                    "I need browser access enabled before I can do that for real."
                )
            blocker = self._describe_blocker(result)
            next_step = self._describe_next_step(result)
            return f"I'm blocked in the browser because {self._humanize_blocker(blocker)}. {next_step}"
        if "tool invocation" in blocker_text or result.agent == "coding_agent":
            return (
                "I'm blocked because live coding execution is not available here. "
                "I can outline the work, but I need coding access connected before I can carry it through."
            )
        if "codex" in blocker_text:
            if any(token in blocker_text for token in ("not fully configured", "enabled", "not available", "command")):
                return (
                    "I'm blocked because the advanced coding worker is not configured here. "
                    "I need the local coding command enabled and pointed at this workspace before I can use it."
                )
            blocker = self._describe_blocker(result)
            next_step = self._describe_next_step(result)
            return f"I'm blocked on the coding work because {self._humanize_blocker(blocker)}. {next_step}"
        blocker = self._describe_blocker(result)
        next_step = self._describe_next_step(result)
        return f"I'm blocked because {self._humanize_blocker(blocker)}. {next_step}"

    def _assistant_friendly_incomplete_reply(
        self,
        task: Task,
        *,
        next_step: str,
        include_lead: bool = True,
    ) -> str:
        goal_text = task.goal.lower()
        next_text = next_step.lower()
        combined = f"{goal_text} {next_text}"
        if "browser" in goal_text and any(
            token in combined for token in ("disabled", "adapter", "browser_enabled", "not connected")
        ):
            reply = "I can't open a live browser here until browser access is enabled."
        elif any(token in goal_text for token in ("email", "message", "calendar")):
            reply = "I can't carry out real email, messaging, or calendar actions here until those accounts are connected."
        elif "tool invocation" in combined or "coding" in goal_text or "code " in goal_text:
            reply = (
                "The coding work is not done because live coding access is not connected here. "
                f"I still need {self._next_step_clause(next_step)}."
            )
        elif any(token in next_text for token in ("artifact", "verification", "review", "retrieval", "source collection")):
            reply = "I started on that, but I haven't finished it yet because this runtime still needs live build and verification paths to carry it through."
        else:
            reply = f"I started on that, but I still need {self._next_step_clause(next_step)}."
        if include_lead:
            return reply
        return reply[0].upper() + reply[1:] if reply else reply

    def _pending_note(self, task: Task) -> str | None:
        if task.status == TaskStatus.RUNNING:
            return "It's not fully done yet."
        if task.status == TaskStatus.BLOCKED:
            return "It's currently blocked."
        return None

    def _first_next_step(self, results: list[AgentResult]) -> str | None:
        for result in results:
            if result.next_actions:
                return result.next_actions[0]
        return None

    def _next_step_clause(self, text: str) -> str:
        stripped = self._lowercase_first(text).rstrip(".")
        stripped = self._humanize_blocker(stripped)
        if stripped.startswith(("to ", "you to ", "your ", "a ", "an ", "the ", "more ")):
            return stripped
        return f"to {stripped}"

    def _humanize_blocker(self, text: str) -> str:
        cleaned = " ".join(text.strip().rstrip(".").split())
        replacements = {
            "live browser execution": "live browser access",
            "browser adapter": "browser access",
            "outbound adapter": "account connection",
            "coding adapter": "coding access",
            "real coding execution path": "live coding access",
            "not wired into the runtime": "not available here",
            "in this runtime": "here",
        }
        lowered = cleaned.lower()
        for source, target in replacements.items():
            lowered = lowered.replace(source, target)
        lowered = lowered.replace("manus", "Manus")
        return lowered or "I need one more piece of setup"

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
            )
        )

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
            relative = candidate.relative_to(Path(settings.workspace_root))
            return f"`{relative.as_posix()}`"
        except ValueError:
            return f"`{candidate.name}`"

    def _lowercase_first(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            return stripped
        return stripped[0].lower() + stripped[1:]

    def _contains_any_phrase(self, normalized_message: str, phrases: tuple[str, ...]) -> bool:
        return any(f" {self._normalize_phrase_text(phrase).strip()} " in normalized_message for phrase in phrases)

    def _normalize_phrase_text(self, value: str) -> str:
        translation = str.maketrans({char: " " for char in ".,!?;:\n\r\t"})
        compact = " ".join(value.translate(translation).split())
        return f" {compact} "

    def _is_short_social_message(self, normalized_message: str, phrases: tuple[str, ...]) -> bool:
        stripped = normalized_message.strip()
        if len(stripped.split()) > 4:
            return False
        return any(stripped == self._normalize_phrase_text(phrase).strip() for phrase in phrases)

    def _looks_like_simple_math(self, value: str) -> bool:
        compact = value.strip()
        if len(compact) > 40 or not compact:
            return False
        return bool(re.fullmatch(r"[\d\s\.\+\-\*\/%\(\)xX=]+", compact))

    def _looks_like_preference_statement(self, message: str) -> bool:
        lowered = message.lower().strip()
        preference_markers = (
            "please keep",
            "please be",
            "i prefer",
            "do not",
            "don't",
        )
        style_markers = (
            "concise",
            "brief",
            "direct",
            "natural",
            "less jargon",
            "more detail",
            "tone",
        )
        return any(marker in lowered for marker in preference_markers) and any(
            marker in lowered for marker in style_markers
        )

    def _looks_like_assistant_question(self, message: str) -> bool:
        stripped = message.strip()
        if self._looks_like_capability_question(stripped.lower(), self._normalize_phrase_text(stripped.lower())):
            return True
        if self._looks_like_question_shaped_action_request(stripped):
            return False
        if stripped.endswith("?") and not self._looks_like_execution_request(
            stripped.lower(),
            self._normalize_phrase_text(stripped.lower()),
            execute_markers=("build", "implement", "research", "investigate", "audit", "write", "create"),
        ):
            return True
        conversational_starts = (
            "what ",
            "who ",
            "why ",
            "how ",
            "when ",
            "where ",
            "can you ",
            "could you ",
            "would you ",
            "do you ",
            "are you ",
            "is this ",
            "help me ",
        )
        return stripped.startswith(conversational_starts)

    def _looks_like_small_action(self, message: str) -> bool:
        normalized = message.strip()
        if self._looks_like_capability_question(message.lower().strip(), self._normalize_phrase_text(message.lower())):
            return False
        for prefix in ("now ", "please ", "can you ", "could you "):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        action_starts = (
            "create ",
            "write ",
            "make ",
            "list ",
            "add ",
            "schedule ",
            "put ",
            "set ",
            "update ",
            "edit ",
            "change ",
            "run ",
            "open ",
            "go to ",
            "browse ",
            "navigate ",
            "generate ",
            "read ",
            "send ",
            "cancel ",
            "delete ",
            "remove ",
            "reschedule ",
        )
        if normalized.startswith(action_starts):
            return True
        return any(
            phrase in normalized
            for phrase in (
                "create a file",
                "create an email",
                "send a message",
                "send an email",
                "run command",
                "run python",
                "create test.txt",
            )
        )

    def _looks_like_email_operations_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        if parse_explicit_contact_statement(message) is not None:
            return False
        if lowered.startswith("message ") and " saying " in lowered:
            return True
        if not any(token in lowered for token in ("gmail", "email", "emails", "mailbox", "newsletter", "newsletters", "inbox")):
            return False
        if any(token in lowered for token in ("calendar", "reminder")):
            return False
        return any(
            token in lowered
            for token in (
                "what ",
                "which ",
                "do ",
                "have ",
                "any ",
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

    def _looks_like_personal_ops_request(self, message: str) -> bool:
        return looks_like_personal_ops_request(message)

    def _looks_like_bounded_coding_artifact_request(self, message: str) -> bool:
        lowered = " ".join(message.lower().split())
        if any(term in lowered for term in ("browser", "website", "calendar", "reminder", "email")):
            return False
        if not any(term in lowered for term in ("build", "create", "make", "write", "generate")):
            return False
        return "python script" in lowered

    def _looks_like_google_tasks_request(self, message: str) -> bool:
        if looks_like_google_tasks_request(message):
            return True
        lowered = " ".join(message.lower().strip().split())
        if not any(word in lowered for word in ("mark", "complete", "finish", "done")):
            return False
        return bool(self.operator_context.resolve_recent_referents(object_type="google_task", pronoun_text=message))

    def _looks_like_referent_scheduling_action(self, message: str) -> bool:
        lowered = " ".join(message.lower().strip().split())
        if not re.search(r"\b(that|this|it|one|first|second|third|last)\b", lowered):
            return False
        if not any(
            word in lowered
            for word in ("cancel", "delete", "remove", "move", "update", "change", "reschedule", "make")
        ):
            return False
        return bool(
            self.operator_context.resolve_recent_referents(object_type="calendar_event", pronoun_text=message)
            or self.operator_context.resolve_recent_referents(object_type="reminder", pronoun_text=message)
        )

    def _looks_like_direct_browser_request(self, message: str) -> bool:
        return extract_obvious_browser_request(message) is not None

    def _looks_like_browser_file_objective(self, message: str) -> bool:
        lowered = " ".join(message.lower().split())
        if not self._looks_like_direct_browser_request(lowered):
            return False
        if not any(term in lowered for term in ("save", "write", "create", "put")):
            return False
        return any(term in lowered for term in (".txt", ".md", ".json", "file", "summary"))

    def _browser_file_objective_decision(self, message: str) -> AssistantDecision | None:
        if not self._looks_like_browser_file_objective(message):
            return None
        return AssistantDecision(
            mode=RequestMode.EXECUTE,
            escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
            reasoning="Browser evidence plus a saved file is a multi-step objective that needs execution and verification.",
            should_use_tools=True,
            requires_minimal_follow_up=False,
            intent_label="browser_file_objective",
        )

    def _looks_like_question_shaped_action_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        if self._looks_like_capability_question(lowered, self._normalize_phrase_text(lowered)):
            return False
        request_starts = (
            "can you ",
            "could you ",
            "would you ",
            "will you ",
            "please ",
        )
        if not lowered.startswith(request_starts):
            return False
        candidate = lowered
        for prefix in request_starts:
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        return candidate.startswith(
            (
                "write ",
                "create ",
                "make ",
                "generate ",
                "run ",
                "open ",
                "list ",
                "go to ",
                "browse ",
                "navigate ",
                "add ",
                "schedule ",
                "put ",
                "set ",
                "update ",
                "edit ",
                "send ",
            )
        )

    def _should_force_deterministic_browser_reply(self, task: Task) -> bool:
        if self._looks_like_direct_browser_request(task.goal):
            return True
        if any(result.tool_name == "browser_tool" for result in task.results):
            return True
        return any(
            subtask.assigned_agent == "browser_agent"
            or (subtask.tool_invocation and subtask.tool_invocation.tool_name == "browser_tool")
            for subtask in task.subtasks
        )

    def _looks_like_explicit_reminder_request(self, message: str) -> bool:
        if not any(marker in message for marker in ("remind me", "set a reminder", "schedule a reminder")):
            return False
        if any(
            phrase in message
            for phrase in (
                "remind me what",
                "remind me who",
                "remind me why",
                "remind me how",
                "remind me where",
                "remind me when we",
                "remind me what this project is",
                "did you remind me",
                "what reminders do i have",
                "what recurring reminders are active",
                "what reminders have fired",
                "is reminder delivery working",
            )
        ):
            return False
        return any(
            token in message
            for token in (
                " in ",
                " at ",
                " tomorrow",
                " next ",
                " on ",
                " after ",
                " tonight",
                " this evening",
                " this afternoon",
                " this morning",
                " yesterday",
                " later today",
                " every ",
                " each ",
                " daily",
                " weekly",
                " monthly",
                " weekday",
            )
        ) or "set a reminder" in message or "schedule a reminder" in message

    def _looks_like_execution_request(
        self,
        message: str,
        normalized_message: str,
        execute_markers: tuple[str, ...] | None = None,
    ) -> bool:
        markers = execute_markers or (
            "build",
            "implement",
            "refactor",
            "debug",
            "fix",
            "research",
            "investigate",
            "compare",
            "current",
            "latest",
            "recent",
            "news",
            "documentation",
            "docs",
            "look up",
            "coordinate",
            "workflow",
            "end-to-end",
            "multi-step",
            "audit",
            "failing test",
            "tests",
        )
        if any(marker in message for marker in markers):
            if self._contains_any_phrase(
                normalized_message,
                ("help me plan", "create a plan for", "make a plan for", "plan the next step"),
            ):
                return False
            return True
        return False

    def _looks_like_source_backed_research_request(self, message: str) -> bool:
        lowered = " ".join(message.lower().split())
        if self._looks_like_direct_browser_request(lowered):
            return False
        if self._looks_like_meta_or_memory_request(lowered, self._normalize_phrase_text(lowered)):
            return False
        return any(
            marker in lowered
            for marker in (
                "research",
                "compare",
                "comparison",
                "current",
                "latest",
                "recent",
                "news",
                "documentation",
                "docs",
                "look up",
                "source",
                "sources",
            )
        )

    def _looks_like_meta_or_memory_request(self, message: str, normalized_message: str) -> bool:
        if self._contains_any_phrase(
            normalized_message,
            (
                "what do you remember",
                "what do you know about me",
                "what were we focused on before",
                "what are you working on",
                "what did you just do",
                "what can you do",
                "what tools do you have",
                "who are you",
                "what is this project",
                "continue",
                "show me the files you created",
                "what files did you create",
                "what reminders do i have",
                "what recurring reminders are active",
                "what reminders have fired",
                "did you remind me",
                "is reminder delivery working",
            ),
        ):
            return True
        if message.endswith("?") and any(
            token in message
            for token in ("remember", "memory", "working on", "focused on", "can you do", "tools", "model")
        ):
            return True
        return False

    def _capability_question_decision(
        self,
        message: str,
        normalized_message: str,
    ) -> AssistantDecision | None:
        if not self._looks_like_capability_question(message, normalized_message):
            return None
        return AssistantDecision(
            mode=RequestMode.ANSWER,
            escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
            reasoning="This reads like a capability question, so Sovereign should answer directly instead of acting.",
            should_use_tools=False,
            intent_label="capability",
        )

    def _ambiguous_request_decision(
        self,
        message: str,
        normalized_message: str,
    ) -> AssistantDecision | None:
        follow_up_prompt = self._clarification_prompt_for_message(message, normalized_message)
        if follow_up_prompt is None:
            return None
        return AssistantDecision(
            mode=RequestMode.ANSWER,
            escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
            reasoning="This request is too ambiguous to execute safely, so the assistant should ask a clarifying follow-up first.",
            should_use_tools=False,
            requires_minimal_follow_up=True,
            intent_label="ambiguous",
            follow_up_prompt=follow_up_prompt,
        )

    def _looks_like_capability_question(self, message: str, normalized_message: str) -> bool:
        if any(
            phrase in message
            for phrase in (
                "what can you do",
                "what do you do",
                "what tools do you have",
                "what do you have access to",
                "what agents do you have",
                "what is currently connected",
                "what's currently connected",
                "what integrations are connected",
                "what should we build next",
                "what should i build next",
                "who are you",
                "are you able to",
                "are you capable of",
                "do you support",
            )
        ):
            return True
        if " or nah" in message or " or not" in message:
            return True
        if not message.startswith(("can you ", "could you ", "can u ", "could u ", "do you ")):
            return False
        concrete_action_markers = (
            ".txt",
            ".md",
            ".py",
            " called ",
            " named ",
            " workspace/",
            "http://",
            "https://",
            " tomorrow",
            " next ",
            " at ",
        )
        if any(marker in message for marker in concrete_action_markers):
            return False
        return any(
            phrase in normalized_message
            for phrase in (
                " can you create files ",
                " can you create a file ",
                " can u make a file ",
                " can you make a file ",
                " can you browse websites ",
                " can you open websites ",
                " can you use the browser ",
                " can you use browser ",
                " can you use browser use ",
                " can you use codex ",
                " can you send emails ",
                " can you send email ",
                " can you use gmail ",
                " can you see my calendar ",
                " can you see my tasks ",
                " can you see calendar ",
                " can you see tasks ",
                " do you have agents ",
                " do you have subagents ",
                " do you have codex ",
                " do you have gmail ",
                " do you have calendar ",
                " do you have tasks ",
                " can u make a file or nah ",
                " can you make a file or nah ",
                " can you create files or nah ",
            )
        )

    def _clarification_prompt_for_message(self, message: str, normalized_message: str) -> str | None:
        if message in {"wyd", "wdyd", "wym", "wdym", "sup", "yo?"}:
            return "Do you want a quick chat reply, or do you want help with something specific?"
        if message.startswith(("i might want", "i may want", "maybe i want", "i'm thinking about")):
            if "file" in message or "note" in message:
                return "Do you want me to create a file now, or are you just asking whether I can?"
            return "What do you want me to do with that?"
        if any(
            phrase in normalized_message
            for phrase in (
                " maybe a file ",
                " maybe a note ",
                " open the website ",
                " open a website ",
                " open the browser ",
            )
        ):
            return "What exactly do you want me to act on?"
        return None

    def _obvious_assistant_fast_path_decision(self, user_message: str) -> AssistantDecision | None:
        normalized_message = " ".join(user_message.lower().split())
        if not normalized_message:
            return None
        if self._is_memory_follow_up_request(normalized_message):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="Memory follow-up questions should stay on the lightweight memory path.",
                should_use_tools=False,
                intent_label="memory",
            )
        if is_obvious_assistant_fast_path(normalized_message):
            reasoning = "Obvious lightweight assistant and memory messages should skip heavy planning."
            if is_thanks_message(normalized_message):
                reasoning = "Brief social acknowledgements should stay on the lightweight assistant path."
            elif is_name_statement(normalized_message):
                reasoning = "Simple identity statements should be handled as direct assistant memory updates."
            elif is_forget_name_statement(normalized_message):
                reasoning = "Simple memory deletion requests should stay on the lightweight assistant path."
            elif is_explicit_memory_statement(normalized_message) or is_short_personal_fact_statement(
                normalized_message
            ):
                reasoning = "Explicit memory updates should stay on the lightweight assistant path."
            elif (
                is_user_memory_question(normalized_message)
                or is_project_memory_question(normalized_message)
                or is_memory_lookup(normalized_message)
            ):
                reasoning = "Direct memory questions should stay conversational and use the memory layer."
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning=reasoning,
                should_use_tools=False,
                intent_label="memory"
                if (
                    is_name_statement(normalized_message)
                    or is_forget_name_statement(normalized_message)
                    or is_explicit_memory_statement(normalized_message)
                    or is_short_personal_fact_statement(normalized_message)
                    or is_user_memory_question(normalized_message)
                    or is_project_memory_question(normalized_message)
                    or is_memory_lookup(normalized_message)
                )
                else "chat",
            )
        return None

    def _trivial_assistant_fast_path_decision(self, user_message: str) -> AssistantDecision | None:
        normalized_message = " ".join(user_message.lower().split())
        if not normalized_message:
            return None
        if is_greeting_message(normalized_message) or is_thanks_message(normalized_message):
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="A tiny social message should get an immediate conversational reply.",
                should_use_tools=False,
                intent_label="chat",
            )
        if normalized_message in {"how are you", "how are you?"}:
            return AssistantDecision(
                mode=RequestMode.ANSWER,
                escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
                reasoning="A tiny social check-in should get an immediate conversational reply.",
                should_use_tools=False,
                intent_label="chat",
            )
        return None

    def _is_memory_follow_up_request(self, normalized_message: str) -> bool:
        if not is_memory_follow_up_phrase(normalized_message):
            return False
        recent_turns = self.operator_context.recent_conversation_turns(limit=4)
        if len(recent_turns) < 3:
            return False
        assistant_index = -1
        user_index = -2
        if recent_turns[-1][0] == "user" and " ".join(recent_turns[-1][1].lower().split()) == normalized_message:
            assistant_index = -2
            user_index = -3
        if len(recent_turns) < abs(user_index):
            return False
        last_role, last_content = recent_turns[assistant_index]
        previous_role, previous_content = recent_turns[user_index]
        if last_role != "assistant" or previous_role != "user":
            return False
        if not self._looks_like_memory_reply(last_content):
            return False
        return self._looks_like_memory_prompt(previous_content)

    def _looks_like_memory_prompt(self, text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return any(
            (
                is_user_memory_question(normalized),
                is_project_memory_question(normalized),
                is_memory_lookup(normalized),
                is_memory_follow_up_phrase(normalized),
            )
        )

    def _looks_like_memory_reply(self, text: str) -> bool:
        normalized = " ".join(text.lower().split())
        reply_markers = (
            "i remember ",
            "that's all i currently have",
            "i only have one saved detail",
            "i only have one saved project sovereign detail",
            "i don't have much personal context stored yet",
            "i don't have much saved about you yet",
            "i don't have any saved memory",
            "i know the high-level shape of project sovereign",
        )
        return any(marker in normalized for marker in reply_markers)

    def _looks_like_objective_completion_request(self, message: str) -> bool:
        objective_markers = (
            "keep going until",
            "until it works",
            "until you're blocked",
            "until you are blocked",
            "own this",
            "take this project forward",
            "handle this objective",
            "complete this objective",
            "see this through",
            "finish this",
        )
        return any(marker in message for marker in objective_markers)

    def _default_escalation_for_mode(self, mode: str) -> ExecutionEscalation:
        if mode == "ANSWER":
            return ExecutionEscalation.CONVERSATIONAL_ADVICE
        if mode == "ACT":
            return ExecutionEscalation.SINGLE_ACTION
        return ExecutionEscalation.BOUNDED_TASK_EXECUTION
