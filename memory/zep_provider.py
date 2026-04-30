"""Zep-backed durable memory provider."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any

from app.config import settings
from core.models import utcnow
from memory.types import ConversationTurn, MemoryFact

try:
    from zep_cloud import Message
    from zep_cloud.client import Zep
except ImportError:  # pragma: no cover - optional at runtime
    Message = None  # type: ignore[assignment]
    Zep = None  # type: ignore[assignment]


class ZepMemoryProvider:
    """Stores durable facts and conversation turns in Zep."""

    name = "zep"
    max_turns = 24

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        user_id: str | None = None,
        thread_id: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key or settings.zep_api_key
        self.base_url = base_url or settings.zep_base_url
        self.user_id = user_id or settings.zep_user_id
        self.thread_id = thread_id or settings.zep_thread_id
        self._lock = Lock()
        self._bootstrapped = False
        self._client = client or self._build_client()

    @property
    def available(self) -> bool:
        return self._client is not None and bool(self.api_key)

    def record_turn(self, role: str, content: str) -> None:
        cleaned = " ".join(content.split())
        if not cleaned:
            return
        self._ensure_ready()
        ignore_roles = ["assistant"] if role == "assistant" else None
        message_type = Message or SimpleNamespace
        self._client.thread.add_messages(
            self.thread_id,
            messages=[
                message_type(
                    role=role if role in {"system", "assistant", "user", "tool", "function"} else "user",
                    content=cleaned,
                    created_at=utcnow().isoformat(),
                    metadata={"source": "project_sovereign"},
                )
            ],
            ignore_roles=ignore_roles,
        )

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]:
        self._ensure_ready()
        response = self._client.thread.get(self.thread_id, lastn=limit or self.max_turns)
        turns: list[ConversationTurn] = []
        for message in (response.messages or []):
            turns.append(
                ConversationTurn(
                    role=getattr(message, "role", "user"),
                    content=getattr(message, "content", ""),
                    created_at=getattr(message, "created_at", None) or utcnow().isoformat(),
                )
            )
        return turns[-(limit or self.max_turns) :]

    def upsert_fact(
        self,
        *,
        layer: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "system",
    ) -> None:
        cleaned_value = " ".join(value.split())
        cleaned_key = " ".join(key.lower().split())
        if not cleaned_key or not cleaned_value:
            return
        self._ensure_ready()
        self.delete_fact(layer=layer, key=cleaned_key, category=category)
        self._client.graph.add_fact_triple(
            user_id=self.user_id,
            fact=cleaned_value,
            fact_name=self._bounded_name(category),
            source_node_name=self._bounded_name(f"layer:{layer}"),
            target_node_name=self._bounded_name(cleaned_key),
            edge_attributes={
                "provider": "project_sovereign",
                "memory_kind": "fact",
                "layer": layer,
                "category": category,
                "key": cleaned_key,
                "confidence": round(max(0.0, min(confidence, 1.0)), 4),
                "source": source,
                "updated_at": utcnow().isoformat(),
            },
            metadata={
                "provider": "project_sovereign",
                "memory_kind": "fact",
                "layer": layer,
                "category": category,
                "key": cleaned_key,
            },
        )

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]:
        self._ensure_ready()
        edges = self._client.graph.edge.get_by_user_id(self.user_id, limit=256)
        facts = [fact for fact in self._facts_from_edges(edges) if fact.layer == layer]
        if category is not None:
            facts = [fact for fact in facts if fact.category == category]
        facts.sort(key=lambda item: (item.updated_at, item.confidence), reverse=True)
        deduped: list[MemoryFact] = []
        seen: set[tuple[str, str]] = set()
        for fact in facts:
            identity = (fact.category, fact.key)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(fact)
        return deduped

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        cleaned_query = " ".join(query.split())
        if not cleaned_query:
            return []
        self._ensure_ready()
        result = self._client.graph.search(
            user_id=self.user_id,
            query=cleaned_query,
            scope="edges",
            limit=12,
        )
        allowed_layers = set(layers or ("user", "project", "operational"))
        facts = [
            fact
            for fact in self._facts_from_edges(result.edges or [])
            if fact.layer in allowed_layers
        ]
        deduped: list[MemoryFact] = []
        seen: set[tuple[str, str, str]] = set()
        for fact in facts:
            identity = (fact.layer, fact.category, fact.key)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(fact)
            if len(deduped) >= 8:
                break
        return deduped

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None:
        self._ensure_ready()
        cleaned_key = " ".join(key.lower().split())
        edges = self._client.graph.edge.get_by_user_id(self.user_id, limit=256)
        for edge in edges:
            attributes = getattr(edge, "attributes", None) or {}
            if attributes.get("provider") != "project_sovereign":
                continue
            if attributes.get("memory_kind") != "fact":
                continue
            if attributes.get("layer") != layer:
                continue
            if attributes.get("key") != cleaned_key:
                continue
            if category is not None and attributes.get("category") != category:
                continue
            uuid_value = getattr(edge, "uuid_", None) or getattr(edge, "uuid", None)
            if uuid_value:
                self._client.graph.edge.delete(uuid_value)

    def prune_transient_memories(self, *, active_task_ids: set[str]) -> int:
        removed = 0
        for fact in self.list_facts("project"):
            if fact.category == "current_goal" and self._timestamp_age_hours(fact.updated_at) > 24:
                self.delete_fact(layer=fact.layer, key=fact.key, category=fact.category)
                removed += 1
        for fact in self.list_facts("operational"):
            task_id = self._task_id_from_fact_key(fact.key)
            age_hours = self._timestamp_age_hours(fact.updated_at)
            if fact.category == "active_task" and task_id not in active_task_ids:
                self.delete_fact(layer=fact.layer, key=fact.key, category=fact.category)
                removed += 1
                continue
            if fact.category in {"recent_result", "task_context"} and age_hours > 24 and task_id not in active_task_ids:
                self.delete_fact(layer=fact.layer, key=fact.key, category=fact.category)
                removed += 1
        return removed

    def _facts_from_edges(self, edges: list[Any]) -> list[MemoryFact]:
        facts: list[MemoryFact] = []
        for edge in edges:
            attributes = getattr(edge, "attributes", None) or {}
            if attributes.get("provider") != "project_sovereign":
                continue
            if attributes.get("memory_kind") != "fact":
                continue
            layer = str(attributes.get("layer", "")).strip()
            category = str(attributes.get("category", "")).strip() or "context"
            key = str(attributes.get("key", "")).strip().lower()
            value = str(getattr(edge, "fact", "")).strip()
            if not layer or not key or not value:
                continue
            confidence_raw = attributes.get("confidence", 0.5)
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = 0.5
            timestamp = str(attributes.get("updated_at") or getattr(edge, "created_at", "") or utcnow().isoformat())
            facts.append(
                MemoryFact(
                    layer=layer,
                    category=category,
                    key=key,
                    value=value,
                    confidence=confidence,
                    source=str(attributes.get("source", "zep")),
                    created_at=str(getattr(edge, "created_at", None) or timestamp),
                    updated_at=timestamp,
                )
            )
        return facts

    def _build_client(self) -> Any | None:
        if not self.api_key or Zep is None:
            return None
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return Zep(**kwargs)

    def _ensure_ready(self) -> None:
        if not self.available:
            raise RuntimeError("Zep is not configured.")
        with self._lock:
            if self._bootstrapped:
                return
            try:
                self._client.user.get(self.user_id)
            except Exception:
                self._client.user.add(
                    user_id=self.user_id,
                    first_name="Sovereign",
                    last_name="User",
                )
            try:
                self._client.thread.get(self.thread_id, lastn=1)
            except Exception:
                self._client.thread.create(thread_id=self.thread_id, user_id=self.user_id)
            self._bootstrapped = True

    def _bounded_name(self, value: str) -> str:
        cleaned = " ".join(value.split())
        return cleaned[:50] or "memory"

    def _timestamp_age_hours(self, iso_timestamp: str) -> float:
        from datetime import datetime

        try:
            timestamp = datetime.fromisoformat(iso_timestamp)
        except ValueError:
            return 10_000.0
        return max((utcnow() - timestamp).total_seconds() / 3600.0, 0.0)

    def _task_id_from_fact_key(self, key: str) -> str | None:
        import re

        match = re.search(r"task[:\-]([a-z0-9\-]+)", key.lower())
        return match.group(1) if match else None
