"""Contracts for retrieval and vector backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class RetrievalRequest(BaseModel):
    query: str
    filters: dict[str, str] = Field(default_factory=dict)
    top_k: int = 5


class RetrievalResult(BaseModel):
    success: bool
    summary: str
    matches: list[dict[str, object]] = Field(default_factory=list)
    backend: str
    blockers: list[str] = Field(default_factory=list)


class RetrievalAdapter(ABC):
    @abstractmethod
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Run semantic or hybrid retrieval against a backend."""
