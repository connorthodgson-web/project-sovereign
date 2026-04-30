"""Compatibility facade over Memory Platform v2 stores.

Old callers can still use MemoryStore directly. Internally, semantic methods
delegate to SemanticMemoryStore and active-work/reminder methods delegate to
OperationalStateStore. Personal Ops has its own separate store and is exposed
for composition, not mixed into chat/fact memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memory.adapters import ProviderOperationalStateAdapter, ProviderSemanticMemoryAdapter
from memory.contracts import OperationalStateStore, PersonalOpsStore, SemanticMemoryStore
from memory.personal_ops_store import JsonPersonalOpsStore
from memory.provider import MemoryBackend, build_memory_backend
from memory.types import (
    ActionRecord,
    ActiveTaskRecord,
    ConversationTurn,
    MemoryFact,
    MemorySnapshot,
    OpenLoopRecord,
    ReminderRecord,
)


class MemoryStore:
    """Backwards-compatible wrapper that now delegates to a provider backend."""

    def __init__(
        self,
        file_path: str | Path | None = None,
        *,
        provider: MemoryBackend | None = None,
        semantic_store: SemanticMemoryStore | None = None,
        operational_store: OperationalStateStore | None = None,
        personal_ops_store: PersonalOpsStore | None = None,
    ) -> None:
        self._provider = provider or build_memory_backend(file_path=file_path)
        self.semantic: SemanticMemoryStore = semantic_store or ProviderSemanticMemoryAdapter(self._provider)
        self.operational: OperationalStateStore = operational_store or ProviderOperationalStateAdapter(self._provider)
        self.personal_ops: PersonalOpsStore = personal_ops_store or JsonPersonalOpsStore()

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", self._provider.__class__.__name__)

    @property
    def _snapshot(self) -> MemorySnapshot:
        local_provider = getattr(self._provider, "local", self._provider)
        return getattr(local_provider, "_snapshot")

    def snapshot(self) -> MemorySnapshot:
        return self._provider.snapshot()

    # v1 -> v2 semantic mapping: session turns live in SemanticMemoryStore.
    def record_turn(self, role: str, content: str) -> None:
        self.semantic.record_turn(role, content)

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]:
        return self.semantic.list_turns(limit=limit)

    # v1 -> v2 semantic/operational mapping: user/project facts are semantic;
    # layer="operational" facts are routed to OperationalStateStore.
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
        if layer == "operational":
            self.operational.upsert_operational_fact(
                category=category,
                key=key,
                value=value,
                confidence=confidence,
                source=source,
            )
            return
        self.semantic.upsert_fact(
            layer=layer,
            category=category,
            key=key,
            value=value,
            confidence=confidence,
            source=source,
        )

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]:
        if layer == "operational":
            return self.operational.list_operational_facts(category=category)
        return self.semantic.list_facts(layer, category=category)

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        requested_layers = layers or ("user", "project", "operational")
        semantic_layers = tuple(layer for layer in requested_layers if layer != "operational")
        operational_requested = "operational" in requested_layers
        matches: list[MemoryFact] = []
        if semantic_layers:
            matches.extend(self.semantic.search_facts(query, layers=semantic_layers))
        if operational_requested:
            matches.extend(self._provider.search_facts(query, layers=("operational",)))
        return matches[:8]

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None:
        if layer == "operational":
            self.operational.delete_operational_fact(key=key, category=category)
            return
        self.semantic.delete_fact(layer=layer, key=key, category=category)

    # v1 -> v2 operational mapping: actions, tasks, open loops, reminders, and
    # transient pruning all belong to OperationalStateStore.
    def record_action(
        self,
        summary: str,
        *,
        status: str,
        kind: str = "action",
        task_id: str | None = None,
    ) -> None:
        self.operational.record_action(summary, status=status, kind=kind, task_id=task_id)

    def set_active_task(
        self,
        *,
        task_id: str,
        goal: str,
        status: Any,
        summary: str | None = None,
    ) -> None:
        self.operational.set_active_task(task_id=task_id, goal=goal, status=status, summary=summary)

    def remove_active_task(self, task_id: str) -> None:
        self.operational.remove_active_task(task_id)

    def upsert_open_loop(
        self,
        *,
        key: str,
        summary: str,
        status: str = "open",
        source: str = "system",
    ) -> None:
        self.operational.upsert_open_loop(key=key, summary=summary, status=status, source=source)

    def close_open_loop(self, key: str) -> None:
        self.operational.close_open_loop(key)

    def upsert_reminder(self, **kwargs: Any) -> ReminderRecord:
        return self.operational.upsert_reminder(**kwargs)

    def list_reminders(self, **kwargs: Any) -> list[ReminderRecord]:
        return self.operational.list_reminders(**kwargs)

    def get_reminder(self, reminder_id: str) -> ReminderRecord | None:
        return self.operational.get_reminder(reminder_id)

    def mark_reminder_delivered(self, reminder_id: str, **kwargs: Any) -> ReminderRecord | None:
        return self.operational.mark_reminder_delivered(reminder_id, **kwargs)

    def mark_recurring_reminder_delivered(self, reminder_id: str, **kwargs: Any) -> ReminderRecord | None:
        return self.operational.mark_recurring_reminder_delivered(reminder_id, **kwargs)

    def mark_reminder_failed(self, reminder_id: str, **kwargs: Any) -> ReminderRecord | None:
        return self.operational.mark_reminder_failed(reminder_id, **kwargs)

    def cancel_reminder(self, reminder_id: str, **kwargs: Any) -> ReminderRecord | None:
        return self.operational.cancel_reminder(reminder_id, **kwargs)

    def prune_transient_memories(self) -> int:
        return self.operational.prune_transient_memories()

    def reset(self) -> None:
        self._provider.reset()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


memory_store = MemoryStore()

__all__ = [
    "ActionRecord",
    "ActiveTaskRecord",
    "ConversationTurn",
    "MemoryFact",
    "MemorySnapshot",
    "MemoryStore",
    "OpenLoopRecord",
    "OperationalStateStore",
    "PersonalOpsStore",
    "ReminderRecord",
    "SemanticMemoryStore",
    "memory_store",
]
