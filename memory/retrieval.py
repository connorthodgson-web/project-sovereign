"""Memory retrieval helpers with pluggable retrieval strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Protocol

from memory.memory_store import MemoryFact, MemoryStore, memory_store
from memory.provider import MemoryBackend


@dataclass
class RetrievalQuery:
    """Structured retrieval request shared across backends."""

    text: str
    layers: tuple[str, ...] = ("user", "project", "operational")
    limit: int = 8
    metadata: dict[str, Any] | None = None


@dataclass
class RetrievalMatch:
    """Ranked memory candidate returned by a backend."""

    fact: MemoryFact
    score: float = 0.0
    reason: str | None = None


class RetrievalBackend(Protocol):
    """Contract for memory retrieval backends."""

    name: str

    def search(self, query: RetrievalQuery) -> list[RetrievalMatch]:
        """Return ranked memory matches for the query."""


@dataclass
class RetrievalResult:
    """Structured retrieval output for future semantic/vector backends."""

    backend: str
    strategy: str
    matches: list[RetrievalMatch]


class KeywordRetrievalBackend:
    """Current keyword-based retrieval backend."""

    name = "keyword"

    def __init__(self, store: MemoryBackend | None = None) -> None:
        self.store = store or memory_store

    def search(self, query: RetrievalQuery) -> list[RetrievalMatch]:
        matches = self.store.search_facts(query.text, layers=query.layers)[: query.limit]
        return [
            RetrievalMatch(fact=fact, score=float(query.limit - index), reason="keyword_ranked")
            for index, fact in enumerate(matches)
        ]


class SemanticRetrievalBackend:
    """Semantic retrieval through the active memory provider when available."""

    name = "semantic"

    def __init__(self, store: MemoryBackend | None = None) -> None:
        self.store = store or memory_store

    def search(self, query: RetrievalQuery) -> list[RetrievalMatch]:
        provider_name = getattr(self.store, "provider_name", getattr(self.store, "name", ""))
        if provider_name != "chroma":
            return []
        matches = self.store.search_facts(query.text, layers=query.layers)[: query.limit]
        return [
            RetrievalMatch(fact=fact, score=float(query.limit - index), reason="semantic_ranked")
            for index, fact in enumerate(matches)
        ]


class MemoryRetriever:
    """Coordinates retrieval from the current memory backend."""

    def __init__(self, store: MemoryBackend | None = None) -> None:
        self.store = store or memory_store
        self._backends: dict[str, RetrievalBackend] = {
            "keyword": KeywordRetrievalBackend(self.store),
            "semantic": SemanticRetrievalBackend(self.store),
        }

    def search(
        self,
        query: str,
        *,
        strategy: str = "keyword",
        limit: int = 8,
    ) -> list[str]:
        """Return relevant memory fragments for the query."""

        return [match.fact.value for match in self.retrieve(query, strategy=strategy, limit=limit).matches]

    def retrieve(
        self,
        query: str,
        *,
        strategy: str = "keyword",
        limit: int = 8,
    ) -> RetrievalResult:
        """Return structured retrieval output for the requested strategy."""

        backend = self._backends.get(strategy) or self._backends["keyword"]
        retrieval_query = RetrievalQuery(text=query, limit=limit)
        matches = backend.search(retrieval_query)
        if not matches and strategy == "semantic":
            return RetrievalResult(
                backend=self._backends["keyword"].name,
                strategy="semantic_fallback_to_keyword",
                matches=self._backends["keyword"].search(retrieval_query),
            )
        return RetrievalResult(
            backend=backend.name,
            strategy=strategy if strategy in self._backends else "keyword",
            matches=matches,
        )
