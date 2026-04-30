"""Compiled prompt context model for Memory Platform v2."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CompiledPromptContext:
    """Prompt memory grouped by source and lifetime.

    core_memory is stable and usually useful. retrieved_memory is query-scoped.
    Personal Ops and operational state stay isolated so prompts can include them
    only when they are relevant to the user's current request.
    """

    core_memory: list[str] = field(default_factory=list)
    retrieved_memory: list[str] = field(default_factory=list)
    personal_ops_state: list[str] = field(default_factory=list)
    operational_state: list[str] = field(default_factory=list)
    short_term_state: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        sections = [
            "compiled_prompt_context:",
            "core_memory:",
            *[f"- {item}" for item in self.core_memory],
            "retrieved_memory:",
            *[f"- {item}" for item in self.retrieved_memory],
            "personal_ops_state:",
            *[f"- {item}" for item in self.personal_ops_state],
            "operational_state:",
            *[f"- {item}" for item in self.operational_state],
            "short_term_state:",
            *[f"- {item}" for item in self.short_term_state],
        ]
        return "\n".join(sections)
