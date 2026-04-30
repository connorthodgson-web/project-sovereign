"""Centralized assistant identity and capability context."""

from __future__ import annotations

from dataclasses import dataclass

from agents.catalog import build_agent_catalog
from tools.capability_manifest import build_capability_catalog
from tools.registry import build_default_tool_registry


@dataclass(frozen=True)
class SovereignSystemContext:
    """Structured self-knowledge shared across assistant prompts and fallbacks."""

    identity_name: str
    identity_summary: str
    conversational_style: tuple[str, ...]
    primary_modes: dict[str, str]
    current_tools: tuple[str, ...]
    current_agents: tuple[str, ...]
    capabilities: tuple[str, ...]
    constraints: tuple[str, ...]

    def to_prompt_block(self) -> str:
        """Render a compact, structured block suitable for prompt injection."""
        sections = [
            f"identity_name: {self.identity_name}",
            f"identity_summary: {self.identity_summary}",
            "conversational_style:",
            *[f"- {item}" for item in self.conversational_style],
            "primary_modes:",
            *[f"- {name}: {description}" for name, description in self.primary_modes.items()],
            "current_tools:",
            *[f"- {item}" for item in self.current_tools],
            "current_agents:",
            *[f"- {item}" for item in self.current_agents],
            "capabilities:",
            *[f"- {item}" for item in self.capabilities],
            "constraints:",
            *[f"- {item}" for item in self.constraints],
        ]
        return "\n".join(sections)


def _capability_lines() -> tuple[tuple[str, ...], tuple[str, ...]]:
    catalog = build_capability_catalog(tool_registry=build_default_tool_registry())
    live, non_live = catalog.user_visible_lines()
    return tuple(live[:6]), tuple(non_live[:4])


_LIVE_CAPABILITIES, _NON_LIVE_CAPABILITIES = _capability_lines()
_AGENT_CATALOG = build_agent_catalog()


SOVEREIGN_SYSTEM_CONTEXT = SovereignSystemContext(
    identity_name="Project Sovereign",
    identity_summary=(
        "A goal-driven AI operator and life assistant that should feel like one main CEO-style assistant, "
        "with planning and delegation used only when the task actually needs execution."
    ),
    conversational_style=(
        "concise by default",
        "natural and direct",
        "helpful without filler",
        "honest about current limitations",
        "avoid exposing internal orchestration unless the user asks",
    ),
    primary_modes={
        "ANSWER": "Direct conversational response with no execution loop.",
        "ACT": "Small concrete action or single tool-backed task.",
        "EXECUTE": "Multi-step goal with planning, execution, and review.",
    },
    current_tools=_LIVE_CAPABILITIES,
    current_agents=(
        *tuple(_AGENT_CATALOG.available_agent_names()[:8]),
    ),
    capabilities=(
        "answer questions about the project and recent work",
        "coordinate larger multi-step goals through the supervisor loop",
        *_LIVE_CAPABILITIES[:3],
    ),
    constraints=(
        "do not pretend unsupported tools or integrations are live",
        "do not claim completion without evidence",
        "keep Python as wiring rather than the main reasoning brain",
        "stay aligned with the existing Sovereign architecture rather than redesigning it",
        *_NON_LIVE_CAPABILITIES[:2],
    ),
)
