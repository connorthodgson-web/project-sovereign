"""Reminder adapter backed by the in-process reminder scheduler service."""

from __future__ import annotations

from datetime import datetime

from integrations.reminders.contracts import ReminderAdapter, ReminderRequest, ReminderResult
from integrations.reminders.service import ReminderSchedulerService, reminder_scheduler_service


class APSchedulerReminderAdapter(ReminderAdapter):
    """Contract adapter that schedules one-time reminders in the live service."""

    def __init__(self, *, service: ReminderSchedulerService | None = None) -> None:
        self.service = service or reminder_scheduler_service

    def schedule(self, request: ReminderRequest) -> ReminderResult:
        deliver_at = _parse_schedule(request.schedule)
        if deliver_at is None:
            return ReminderResult(
                success=False,
                summary="Reminder scheduling could not parse the requested time.",
                blockers=["The reminder schedule must be a valid ISO timestamp."],
            )

        success, summary, record, blockers = self.service.schedule_one_time_reminder(
            summary=request.summary,
            deliver_at=deliver_at,
            channel_id=request.channel or request.metadata.get("channel_id", ""),
            user_id=request.recipient or request.metadata.get("user_id"),
            source=request.metadata.get("source", "reminder_agent"),
            metadata=request.metadata,
        )
        metadata = {"delivery_channel": request.delivery_channel}
        if record is not None:
            metadata.update(
                {
                    "deliver_at": record.deliver_at,
                    "status": record.status,
                    "channel": record.channel,
                }
            )
        return ReminderResult(
            success=success,
            summary=summary,
            reminder_id=record.reminder_id if record else None,
            blockers=blockers,
            metadata=metadata,
        )


def _parse_schedule(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
