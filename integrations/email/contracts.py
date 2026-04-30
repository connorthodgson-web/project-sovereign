"""Contracts for future email provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class EmailRequest(BaseModel):
    recipients: list[str]
    subject: str
    body: str
    metadata: dict[str, str] = Field(default_factory=dict)


class EmailResult(BaseModel):
    success: bool
    summary: str
    delivery_id: str | None = None
    blockers: list[str] = Field(default_factory=list)


class EmailAdapter(ABC):
    @abstractmethod
    def send(self, request: EmailRequest) -> EmailResult:
        """Send or stage an email through a provider adapter."""
