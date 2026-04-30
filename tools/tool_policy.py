"""Cheap-first tool selection policy scaffolding.

This module keeps small, explicit guardrails around tool cost and availability.
It does not replace LLM-led planning; it gives planners and tests a stable policy
surface for reasoning about capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from tools.capability_manifest import CapabilityCatalog, CapabilitySnapshot, build_capability_catalog


@dataclass(frozen=True)
class ToolPolicyDecision:
    """Policy guidance for a user request before or during planning."""

    preferred_capability_ids: tuple[str, ...]
    required_capability_ids: tuple[str, ...] = ()
    blocked: bool = False
    blocker: str | None = None
    rationale: str = ""
    capability_sequence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)


class ToolCostPolicy:
    """Small policy layer for cheap/local preference and premium blockers."""

    premium_ids = {"manus_agent", "openai_agents_sdk"}

    def __init__(self, catalog: CapabilityCatalog | None = None) -> None:
        self.catalog = catalog or build_capability_catalog()

    def assess(self, request: str) -> ToolPolicyDecision:
        lowered = " ".join(request.lower().split())
        explicit_manus = "manus" in lowered
        explicit_browser_use = "browser use" in lowered or "browser-use" in lowered

        if explicit_manus:
            return self._explicit_unavailable_or_allowed("manus_agent", "Manus")
        if explicit_browser_use:
            return self._explicit_unavailable_or_allowed("browser_use_browser", "Browser Use")

        sequence = self.infer_capability_sequence(request)
        if "browser" in sequence:
            browser_capability = self._preferred_browser_capability(lowered)
            preferred = tuple(
                browser_capability if capability == "browser" else capability for capability in sequence
            )
            return ToolPolicyDecision(
                preferred_capability_ids=preferred,
                capability_sequence=preferred,
                rationale="Prefer the local Playwright-backed browser path for direct browser requests; reserve stronger browser backends for future complex escalation.",
            )

        if sequence:
            return ToolPolicyDecision(
                preferred_capability_ids=sequence,
                capability_sequence=sequence,
                rationale="Use the lowest-cost local capability sequence that matches the request.",
            )

        return ToolPolicyDecision(
            preferred_capability_ids=("assistant_direct",),
            capability_sequence=("assistant_direct",),
            rationale="No tool is required; answer directly on the assistant path.",
        )

    def infer_capability_sequence(self, request: str) -> tuple[str, ...]:
        lowered = " ".join(request.lower().split())
        sequence: list[str] = []
        if self._looks_like_browser_request(lowered):
            sequence.append("browser")
        elif self._looks_like_search_research_request(lowered):
            sequence.append("source_backed_search")
        if self._looks_like_file_request(lowered):
            sequence.append("file_tool")
        if self._looks_like_reminder_request(lowered):
            sequence.append("reminder_scheduler")
        if self._looks_like_calendar_request(lowered):
            sequence.append("google_calendar")
        if self._looks_like_tasks_request(lowered):
            sequence.append("google_tasks")
        if self._looks_like_memory_request(lowered):
            sequence.append("memory_context")
        if self._looks_like_coding_request(lowered):
            sequence.append("codex_cli")
        return tuple(dict.fromkeys(sequence))

    def _explicit_unavailable_or_allowed(self, capability_id: str, label: str) -> ToolPolicyDecision:
        snapshot = self.catalog.snapshot_for(capability_id)
        if snapshot is None:
            return ToolPolicyDecision(
                preferred_capability_ids=(),
                required_capability_ids=(capability_id,),
                blocked=True,
                blocker=f"{label} is not in the capability manifest yet.",
                rationale="Explicit premium request cannot proceed without a known capability record.",
            )
        if not snapshot.is_live:
            requirements = snapshot.missing_config or snapshot.config_requirements
            setup = f" Missing setup: {', '.join(requirements)}." if requirements else ""
            return ToolPolicyDecision(
                preferred_capability_ids=(),
                required_capability_ids=(capability_id,),
                blocked=True,
                blocker=f"{label} is {snapshot.status}, so I can't use it for real yet.{setup}",
                rationale="Explicit premium request is blocked until the capability is configured and enabled.",
            )
        return ToolPolicyDecision(
            preferred_capability_ids=(capability_id,),
            required_capability_ids=(capability_id,),
            rationale=f"The user explicitly requested {label}, and the capability is live.",
        )

    def _preferred_browser_capability(self, lowered: str) -> str:
        complex_markers = (
            "log in",
            "login",
            "book",
            "buy",
            "checkout",
            "fill out",
            "multi-step",
            "portal",
            "captcha",
            "2fa",
        )
        if any(marker in lowered for marker in complex_markers):
            target = self.catalog.snapshot_for("browser_use_browser")
            if target is not None and target.is_live:
                return "browser_use_browser"
        return "playwright_browser"

    def _looks_like_browser_request(self, lowered: str) -> bool:
        return bool(
            re.search(r"\b(open|browse|go to|navigate|inspect|summarize|book|buy|checkout)\b", lowered)
            and re.search(r"\b(site|website|page|browser|url|[a-z0-9.-]+\.[a-z]{2,}|cnn|espn|wikipedia)\b", lowered)
        )

    def _looks_like_search_research_request(self, lowered: str) -> bool:
        return any(
            marker in lowered
            for marker in (
                "research",
                "compare",
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

    def _looks_like_file_request(self, lowered: str) -> bool:
        return bool(
            re.search(r"\b(save|write|create|read|list|file|\.txt|\.md|\.py|\.json)\b", lowered)
            and ("headline" in lowered or "file" in lowered or re.search(r"\.[a-z0-9]{2,5}\b", lowered))
        )

    def _looks_like_reminder_request(self, lowered: str) -> bool:
        return "remind me" in lowered or "set a reminder" in lowered or "schedule a reminder" in lowered

    def _looks_like_calendar_request(self, lowered: str) -> bool:
        if "reminder" in lowered:
            return False
        return any(
            marker in lowered
            for marker in (
                "calendar",
                "what do i have today",
                "what do i have tomorrow",
                "what do i have this week",
                "next event",
                "appointment",
            )
        )

    def _looks_like_tasks_request(self, lowered: str) -> bool:
        if any(token in lowered for token in ("calendar", "reminder", "gmail", "email")):
            return False
        return any(
            marker in lowered
            for marker in (
                "my tasks",
                "task list",
                "to-do",
                "todo",
                "tasks due today",
                "what tasks do i have",
            )
        )

    def _looks_like_memory_request(self, lowered: str) -> bool:
        return "remember" in lowered or "what do you remember" in lowered or "memory" in lowered

    def _looks_like_coding_request(self, lowered: str) -> bool:
        return bool(
            re.search(r"\b(build|implement|refactor|debug|fix)\b", lowered)
            and re.search(r"\b(code|test|tests|module|feature|bug|regression)\b", lowered)
        )


def build_tool_cost_policy(catalog: CapabilityCatalog | None = None) -> ToolCostPolicy:
    """Create the default cheap-first tool policy."""

    return ToolCostPolicy(catalog=catalog)
