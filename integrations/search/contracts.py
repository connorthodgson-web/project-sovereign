"""Provider-neutral contracts for source-backed search and research."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from core.models import utcnow


class SearchSource(BaseModel):
    """One source returned by a search provider."""

    title: str
    url: str
    snippet: str | None = None
    date: str | None = None


class SearchRequest(BaseModel):
    """Normalized research request sent to a search provider."""

    query: str
    max_results: int = 5


class SearchResult(BaseModel):
    """Source-backed search answer produced by a provider."""

    query: str
    provider: str
    answer: str
    sources: list[SearchSource] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: utcnow().isoformat())
    raw_metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def has_source_evidence(self) -> bool:
        return bool(
            self.answer.strip()
            and any(source.title.strip() and source.url.strip() for source in self.sources)
        )


class SearchProvider(Protocol):
    """Minimal interface every live search provider must implement."""

    provider_name: str

    def is_configured(self) -> bool:
        """Return whether the provider has enough runtime config to run."""

    def search(self, request: SearchRequest) -> SearchResult:
        """Run a source-backed search."""


class SearchProviderError(RuntimeError):
    """Raised when a configured search provider cannot complete a search."""

