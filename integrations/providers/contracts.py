"""Contracts for future model/provider routing layers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class ModelRoutingRequest(BaseModel):
    intent: str
    risk_level: str = "normal"
    preferred_capabilities: list[str] = Field(default_factory=list)


class ModelRoutingDecision(BaseModel):
    provider: str
    model: str
    reasoning: str


class ProviderRouter(ABC):
    @abstractmethod
    def route(self, request: ModelRoutingRequest) -> ModelRoutingDecision:
        """Choose a provider/model pair for a given task profile."""
