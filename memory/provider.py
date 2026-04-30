"""Memory provider abstraction and staged backend composition."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import settings
from core.models import ReminderScheduleKind, ReminderStatus, TaskStatus
from memory.local_provider import LocalMemoryProvider
from memory.types import ConversationTurn, MemoryFact, MemorySnapshot, ReminderRecord
from memory.zep_provider import ZepMemoryProvider


@runtime_checkable
class MemoryBackend(Protocol):
    """Small provider surface used by the assistant and memory policy layer."""

    name: str

    def snapshot(self) -> MemorySnapshot: ...

    def record_turn(self, role: str, content: str) -> None: ...

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]: ...

    def record_action(self, summary: str, *, status: str, kind: str = "action", task_id: str | None = None) -> None: ...

    def upsert_fact(
        self,
        *,
        layer: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "system",
    ) -> None: ...

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]: ...

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]: ...

    def set_active_task(
        self,
        *,
        task_id: str,
        goal: str,
        status: TaskStatus | str,
        summary: str | None = None,
    ) -> None: ...

    def remove_active_task(self, task_id: str) -> None: ...

    def upsert_open_loop(self, *, key: str, summary: str, status: str = "open", source: str = "system") -> None: ...

    def close_open_loop(self, key: str) -> None: ...

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
    ) -> ReminderRecord: ...

    def list_reminders(self, *, statuses: tuple[ReminderStatus | str, ...] | None = None) -> list[ReminderRecord]: ...

    def get_reminder(self, reminder_id: str) -> ReminderRecord | None: ...

    def mark_reminder_delivered(self, reminder_id: str, *, delivery_id: str | None = None) -> ReminderRecord | None: ...

    def mark_recurring_reminder_delivered(
        self,
        reminder_id: str,
        *,
        next_deliver_at: str,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None: ...

    def mark_reminder_failed(self, reminder_id: str, *, reason: str) -> ReminderRecord | None: ...

    def cancel_reminder(self, reminder_id: str, *, reason: str | None = None) -> ReminderRecord | None: ...

    def reset(self) -> None: ...

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None: ...

    def prune_transient_memories(self) -> int: ...


class HybridMemoryProvider:
    """Stages Zep in front of the local snapshot without breaking current behavior."""

    name = "hybrid"

    def __init__(
        self,
        *,
        local: LocalMemoryProvider,
        zep: ZepMemoryProvider | None = None,
        backend_mode: str = "hybrid",
    ) -> None:
        self.local = local
        self.zep = zep
        self.backend_mode = backend_mode

    @property
    def _zep_enabled(self) -> bool:
        return self.zep is not None and self.zep.available

    @property
    def _dual_write(self) -> bool:
        return self.backend_mode == "hybrid"

    def snapshot(self) -> MemorySnapshot:
        snapshot = self.local.snapshot()
        if self._zep_enabled:
            try:
                snapshot.session_turns = self.zep.list_turns(limit=self.local.max_turns)
                snapshot.user_facts = self.zep.list_facts("user")
                snapshot.project_facts = self.zep.list_facts("project")
            except Exception:
                pass
        return snapshot

    def record_turn(self, role: str, content: str) -> None:
        if self._zep_enabled:
            try:
                self.zep.record_turn(role, content)
                if self._dual_write:
                    self.local.record_turn(role, content)
                return
            except Exception:
                pass
        self.local.record_turn(role, content)

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]:
        if self._zep_enabled:
            try:
                return self.zep.list_turns(limit=limit)
            except Exception:
                pass
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
        if layer == "operational":
            self.local.upsert_fact(
                layer=layer,
                category=category,
                key=key,
                value=value,
                confidence=confidence,
                source=source,
            )
            return
        if self._zep_enabled:
            try:
                self.zep.upsert_fact(
                    layer=layer,
                    category=category,
                    key=key,
                    value=value,
                    confidence=confidence,
                    source=source,
                )
                if self._dual_write:
                    self.local.upsert_fact(
                        layer=layer,
                        category=category,
                        key=key,
                        value=value,
                        confidence=confidence,
                        source=source,
                    )
                return
            except Exception:
                pass
        self.local.upsert_fact(
            layer=layer,
            category=category,
            key=key,
            value=value,
            confidence=confidence,
            source=source,
        )

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]:
        if layer == "operational":
            return self.local.list_facts(layer, category=category)
        if self._zep_enabled:
            try:
                facts = self.zep.list_facts(layer, category=category)
                if facts or self.backend_mode == "zep":
                    return facts
            except Exception:
                pass
        return self.local.list_facts(layer, category=category)

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        requested_layers = layers or ("user", "project", "operational")
        if set(requested_layers) == {"operational"}:
            return self.local.search_facts(query, layers=("operational",))
        if self._zep_enabled:
            try:
                semantic_layers = tuple(layer for layer in requested_layers if layer != "operational")
                facts = self.zep.search_facts(query, layers=semantic_layers)
                if facts:
                    return facts
                if self.backend_mode == "zep":
                    return []
            except Exception:
                pass
        return self.local.search_facts(query, layers=requested_layers)

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

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None:
        if self._zep_enabled and layer != "operational":
            try:
                self.zep.delete_fact(layer=layer, key=key, category=category)
            except Exception:
                pass
        self.local.delete_fact(layer=layer, key=key, category=category)

    def prune_transient_memories(self) -> int:
        removed = self.local.prune_transient_memories()
        if self._zep_enabled:
            try:
                removed += self.zep.prune_transient_memories(active_task_ids=set())
            except Exception:
                pass
        return removed


def build_memory_backend(file_path: str | Path | None = None) -> MemoryBackend:
    """Construct the staged memory backend selected by current settings."""

    local = LocalMemoryProvider(file_path=file_path)
    provider_mode = (getattr(settings, "memory_provider", "local") or "local").strip().lower()
    if provider_mode == "chroma":
        try:
            from memory.chroma_provider import ChromaMemoryProvider

            chroma = ChromaMemoryProvider(local=local)
            if chroma.available:
                return chroma
        except Exception:
            pass
        return local
    backend_mode = (settings.memory_backend or "hybrid").strip().lower()
    if backend_mode == "local":
        return local

    zep = ZepMemoryProvider()
    if not zep.available:
        return local
    return HybridMemoryProvider(local=local, zep=zep, backend_mode=backend_mode)
