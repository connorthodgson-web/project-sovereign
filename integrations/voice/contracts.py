"""Contracts for future voice and call integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class VoiceRequest(BaseModel):
    action: str
    content: str


class VoiceResult(BaseModel):
    success: bool
    summary: str
    artifact_path: str | None = None
    blockers: list[str] = Field(default_factory=list)


class VoiceAdapter(ABC):
    @abstractmethod
    def execute(self, request: VoiceRequest) -> VoiceResult:
        """Run a voice, TTS, STT, or call-oriented action."""
