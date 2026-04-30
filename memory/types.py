"""Shared memory data contracts used across providers."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.models import ReminderScheduleKind, ReminderStatus, utcnow


class MemoryFact(BaseModel):
    """A reusable normalized fact captured for future context."""

    layer: str
    category: str
    key: str
    value: str
    confidence: float = 0.5
    source: str = "system"
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ConversationTurn(BaseModel):
    """A recent conversation turn kept for continuity."""

    role: str
    content: str
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ActionRecord(BaseModel):
    """An operator action that is useful to summarize later."""

    summary: str
    status: str
    kind: str = "action"
    task_id: str | None = None
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ActiveTaskRecord(BaseModel):
    """Persisted pointer to current work."""

    task_id: str
    goal: str
    status: str
    summary: str | None = None
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class OpenLoopRecord(BaseModel):
    """Persisted follow-up or unresolved thread."""

    key: str
    summary: str
    status: str = "open"
    source: str = "system"
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())


class ReminderRecord(BaseModel):
    """Persisted reminder scheduling and delivery state."""

    reminder_id: str
    summary: str
    deliver_at: str
    channel: str
    recipient: str | None = None
    delivery_channel: str = "slack"
    status: str = ReminderStatus.PENDING.value
    schedule_kind: str = ReminderScheduleKind.ONE_TIME.value
    recurrence_rule: str | None = None
    recurrence_description: str | None = None
    timezone_name: str | None = None
    source: str = "system"
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())
    delivered_at: str | None = None
    last_delivered_at: str | None = None
    failed_at: str | None = None
    canceled_at: str | None = None
    delivery_id: str | None = None
    failure_reason: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemorySnapshot(BaseModel):
    """Serialized memory document persisted to disk."""

    session_turns: list[ConversationTurn] = Field(default_factory=list)
    recent_actions: list[ActionRecord] = Field(default_factory=list)
    active_tasks: list[ActiveTaskRecord] = Field(default_factory=list)
    open_loops: list[OpenLoopRecord] = Field(default_factory=list)
    reminders: list[ReminderRecord] = Field(default_factory=list)
    user_facts: list[MemoryFact] = Field(default_factory=list)
    project_facts: list[MemoryFact] = Field(default_factory=list)
    operational_facts: list[MemoryFact] = Field(default_factory=list)
