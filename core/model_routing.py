"""Shared structured model routing and escalation policy for LLM-backed calls."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from app.config import settings


_PLACEHOLDER_MODEL_VALUES = {
    "your-model",
    "your-fast-model",
    "your-balanced-model",
    "your-frontier-model",
    "placeholder",
    "example-model",
}


class ModelProvider:
    """Supported direct model providers for a routed request."""

    OPENROUTER = "openrouter"
    OPENAI = "openai"


class ModelTier:
    """Named model tiers used across routing and escalation."""

    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"

    ORDER = (TIER_1, TIER_2, TIER_3)


@dataclass(frozen=True)
class ModelRequestContext:
    """Structured task context used to choose a model tier and escalation policy."""

    intent_label: str = "assistant"
    request_mode: str = "answer"
    selected_lane: str = "assistant"
    selected_agent: str = "assistant_agent"
    task_complexity: str = "low"
    risk_level: str = "low"
    requires_tool_use: bool = False
    requires_review: bool = False
    verifier_failed: bool = False
    reviewer_rejected: bool = False
    replan_count: int = 0
    evidence_quality: str = "unknown"
    user_visible_latency_sensitivity: str = "high"
    cost_sensitivity: str = "high"
    fallback_allowed: bool = True

    def as_log_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelSelection:
    """Resolved model selection for a single LLM call attempt."""

    model: str
    tier: str
    provider: str
    reason: str
    token_estimate: int
    escalation_allowed: bool
    context: ModelRequestContext


class ModelRouter:
    """Selects models dynamically and validates whether escalation is warranted."""

    _TIER_1_LABELS = {
        "assistant_decision",
        "conversation_answer",
        "memory_extract",
        "reminder_parse",
    }
    _TIER_2_LABELS = {
        "assistant_compose",
        "planner_create_plan",
        "router_agent_select",
        "browser_synthesis",
        "browser_backend_selection",
        "browser_target_resolution",
    }
    _JSON_LABELS = {
        "assistant_decision",
        "planner_create_plan",
        "router_agent_select",
        "goal_evaluate",
        "browser_backend_selection",
        "browser_target_resolution",
        "memory_extract",
        "reminder_parse",
    }
    _FAST_PATH_INTENTS = {
        "assistant",
        "capability",
        "chat",
        "clarify",
        "memory",
        "utility",
        "ambiguous",
        "empty",
    }
    _NON_ESCALATING_INTENTS = {
        "assistant",
        "capability",
        "chat",
        "clarify",
        "memory",
        "ambiguous",
        "reminder_action",
        "calendar_action",
        "local_file_action",
    }
    _NON_ESCALATING_AGENTS = {
        "assistant_agent",
        "memory_agent",
        "scheduling_agent",
    }
    _HIGH_RISK_LEVELS = {"high", "critical"}
    _HIGH_COMPLEXITY_LEVELS = {"high", "critical"}
    _LOW_EVIDENCE = {"low", "missing", "none"}
    _VALIDATION_ESCALATION_REASONS = {
        "invalid_json",
        "empty_response",
        "planner_missing_subtasks",
        "router_missing_agent_name",
        "router_missing_reasoning",
        "assistant_decision_invalid_mode",
        "assistant_decision_missing_reasoning",
        "goal_evaluate_missing_fields",
        "goal_evaluate_invalid_missing",
        "goal_evaluate_missing_reasoning",
        "low_confidence_critical_evaluation",
        "browser_backend_invalid",
        "browser_target_low_confidence",
        "memory_extract_invalid",
        "low_evidence_quality",
    }

    def __init__(self) -> None:
        tier_1 = (
            self._configured_model(settings.model_tier_1)
            or settings.openrouter_model_tier1
            or settings.openrouter_model
        )
        tier_2 = (
            self._configured_model(settings.model_tier_2)
            or settings.openrouter_model_tier2
            or settings.openrouter_model
        )
        tier_3 = (
            self._configured_model(settings.model_tier_3)
            or settings.openrouter_model_tier3
            or settings.frontier_model_name
            or tier_2
            or settings.openrouter_model
        )
        self.tier_models = {
            ModelTier.TIER_1: tier_1,
            ModelTier.TIER_2: tier_2,
            ModelTier.TIER_3: tier_3,
        }
        self.openai_tier_3_model = self._configured_model(
            settings.openai_model_tier_3
        ) or self._configured_model(settings.model_tier_3)

    def enabled(self) -> bool:
        return bool(settings.model_routing_enabled)

    def default_context(
        self,
        *,
        label: str | None,
        prompt: str,
    ) -> ModelRequestContext:
        label_value = (label or "assistant").strip().lower()
        prompt_lower = prompt.lower()
        if label_value in {"conversation_answer", "memory_extract"}:
            return ModelRequestContext(
                intent_label="memory" if label_value == "memory_extract" else "assistant",
                request_mode="answer",
                selected_lane="assistant",
                selected_agent="assistant_agent" if label_value == "conversation_answer" else "memory_agent",
                task_complexity="low",
                risk_level="low",
                requires_tool_use=False,
                requires_review=False,
                evidence_quality="high" if label_value == "memory_extract" else "unknown",
                user_visible_latency_sensitivity="high",
                cost_sensitivity="high",
            )
        if label_value == "assistant_decision":
            return ModelRequestContext(
                intent_label="assistant",
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
            )
        if label_value == "planner_create_plan":
            return ModelRequestContext(
                intent_label="planning",
                request_mode="execute",
                selected_lane="execution_flow",
                selected_agent="planner_agent",
                task_complexity="medium",
                risk_level="medium",
                requires_tool_use=False,
                requires_review=True,
                evidence_quality="unknown",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            )
        if label_value == "router_agent_select":
            return ModelRequestContext(
                intent_label="routing",
                request_mode="execute",
                selected_lane="execution_flow",
                selected_agent="router",
                task_complexity="medium",
                risk_level="medium",
                requires_tool_use=False,
                requires_review=False,
                evidence_quality="unknown",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            )
        if label_value.startswith("browser_"):
            evidence_quality = "low" if "limited evidence" in prompt_lower else "medium"
            return ModelRequestContext(
                intent_label="browser_action",
                request_mode="act",
                selected_lane="browser",
                selected_agent="browser_agent",
                task_complexity="medium",
                risk_level="medium",
                requires_tool_use=True,
                requires_review=True,
                evidence_quality=evidence_quality,
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            )
        if label_value == "goal_evaluate":
            return ModelRequestContext(
                intent_label="verification",
                request_mode="execute",
                selected_lane="verification",
                selected_agent="verifier_agent",
                task_complexity="medium",
                risk_level="high" if self._prompt_looks_critical(prompt_lower) else "medium",
                requires_tool_use=False,
                requires_review=True,
                evidence_quality="unknown",
                user_visible_latency_sensitivity="medium",
                cost_sensitivity="medium",
            )
        return ModelRequestContext(
            intent_label=label_value or "assistant",
            request_mode="answer",
            selected_lane="assistant",
            selected_agent="assistant_agent",
            task_complexity="medium" if self._prompt_needs_balanced(prompt_lower, token_estimate=self.estimate_tokens(prompt)) else "low",
            risk_level="medium" if self._prompt_looks_critical(prompt_lower) else "low",
            requires_tool_use=False,
            requires_review=False,
            evidence_quality="unknown",
            user_visible_latency_sensitivity="medium",
            cost_sensitivity="medium",
        )

    def select(
        self,
        *,
        label: str | None,
        prompt: str,
        system_prompt: str | None = None,
        preferred_tier: str | None = None,
        context: ModelRequestContext | None = None,
    ) -> ModelSelection:
        token_estimate = self.estimate_tokens(prompt, system_prompt)
        resolved_context = context or self.default_context(label=label, prompt=prompt)
        tier = preferred_tier or self._base_tier(
            label=label,
            prompt=prompt,
            token_estimate=token_estimate,
            context=resolved_context,
        )
        provider, model = self._provider_for_tier(tier=tier, context=resolved_context)
        escalation_allowed = self.escalation_allowed(context=resolved_context)
        reason = self._reason_for_selection(
            label=label,
            prompt=prompt,
            token_estimate=token_estimate,
            tier=tier,
            provider=provider,
            context=resolved_context,
        )
        return ModelSelection(
            model=model,
            tier=tier,
            provider=provider,
            reason=reason,
            token_estimate=token_estimate,
            escalation_allowed=escalation_allowed,
            context=resolved_context,
        )

    def next_tier(
        self,
        tier: str,
        *,
        context: ModelRequestContext | None = None,
        reason: str | None = None,
    ) -> str | None:
        resolved_context = context or ModelRequestContext()
        if not self.escalation_allowed(context=resolved_context, reason=reason):
            return None
        try:
            index = ModelTier.ORDER.index(tier)
        except ValueError:
            return None
        next_index = index + 1
        if next_index >= len(ModelTier.ORDER):
            return None
        return ModelTier.ORDER[next_index]

    def escalation_allowed(
        self,
        *,
        context: ModelRequestContext,
        reason: str | None = None,
    ) -> bool:
        if not settings.model_escalation_enabled:
            return False
        if context.intent_label in self._NON_ESCALATING_INTENTS:
            return False
        if (
            context.request_mode == "answer"
            and not context.requires_tool_use
            and not context.requires_review
        ):
            return False
        if context.selected_lane in {"assistant", "memory", "clarify"}:
            return False
        if (
            context.selected_lane == "fast_action"
            and context.selected_agent in self._NON_ESCALATING_AGENTS
            and context.task_complexity == "low"
            and context.risk_level == "low"
        ):
            return False
        if reason is None:
            return True
        return reason in self._VALIDATION_ESCALATION_REASONS or reason == "request_failed"

    def codex_tier_guidance(self, text: str, *, context: ModelRequestContext | None = None) -> tuple[str, str]:
        token_estimate = self.estimate_tokens(text)
        resolved_context = context or ModelRequestContext(
            intent_label="coding",
            request_mode="execute",
            selected_lane="execution_flow",
            selected_agent="codex_cli_agent",
            task_complexity="high",
            risk_level="medium",
            requires_tool_use=False,
            requires_review=True,
            user_visible_latency_sensitivity="medium",
            cost_sensitivity="medium",
        )
        tier = self._base_tier(
            label="codex_execution_prompt",
            prompt=text,
            token_estimate=token_estimate,
            context=resolved_context,
        )
        reason = self._reason_for_selection(
            label="codex_execution_prompt",
            prompt=text,
            token_estimate=token_estimate,
            tier=tier,
            provider=ModelProvider.OPENROUTER,
            context=resolved_context,
        )
        return tier, reason

    def validation_reason(
        self,
        *,
        label: str | None,
        response_text: str,
        prompt: str,
        context: ModelRequestContext | None = None,
    ) -> str | None:
        cleaned = response_text.strip()
        resolved_context = context or self.default_context(label=label, prompt=prompt)
        if not cleaned:
            return "empty_response"

        if label == "browser_synthesis" and resolved_context.evidence_quality in self._LOW_EVIDENCE:
            return "low_evidence_quality"

        if label not in self._JSON_LABELS:
            return None

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            return "invalid_json"

        if label == "assistant_decision":
            if str(payload.get("mode", "")).strip().upper() not in {"ANSWER", "ACT", "EXECUTE"}:
                return "assistant_decision_invalid_mode"
            if not str(payload.get("reasoning", "")).strip():
                return "assistant_decision_missing_reasoning"
            return None

        if label == "planner_create_plan":
            subtasks = payload.get("subtasks")
            if not isinstance(subtasks, list) or not subtasks:
                return "planner_missing_subtasks"
            return None

        if label == "router_agent_select":
            if not str(payload.get("agent_name", "")).strip():
                return "router_missing_agent_name"
            if not str(payload.get("reasoning", "")).strip():
                return "router_missing_reasoning"
            return None

        if label == "goal_evaluate":
            required = {"satisfied", "reasoning", "missing"}
            if not required.issubset(payload):
                return "goal_evaluate_missing_fields"
            if not isinstance(payload.get("missing"), list):
                return "goal_evaluate_invalid_missing"
            if not str(payload.get("reasoning", "")).strip():
                return "goal_evaluate_missing_reasoning"
            confidence = payload.get("completion_confidence")
            try:
                confidence_value = float(confidence if confidence is not None else 0.0)
            except (TypeError, ValueError):
                confidence_value = 0.0
            if (
                resolved_context.risk_level in self._HIGH_RISK_LEVELS
                and confidence_value < 0.45
            ):
                return "low_confidence_critical_evaluation"
            return None

        if label == "browser_backend_selection":
            if str(payload.get("backend", "")).strip().lower() not in {"playwright", "browser_use"}:
                return "browser_backend_invalid"
            return None

        if label == "browser_target_resolution":
            confidence = str(payload.get("confidence", "")).strip().lower()
            resolved_url = payload.get("resolved_url")
            if resolved_url and confidence not in {"high", "very_high"}:
                return "browser_target_low_confidence"
            return None

        if label == "memory_extract":
            if not isinstance(payload.get("facts", []), list):
                return "memory_extract_invalid"
            return None

        if label == "reminder_parse":
            return None

        return None

    def estimate_tokens(self, prompt: str, system_prompt: str | None = None) -> int:
        combined = f"{system_prompt or ''}\n{prompt}".strip()
        if not combined:
            return 0
        return max(1, math.ceil(len(combined) / 4))

    def describe_strategy(self) -> str:
        if not self.enabled():
            return f"OpenRouter via {settings.openrouter_model}"
        provider_note = "OpenRouter"
        if self._openai_tier_3_ready():
            provider_note = "OpenRouter + OpenAI Tier 3 direct"
        return (
            f"Dynamic model routing via {provider_note} "
            f"(Tier 1: {self.tier_models[ModelTier.TIER_1]}, "
            f"Tier 2: {self.tier_models[ModelTier.TIER_2]}, "
            f"Tier 3: {self.openai_tier_3_model or self.tier_models[ModelTier.TIER_3]})"
        )

    def _base_tier(
        self,
        *,
        label: str | None,
        prompt: str,
        token_estimate: int,
        context: ModelRequestContext,
    ) -> str:
        if context.verifier_failed or context.reviewer_rejected:
            return ModelTier.TIER_3
        if context.replan_count > 0 and context.requires_review:
            return ModelTier.TIER_3
        if (
            context.risk_level in self._HIGH_RISK_LEVELS
            and context.task_complexity in self._HIGH_COMPLEXITY_LEVELS
        ):
            return ModelTier.TIER_3
        if self._is_structured_fast_path(context):
            return ModelTier.TIER_1
        if (
            context.selected_agent in {"planner_agent", "browser_agent", "reviewer_agent", "verifier_agent"}
            or context.selected_lane in {"execution_flow", "browser", "verification"}
            or context.requires_tool_use
            or context.requires_review
            or context.task_complexity in {"medium", "high", "critical"}
            or context.risk_level in {"medium", "high", "critical"}
        ):
            return ModelTier.TIER_2

        lowered = prompt.lower()
        if self._prompt_needs_frontier(lowered, token_estimate=token_estimate):
            return ModelTier.TIER_3
        if label == "goal_evaluate" and self._prompt_looks_critical(lowered):
            return ModelTier.TIER_3
        if label in self._TIER_2_LABELS:
            return ModelTier.TIER_2
        if label in self._TIER_1_LABELS:
            if self._prompt_needs_balanced(lowered, token_estimate=token_estimate):
                return ModelTier.TIER_2
            return ModelTier.TIER_1
        if self._prompt_needs_balanced(lowered, token_estimate=token_estimate):
            return ModelTier.TIER_2
        return self._default_tier()

    def _reason_for_selection(
        self,
        *,
        label: str | None,
        prompt: str,
        token_estimate: int,
        tier: str,
        provider: str,
        context: ModelRequestContext,
    ) -> str:
        if tier == ModelTier.TIER_3:
            if context.verifier_failed:
                return "Verifier failure escalated this request to Tier 3."
            if context.reviewer_rejected:
                return "Reviewer rejection escalated this request to Tier 3."
            if context.replan_count > 0 and context.requires_review:
                return "A replanned review-sensitive request now needs Tier 3."
            if provider == ModelProvider.OPENAI:
                return "Tier 3 is using the direct OpenAI provider for the strongest available reasoning."
            if label == "goal_evaluate":
                return "Final verification and failure-sensitive evaluation require the strongest tier."
            if label == "codex_execution_prompt":
                return "Serious coding execution needs stronger reasoning guidance."
            return "Structured task context marked this request as critical or failure-sensitive, so it starts on Tier 3."
        if tier == ModelTier.TIER_2:
            if context.selected_agent == "browser_agent":
                return "Browser interpretation should start on the balanced tier for grounded synthesis."
            if context.selected_agent in {"planner_agent", "router"}:
                return "Planning and delegation should start on the balanced tier."
            if context.selected_agent == "verifier_agent":
                return "Verification work should start on the balanced tier unless a failure signal forces Tier 3."
            if label == "assistant_compose":
                return "User-facing execution summaries should start on the balanced tier."
            return f"Structured context and prompt size ({token_estimate} estimated tokens) warrant Tier 2."
        if self._is_structured_fast_path(context):
            return "This is a lightweight assistant, memory, clarification, or simple fast-action request, so Tier 1 is the efficient starting point."
        return "No strong complexity or risk signals were present, so Tier 1 is the default starting point."

    def _provider_for_tier(
        self,
        *,
        tier: str,
        context: ModelRequestContext,
    ) -> tuple[str, str]:
        if tier == ModelTier.TIER_3 and self._openai_tier_3_ready():
            return ModelProvider.OPENAI, self.openai_tier_3_model or self.tier_models[ModelTier.TIER_3]
        return ModelProvider.OPENROUTER, self.tier_models[tier]

    def _default_tier(self) -> str:
        default_map = {
            1: ModelTier.TIER_1,
            2: ModelTier.TIER_2,
            3: ModelTier.TIER_3,
        }
        return default_map.get(settings.model_default_tier, ModelTier.TIER_2)

    def _is_structured_fast_path(self, context: ModelRequestContext) -> bool:
        if context.intent_label in self._FAST_PATH_INTENTS:
            return True
        return (
            context.request_mode == "answer"
            and not context.requires_tool_use
            and not context.requires_review
            and context.selected_lane in {"assistant", "memory", "clarify"}
        )

    def _openai_tier_3_ready(self) -> bool:
        return bool(
            settings.openai_enabled
            and settings.openai_api_key
            and self.openai_tier_3_model
        )

    def _configured_model(self, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        lowered = cleaned.lower()
        if lowered in _PLACEHOLDER_MODEL_VALUES:
            return None
        if lowered.startswith(("your-", "example-", "placeholder-")):
            return None
        if "placeholder" in lowered:
            return None
        return cleaned

    def _prompt_needs_balanced(self, prompt: str, *, token_estimate: int) -> bool:
        if token_estimate >= 450:
            return True
        markers = (
            "research",
            "summarize tradeoffs",
            "compare",
            "browser objective",
            "subtask objective",
            "create structured plan",
            "bounded_task_execution",
            "review",
            "verify",
            "coding task",
            "implement",
            "refactor",
            "failing test",
            "multi-step",
        )
        return any(marker in prompt for marker in markers)

    def _prompt_needs_frontier(self, prompt: str, *, token_estimate: int) -> bool:
        if token_estimate >= 1200:
            return True
        markers = (
            "objective_completion",
            "keep going until",
            "verifier",
            "reviewer verification",
            "anti-fake-completion",
            "critical",
            "blocked",
            "incomplete",
            "low-confidence",
            "hard reasoning",
            "complex multi-step",
            "failing auth module",
            "regression",
        )
        return any(marker in prompt for marker in markers)

    def _prompt_looks_critical(self, prompt: str) -> bool:
        markers = (
            "objective_completion",
            "reviewer",
            "verifier",
            "blocked",
            "missing",
            "codex",
            "browser evidence",
        )
        return any(marker in prompt.lower() for marker in markers)
