"""Contracts for future browser automation adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class BrowserExecutionRequest(BaseModel):
    action: str = "open"
    objective: str
    start_url: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    preferred_backend: str | None = None
    allow_backend_fallback: bool = True
    require_screenshot: bool = False
    screenshot_policy: str = "on_failure"
    headless: bool = True
    local_visible: bool = False
    timeout_ms: int = 20000
    max_steps: int | None = 20


class BrowserExecutionResult(BaseModel):
    success: bool
    summary: str
    backend: str = "unknown"
    structured_result: dict[str, object] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    user_action_required: list[str] = Field(default_factory=list)


class BrowserAdapter(ABC):
    """Boundary for Browser Use, Playwright, or future browser runtimes."""

    @abstractmethod
    def execute(self, request: BrowserExecutionRequest) -> BrowserExecutionResult:
        """Execute a browser task and return structured evidence."""
