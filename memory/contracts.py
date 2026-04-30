"""Memory Platform v2 contracts.

These protocols separate durable semantic memory, operational run state, and
Personal Ops data without forcing the current JSON providers to change shape.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.models import ReminderScheduleKind, ReminderStatus, TaskStatus
from memory.personal_ops_store import (
    ContactRecord,
    PersonalListRecord,
    PersonalListItem,
    PersonalOpsSnapshot,
    ProactiveRoutineRecord,
)
from memory.types import (
    ActionRecord,
    ActiveTaskRecord,
    ConversationTurn,
    MemoryFact,
    MemorySnapshot,
    OpenLoopRecord,
    ReminderRecord,
)


@runtime_checkable
class SemanticMemoryStore(Protocol):
    """Durable facts, conversation turns, and query-relevant retrieval."""

    name: str

    def record_turn(self, role: str, content: str) -> None: ...

    def list_turns(self, *, limit: int | None = None) -> list[ConversationTurn]: ...

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

    def delete_fact(self, *, layer: str, key: str, category: str | None = None) -> None: ...


@runtime_checkable
class OperationalStateStore(Protocol):
    """Active work, open loops, reminders metadata, recent actions, and run state."""

    name: str

    def snapshot(self) -> MemorySnapshot: ...

    def record_action(
        self,
        summary: str,
        *,
        status: str,
        kind: str = "action",
        task_id: str | None = None,
    ) -> None: ...

    def list_recent_actions(self, *, limit: int | None = None) -> list[ActionRecord]: ...

    def set_active_task(
        self,
        *,
        task_id: str,
        goal: str,
        status: TaskStatus | str,
        summary: str | None = None,
    ) -> None: ...

    def list_active_tasks(self) -> list[ActiveTaskRecord]: ...

    def remove_active_task(self, task_id: str) -> None: ...

    def upsert_open_loop(
        self,
        *,
        key: str,
        summary: str,
        status: str = "open",
        source: str = "system",
    ) -> None: ...

    def list_open_loops(self, *, include_closed: bool = False) -> list[OpenLoopRecord]: ...

    def close_open_loop(self, key: str) -> None: ...

    def upsert_operational_fact(
        self,
        *,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "system",
    ) -> None: ...

    def list_operational_facts(self, *, category: str | None = None) -> list[MemoryFact]: ...

    def delete_operational_fact(self, *, key: str, category: str | None = None) -> None: ...

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

    def mark_reminder_delivered(
        self,
        reminder_id: str,
        *,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None: ...

    def mark_recurring_reminder_delivered(
        self,
        reminder_id: str,
        *,
        next_deliver_at: str,
        delivery_id: str | None = None,
    ) -> ReminderRecord | None: ...

    def mark_reminder_failed(self, reminder_id: str, *, reason: str) -> ReminderRecord | None: ...

    def cancel_reminder(self, reminder_id: str, *, reason: str | None = None) -> ReminderRecord | None: ...

    def prune_transient_memories(self) -> int: ...


@runtime_checkable
class PersonalOpsStore(Protocol):
    """Structured life-assistant data: lists, notes, and routine manifests."""

    name: str

    def snapshot(self) -> PersonalOpsSnapshot: ...

    def list_lists(self) -> list[PersonalListRecord]: ...

    def get_list(self, name_or_id: str) -> PersonalListRecord | None: ...

    def create_list(self, name: str, *, items: list[str] | None = None) -> PersonalListRecord: ...

    def add_items(self, name_or_id: str, items: list[str]) -> PersonalListRecord: ...

    def remove_item(self, name_or_id: str, item_text_or_id: str) -> tuple[PersonalListRecord, PersonalListItem | None]: ...

    def update_item(self, name_or_id: str, old_text: str, new_text: str) -> tuple[PersonalListRecord, bool]: ...

    def rename_list(self, name_or_id: str, new_name: str) -> PersonalListRecord: ...

    def upsert_proactive_routine(
        self,
        *,
        title: str,
        goal: str,
        cadence: str | None = None,
        status: str = "planned",
        execution_live: bool = False,
    ) -> ProactiveRoutineRecord: ...

    def list_proactive_routines(self) -> list[ProactiveRoutineRecord]: ...

    def upsert_contact(
        self,
        *,
        alias: str,
        email: str,
        name: str | None = None,
        source: str = "user_explicit",
    ) -> ContactRecord: ...

    def list_contacts(self) -> list[ContactRecord]: ...

    def find_contacts(self, alias_or_email: str) -> list[ContactRecord]: ...

    def reset(self) -> None: ...
