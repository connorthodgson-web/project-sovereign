"""Contracts for future calendar provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class CalendarRequest(BaseModel):
    action: str
    event_title: str | None = None
    date_range: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CalendarResult(BaseModel):
    success: bool
    summary: str
    events: list[dict[str, object]] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class CalendarAdapter(ABC):
    @abstractmethod
    def execute(self, request: CalendarRequest) -> CalendarResult:
        """Run a calendar action against a provider adapter."""
