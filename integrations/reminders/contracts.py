"""Contracts for scheduler and reminder backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class ReminderRequest(BaseModel):
    summary: str
    schedule: str
    delivery_channel: str = "slack"
    recipient: str | None = None
    channel: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ReminderResult(BaseModel):
    success: bool
    summary: str
    reminder_id: str | None = None
    blockers: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class ReminderAdapter(ABC):
    @abstractmethod
    def schedule(self, request: ReminderRequest) -> ReminderResult:
        """Create or update a reminder in a scheduler backend."""
