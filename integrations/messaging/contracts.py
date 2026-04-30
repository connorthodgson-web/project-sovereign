"""Contracts for future messaging and notification adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class MessagingRequest(BaseModel):
    channel: str
    recipient: str
    message: str
    metadata: dict[str, str] = Field(default_factory=dict)


class MessagingResult(BaseModel):
    success: bool
    summary: str
    delivery_id: str | None = None
    blockers: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class MessagingAdapter(ABC):
    @abstractmethod
    def send(self, request: MessagingRequest) -> MessagingResult:
        """Deliver or stage a message on an external channel."""
