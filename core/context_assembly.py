"""Shared context assembly for the operator and future subagents."""

from __future__ import annotations

from dataclasses import dataclass

from agents.catalog import AgentCatalog, build_agent_catalog
from core.operator_context import OperatorContextService, RuntimeSnapshot, operator_context
from core.prompt_library import PromptLibrary, get_prompt_library
from tools.capability_manifest import CapabilityCatalog, build_capability_catalog
from tools.registry import ToolRegistry, build_default_tool_registry


@dataclass
class PromptContextBundle:
    """Prompt-ready context assembled from instructions, runtime state, and capabilities."""

    role: str
    instruction_text: str
    runtime_snapshot: RuntimeSnapshot
    capability_catalog: CapabilityCatalog
    agent_catalog: AgentCatalog
    context_profile: str
    user_message: str | None = None
    goal: str | None = None
    extra_sections: list[str] | None = None

    def to_prompt_block(self) -> str:
        live_lines, non_live_lines = self.capability_catalog.user_visible_lines()
        sections = [
            f"role: {self.role}",
            f"context_profile: {self.context_profile}",
            "instructions:",
            self.instruction_text,
            self.agent_catalog.summary_block(),
            self.capability_catalog.policy_block(),
            self.capability_catalog.ceo_context().prompt_block(),
            "runtime_state:",
            self.runtime_snapshot.to_prompt_block(),
            "capabilities_live:",
            *[f"- {line}" for line in live_lines],
            "capabilities_not_live:",
            *[f"- {line}" for line in non_live_lines],
        ]
        if self.goal:
            sections.extend(["goal:", self.goal])
        if self.user_message:
            sections.extend(["user_message:", self.user_message])
        for section in self.extra_sections or []:
            sections.append(section)
        return "\n".join(sections)


class ContextAssembler:
    """Central place for building prompt context across operator layers."""

    role_instruction_files = {
        "operator": [
            "instructions/operator_identity.md",
            "instructions/operator.md",
            "instructions/operator_escalation.md",
            "instructions/completion_control.md",
            "instructions/tool_selection_policy.md",
        ],
        "planning_agent": [
            "instructions/operator_identity.md",
            "instructions/planning_agent.md",
            "instructions/delegation_contract.md",
            "instructions/operator_escalation.md",
            "instructions/completion_control.md",
            "instructions/evidence_expectations.md",
            "instructions/tool_selection_policy.md",
        ],
        "memory_agent": [
            "instructions/operator_identity.md",
            "instructions/memory_agent.md",
        ],
        "conversation": [
            "instructions/operator_identity.md",
            "instructions/operator.md",
            "instructions/capability_honesty.md",
        ],
        "router": [
            "instructions/operator_identity.md",
            "instructions/delegation_contract.md",
            "instructions/operator_escalation.md",
            "instructions/tool_selection_policy.md",
        ],
        "reviewer": [
            "instructions/operator_identity.md",
            "instructions/reviewer_agent.md",
            "instructions/completion_control.md",
            "instructions/evidence_expectations.md",
        ],
        "browser_agent": [
            "instructions/operator_identity.md",
            "instructions/browser_agent.md",
            "instructions/capability_honesty.md",
        ],
        "coding_agent": [
            "instructions/operator_identity.md",
            "instructions/coding_agent.md",
            "instructions/evidence_expectations.md",
        ],
        "research_agent": [
            "instructions/operator_identity.md",
            "instructions/research_agent.md",
            "instructions/capability_honesty.md",
        ],
        "communications_agent": [
            "instructions/operator_identity.md",
            "instructions/communications_agent.md",
            "instructions/capability_honesty.md",
        ],
        "personal_ops_agent": [
            "instructions/operator_identity.md",
            "instructions/personal_ops_agent.md",
            "instructions/scheduling_agent.md",
            "instructions/communications_agent.md",
            "instructions/capability_honesty.md",
        ],
        "reminder_scheduler_agent": [
            "instructions/operator_identity.md",
            "instructions/reminder_agent.md",
            "instructions/capability_honesty.md",
            "instructions/evidence_expectations.md",
        ],
    }
    always_include_instruction_files = [
        "instructions/scheduling_agent.md",
    ]

    def __init__(
        self,
        *,
        operator_context_service: OperatorContextService | None = None,
        prompt_library: PromptLibrary | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        tool_registry: ToolRegistry | None = None,
        agent_catalog: AgentCatalog | None = None,
    ) -> None:
        resolved_registry = tool_registry or build_default_tool_registry()
        self.agent_catalog = agent_catalog or build_agent_catalog()
        self.operator_context = operator_context_service or operator_context
        self.prompt_library = prompt_library or get_prompt_library()
        self.capability_catalog = capability_catalog or build_capability_catalog(
            tool_registry=resolved_registry,
            agent_catalog=self.agent_catalog,
        )

    def build(
        self,
        role: str,
        *,
        user_message: str | None = None,
        goal: str | None = None,
        extra_sections: list[str] | None = None,
        context_profile: str | None = None,
    ) -> PromptContextBundle:
        instruction_paths = self.role_instruction_files.get(role, ["instructions/operator.md"])
        instruction_paths = list(dict.fromkeys([*instruction_paths, *self.always_include_instruction_files]))
        instruction_text = self.prompt_library.read_many(instruction_paths)
        focus_text = user_message or goal
        resolved_profile = context_profile or self._infer_context_profile(
            role,
            user_message=user_message,
            goal=goal,
        )
        return PromptContextBundle(
            role=role,
            instruction_text=instruction_text,
            runtime_snapshot=self.operator_context.build_runtime_snapshot(
                focus_text=focus_text,
                context_profile=resolved_profile,
            ),
            capability_catalog=self.capability_catalog,
            agent_catalog=self.agent_catalog,
            context_profile=resolved_profile,
            user_message=user_message,
            goal=goal,
            extra_sections=extra_sections or [],
        )

    def _infer_context_profile(
        self,
        role: str,
        *,
        user_message: str | None = None,
        goal: str | None = None,
    ) -> str:
        if role not in {"operator", "conversation"}:
            return "task"
        focus_text = (user_message or goal or "").lower().strip()
        if not focus_text:
            return "minimal"
        if self._is_social_prompt(focus_text):
            return "minimal"
        if self._is_memory_prompt(focus_text):
            return "memory"
        if self._is_continuity_prompt(focus_text):
            return "continuity"
        return "task"

    def _is_social_prompt(self, message: str) -> bool:
        normalized = " ".join(message.split())
        return normalized in {
            "hi",
            "hello",
            "hey",
            "yo",
            "what's up",
            "whats up",
            "good morning",
            "good afternoon",
            "good evening",
            "thanks",
            "thank you",
        }

    def _is_memory_prompt(self, message: str) -> bool:
        if any(
            phrase in message
            for phrase in (
                "what do you remember",
                "what do you know about me",
                "what preference did i tell you earlier",
                "what did i say two chats ago",
            )
        ):
            return True
        if message.startswith(("what is my ", "what's my ", "where did i ", "where is my ", "what did i ")):
            return True
        return (
            ("remember" in message or "know" in message)
            and any(
                token in message
                for token in ("about me", "about this project", "about project sovereign", "earlier")
            )
        )

    def _is_continuity_prompt(self, message: str) -> bool:
        if any(
            phrase in message
            for phrase in (
                "what were we focused on before",
                "what are you working on",
                "what did you just do",
                "what was the last task",
                "continue",
            )
        ):
            return True
        return any(
            phrase in message
            for phrase in (
                "what are we working on",
                "where were we at",
                "what still needs work",
                "what's next for sovereign",
                "what is next for sovereign",
            )
        )
