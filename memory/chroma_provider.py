"""Optional local Chroma-backed semantic memory provider."""

from __future__ import annotations

import re
from pathlib import Path
from threading import Lock
from typing import Any

from app.config import settings
from core.models import ReminderScheduleKind, ReminderStatus, TaskStatus, utcnow
from memory.local_provider import LocalMemoryProvider
from memory.safety import looks_secret_like
from memory.types import ConversationTurn, MemoryFact, MemorySnapshot, ReminderRecord

try:
    import chromadb
except ImportError:  # pragma: no cover - optional runtime dependency
    chromadb = None  # type: ignore[assignment]


class ChromaMemoryProvider:
    """Adds local vector search for durable facts while preserving JSON memory."""

    name = "chroma"
    max_results = 8

    def __init__(
        self,
        *,
        local: LocalMemoryProvider,
        path: str | Path | None = None,
        collection_name: str | None = None,
        client: Any | None = None,
        max_distance: float | None = None,
    ) -> None:
        self.local = local
        default_path = Path(settings.workspace_root) / ".sovereign" / "chroma_memory"
        self.path = Path(path) if path else Path(getattr(settings, "chroma_path", "") or default_path)
        self.collection_name = collection_name or getattr(settings, "chroma_collection_name", "sovereign_memory")
        self.max_distance = (
            max_distance
            if max_distance is not None
            else float(getattr(settings, "chroma_max_distance", 1.35))
        )
        self._lock = Lock()
        self._client = client or self._build_client()
        self._collection = self._build_collection()
        self._indexed_local_ids: set[str] = set()

    @property
    def available(self) -> bool:
        return self._collection is not None

    def snapshot(self) -> MemorySnapshot:
        return self.local.snapshot()

    def record_turn(self, role: str, content: str) -> None:
        self.local.record_turn(role, content)

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]:
        return self.local.list_turns(limit=limit)

    def record_action(self, summary: str, *, status: str, kind: str = "action", task_id: str | None = None) -> None:
        self.local.record_action(summary, status=status, kind=kind, task_id=task_id)

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
        if looks_secret_like(f"{cleaned_key} {cleaned_value}"):
            self.local.record_action(
                "Skipped storing a secret-like memory fact.",
                status="skipped",
                kind="memory_safety",
            )
            return

        self.local.upsert_fact(
            layer=layer,
            category=category,
            key=cleaned_key,
            value=cleaned_value,
            confidence=confidence,
            source=source,
        )
        if layer == "operational" or not self.available:
            return
        self._upsert_vector_fact(
            MemoryFact(
                layer=layer,
                category=category,
                key=cleaned_key,
                value=cleaned_value,
                confidence=max(0.0, min(confidence, 1.0)),
                source=source,
            )
        )

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]:
        return self.local.list_facts(layer, category=category)

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        cleaned_query = " ".join(query.split())
        if not cleaned_query:
            return []

        requested_layers = layers or ("user", "project", "operational")
        semantic_layers = tuple(layer for layer in requested_layers if layer != "operational")
        operational_requested = "operational" in requested_layers

        matches: list[MemoryFact] = []
        if semantic_layers and self.available:
            self._index_local_semantic_facts(semantic_layers)
            matches.extend(self._search_vector_facts(cleaned_query, semantic_layers))
        if not matches and semantic_layers:
            matches.extend(self.local.search_facts(cleaned_query, layers=semantic_layers))
        if operational_requested:
            matches.extend(self.local.search_facts(cleaned_query, layers=("operational",)))
        return self._dedupe_facts(matches, limit=self.max_results)

    def set_active_task(
        self,
        *,
        task_id: str,
        goal: str,
        status: TaskStatus | str,
        summary: str | None = None,
    ) -> None:
        self.local.set_active_task(task_id=task_id, goal=goal, status=status, summary=summary)

    def remove_active_task(self, task_id: str) -> None:
        self.local.remove_active_task(task_id)

    def upsert_open_loop(self, *, key: str, summary: str, status: str = "open", source: str = "system") -> None:
        self.local.upsert_open_loop(key=key, summary=summary, status=status, source=source)

    def close_open_loop(self, key: str) -> None:
        self.local.close_open_loop(key)

    def upsert_reminder(
        self,
        *,
        reminder_id: str,
        summary: str,
        deliver_at: str,
        channel: str,
        recipient: str | None = None,
        delivery_channel: str = "slack",
        status: ReminderStatus | str = ReminderStatus.PENDING,
        schedule_kind: ReminderScheduleKind | str = ReminderScheduleKind.ONE_TIME,
        recurrence_rule: str | None = None,
        recurrence_description: str | None = None,
        timezone_name: str | None = None,
        source: str = "system",
        metadata: dict[str, str] | None = None,
    ) -> ReminderRecord:
        return self.local.upsert_reminder(
            reminder_id=reminder_id,
            summary=summary,
            deliver_at=deliver_at,
            channel=channel,
            recipient=recipient,
            delivery_channel=delivery_channel,
            status=status,
            schedule_kind=schedule_kind,
            recurrence_rule=recurrence_rule,
            recurrence_description=recurrence_description,
            timezone_name=timezone_name,
            source=source,
            metadata=metadata,
        )

    def list_reminders(self, *, statuses: tuple[ReminderStatus | str, ...] | None = None) -> list[ReminderRecord]:
        return self.local.list_reminders(statuses=statuses)

    def get_reminder(self, reminder_id: str) -> ReminderRecord | None:
        return self.local.get_reminder(reminder_id)

    def mark_reminder_delivered(self, reminder_id: str, *, delivery_id: str | None = None) -> ReminderRecord | None:
        return self.local.mark_reminder_delivered(reminder_id, delivery_id=delivery_id)

    def mark_recurring_reminder_delivered(
        self,
        reminder_id: str,
        *,
        next_deliver_at: str,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None:
        return self.local.mark_recurring_reminder_delivered(
            reminder_id,
            next_deliver_at=next_deliver_at,
            delivery_id=delivery_id,
        )

    def mark_reminder_failed(self, reminder_id: str, *, reason: str) -> ReminderRecord | None:
        return self.local.mark_reminder_failed(reminder_id, reason=reason)

    def cancel_reminder(self, reminder_id: str, *, reason: str | None = None) -> ReminderRecord | None:
        return self.local.cancel_reminder(reminder_id, reason=reason)

    def reset(self) -> None:
        self.local.reset()
        self._indexed_local_ids.clear()
        if self.available:
            try:
                existing = self._collection.get(include=[])
                ids = existing.get("ids", [])
                if ids:
                    self._collection.delete(ids=ids)
            except Exception:
                pass

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None:
        cleaned_key = " ".join(key.lower().split())
        self.local.delete_fact(layer=layer, key=cleaned_key, category=category)
        if layer == "operational" or not self.available:
            return
        ids = [self._fact_id(layer=layer, category=category, key=cleaned_key)] if category else []
        if not ids:
            ids = self._matching_vector_ids(layer=layer, key=cleaned_key)
        if ids:
            try:
                self._collection.delete(ids=ids)
            except Exception:
                pass

    def prune_transient_memories(self) -> int:
        return self.local.prune_transient_memories()

    def _build_client(self) -> Any | None:
        if chromadb is None:
            return None
        self.path.mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=str(self.path))

    def _build_collection(self) -> Any | None:
        if self._client is None:
            return None
        try:
            return self._client.get_or_create_collection(name=self.collection_name)
        except Exception:
            return None

    def _upsert_vector_fact(self, fact: MemoryFact) -> None:
        if not self.available or looks_secret_like(f"{fact.key} {fact.value}"):
            return
        metadata = {
            "provider": "project_sovereign",
            "memory_kind": "fact",
            "layer": fact.layer,
            "category": fact.category,
            "key": fact.key,
            "confidence": float(max(0.0, min(fact.confidence, 1.0))),
            "source": fact.source,
            "created_at": fact.created_at,
            "updated_at": fact.updated_at,
        }
        document = self._document_text(fact)
        try:
            self._collection.upsert(
                ids=[self._fact_id(layer=fact.layer, category=fact.category, key=fact.key)],
                documents=[document],
                metadatas=[metadata],
            )
            self._indexed_local_ids.add(self._fact_id(layer=fact.layer, category=fact.category, key=fact.key))
        except Exception:
            pass

    def _search_vector_facts(self, query: str, layers: tuple[str, ...]) -> list[MemoryFact]:
        if not self.available:
            return []
        allowed_layers = set(layers)
        try:
            response = self._collection.query(
                query_texts=[query],
                n_results=24,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            return []

        ids = self._first_result_list(response, "ids")
        documents = self._first_result_list(response, "documents")
        metadatas = self._first_result_list(response, "metadatas")
        distances = self._first_result_list(response, "distances")
        facts: list[MemoryFact] = []
        for index, metadata in enumerate(metadatas):
            if not isinstance(metadata, dict):
                continue
            if metadata.get("provider") != "project_sovereign" or metadata.get("memory_kind") != "fact":
                continue
            layer = str(metadata.get("layer", "")).strip()
            if layer not in allowed_layers:
                continue
            distance = self._distance_at(distances, index)
            if distance is not None and distance > self.max_distance:
                continue
            value = self._value_from_document(str(documents[index] if index < len(documents) else ""))
            if not value or looks_secret_like(value):
                continue
            facts.append(
                MemoryFact(
                    layer=layer,
                    category=str(metadata.get("category") or "context"),
                    key=str(metadata.get("key") or (ids[index] if index < len(ids) else "")).lower(),
                    value=value,
                    confidence=self._float_metadata(metadata.get("confidence"), default=0.5),
                    source=str(metadata.get("source") or "chroma"),
                    created_at=str(metadata.get("created_at") or utcnow().isoformat()),
                    updated_at=str(metadata.get("updated_at") or utcnow().isoformat()),
                )
            )
        return facts

    def _index_local_semantic_facts(self, layers: tuple[str, ...]) -> None:
        if not self.available:
            return
        with self._lock:
            for layer in layers:
                for fact in self.local.list_facts(layer):
                    if looks_secret_like(f"{fact.key} {fact.value}"):
                        continue
                    fact_id = self._fact_id(layer=fact.layer, category=fact.category, key=fact.key)
                    if fact_id in self._indexed_local_ids:
                        continue
                    self._upsert_vector_fact(fact)

    def _matching_vector_ids(self, *, layer: str, key: str) -> list[str]:
        if not self.available:
            return []
        try:
            response = self._collection.get(where={"layer": layer}, include=["metadatas"])
        except Exception:
            return []
        ids = response.get("ids", [])
        metadatas = response.get("metadatas", [])
        return [
            item_id
            for item_id, metadata in zip(ids, metadatas, strict=False)
            if isinstance(metadata, dict) and metadata.get("key") == key
        ]

    def _document_text(self, fact: MemoryFact) -> str:
        return f"{fact.value}\ncategory: {fact.category}\nkey: {fact.key}\nlayer: {fact.layer}"

    def _value_from_document(self, document: str) -> str:
        return document.split("\ncategory:", 1)[0].strip()

    def _fact_id(self, *, layer: str, category: str | None, key: str) -> str:
        raw = f"{layer}:{category or '*'}:{key}"
        return re.sub(r"[^a-zA-Z0-9_.:-]+", "_", raw)[:240]

    def _first_result_list(self, response: dict[str, Any], key: str) -> list[Any]:
        value = response.get(key, [])
        if value and isinstance(value[0], list):
            return value[0]
        return value

    def _distance_at(self, distances: list[Any], index: int) -> float | None:
        if index >= len(distances):
            return None
        try:
            return float(distances[index])
        except (TypeError, ValueError):
            return None

    def _float_metadata(self, value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _dedupe_facts(self, facts: list[MemoryFact], *, limit: int) -> list[MemoryFact]:
        deduped: list[MemoryFact] = []
        seen: set[tuple[str, str, str]] = set()
        for fact in facts:
            identity = (fact.layer, fact.category, self._canonical_fact_value(fact.value))
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(fact)
            if len(deduped) >= limit:
                break
        return deduped

    def _canonical_fact_value(self, value: str) -> str:
        lowered = " ".join(value.lower().split()).strip(" .")
        lowered = re.sub(r"^(you told me:|you prefer|your name is|the current project priority is)\s+", "", lowered)
        lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
        return " ".join(lowered.split())
