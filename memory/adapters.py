"""Adapters that expose existing providers through Memory Platform v2 contracts."""

from __future__ import annotations

from core.models import ReminderScheduleKind, ReminderStatus, TaskStatus
from memory.provider import MemoryBackend
from memory.types import ActionRecord, ActiveTaskRecord, MemoryFact, MemorySnapshot, OpenLoopRecord, ReminderRecord


class ProviderSemanticMemoryAdapter:
    """Semantic v2 adapter over the existing local/hybrid/Zep-backed provider."""

    name = "provider_semantic"

    def __init__(self, provider: MemoryBackend) -> None:
        self.provider = provider

    def record_turn(self, role: str, content: str) -> None:
        self.provider.record_turn(role, content)

    def list_turns(self, *, limit: int | None = None):
        return self.provider.list_turns(limit=limit)

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
            raise ValueError("Operational facts belong in OperationalStateStore.")
        self.provider.upsert_fact(
            layer=layer,
            category=category,
            key=key,
            value=value,
            confidence=confidence,
            source=source,
        )

    def list_facts(self, layer: str, *, category: str | None = None) -> list[MemoryFact]:
        if layer == "operational":
            return []
        return self.provider.list_facts(layer, category=category)

    def search_facts(self, query: str, *, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        semantic_layers = tuple(layer for layer in (layers or ("user", "project")) if layer != "operational")
        if not semantic_layers:
            return []
        return self.provider.search_facts(query, layers=semantic_layers)

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None:
        if layer == "operational":
            return
        self.provider.delete_fact(layer=layer, key=key, category=category)


class ProviderOperationalStateAdapter:
    """Operational v2 adapter over the existing JSON operational state."""

    name = "provider_operational"

    def __init__(self, provider: MemoryBackend) -> None:
        self.provider = provider

    def snapshot(self) -> MemorySnapshot:
        return self.provider.snapshot()

    def record_action(
        self,
        summary: str,
        *,
        status: str,
        kind: str = "action",
        task_id: str | None = None,
    ) -> None:
        self.provider.record_action(summary, status=status, kind=kind, task_id=task_id)

    def list_recent_actions(self, *, limit: int | None = None) -> list[ActionRecord]:
        actions = self.provider.snapshot().recent_actions
        return actions[-limit:] if limit is not None else actions

    def set_active_task(
        self,
        *,
        task_id: str,
        goal: str,
        status: TaskStatus | str,
        summary: str | None = None,
    ) -> None:
        self.provider.set_active_task(task_id=task_id, goal=goal, status=status, summary=summary)

    def list_active_tasks(self) -> list[ActiveTaskRecord]:
        return self.provider.snapshot().active_tasks

    def remove_active_task(self, task_id: str) -> None:
        self.provider.remove_active_task(task_id)

    def upsert_open_loop(
        self,
        *,
        key: str,
        summary: str,
        status: str = "open",
        source: str = "system",
    ) -> None:
        self.provider.upsert_open_loop(key=key, summary=summary, status=status, source=source)

    def list_open_loops(self, *, include_closed: bool = False) -> list[OpenLoopRecord]:
        loops = self.provider.snapshot().open_loops
        if include_closed:
            return loops
        return [item for item in loops if item.status != "closed"]

    def close_open_loop(self, key: str) -> None:
        self.provider.close_open_loop(key)

    def upsert_operational_fact(
        self,
        *,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "system",
    ) -> None:
        self.provider.upsert_fact(
            layer="operational",
            category=category,
            key=key,
            value=value,
            confidence=confidence,
            source=source,
        )

    def list_operational_facts(self, *, category: str | None = None) -> list[MemoryFact]:
        return self.provider.list_facts("operational", category=category)

    def delete_operational_fact(self, *, key: str, category: str | None = None) -> None:
        self.provider.delete_fact(layer="operational", key=key, category=category)

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
        return self.provider.upsert_reminder(
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
        return self.provider.list_reminders(statuses=statuses)

    def get_reminder(self, reminder_id: str) -> ReminderRecord | None:
        return self.provider.get_reminder(reminder_id)

    def mark_reminder_delivered(
        self,
        reminder_id: str,
        *,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None:
        return self.provider.mark_reminder_delivered(reminder_id, delivery_id=delivery_id)

    def mark_recurring_reminder_delivered(
        self,
        reminder_id: str,
        *,
        next_deliver_at: str,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None:
        return self.provider.mark_recurring_reminder_delivered(
            reminder_id,
            next_deliver_at=next_deliver_at,
            delivery_id=delivery_id,
        )

    def mark_reminder_failed(self, reminder_id: str, *, reason: str) -> ReminderRecord | None:
        return self.provider.mark_reminder_failed(reminder_id, reason=reason)

    def cancel_reminder(self, reminder_id: str, *, reason: str | None = None) -> ReminderRecord | None:
        return self.provider.cancel_reminder(reminder_id, reason=reason)

    def prune_transient_memories(self) -> int:
        return self.provider.prune_transient_memories()
