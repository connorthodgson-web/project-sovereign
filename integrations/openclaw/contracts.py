"""Contracts for optional OpenClaw-style runtime bridging."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class BridgeTaskRequest(BaseModel):
    goal: str
    role: str
    context: dict[str, object] = Field(default_factory=dict)


class BridgeTaskResult(BaseModel):
    success: bool
    summary: str
    structured_output: dict[str, object] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)


class OpenClawBridgeAdapter(ABC):
    @abstractmethod
    def dispatch(self, request: BridgeTaskRequest) -> BridgeTaskResult:
        """Dispatch work to an external runtime bridge."""
