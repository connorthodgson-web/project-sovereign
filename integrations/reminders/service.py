"""Reminder scheduler service with durable state and outbound delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from threading import Lock
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import Settings, settings
from core.logging import get_logger
from core.models import ReminderScheduleKind, ReminderStatus, utcnow
from integrations.messaging.contracts import MessagingRequest
from integrations.reminders.recurring import RecurringReminderSchedule
from integrations.slack_outbound import SlackOutboundAdapter
from memory.memory_store import MemoryStore, ReminderRecord, memory_store
from memory.provider import MemoryBackend

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as exc:  # pragma: no cover - depends on environment
    BackgroundScheduler = None  # type: ignore[assignment]
    _APSCHEDULER_IMPORT_ERROR: Exception | None = exc
else:
    _APSCHEDULER_IMPORT_ERROR = None


@dataclass(frozen=True)
class ReminderServiceHealth:
    live: bool
    scheduler_started: bool
    blockers: tuple[str, ...] = ()


class ReminderSchedulerService:
    """Lightweight in-process scheduler for one-time and recurring reminders."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        memory_store_instance: MemoryBackend | None = None,
        outbound_adapter: SlackOutboundAdapter | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.memory_store = memory_store_instance or memory_store
        self.outbound_adapter = outbound_adapter or SlackOutboundAdapter(
            runtime_settings=self.settings
        )
        self.logger = get_logger(__name__)
        self._scheduler = None
        self._started = False
        self._lock = Lock()

    def health(self) -> ReminderServiceHealth:
        blockers: list[str] = []
        if not self.settings.reminders_enabled:
            blockers.append("Reminder scheduling is disabled in this runtime.")
        if not self.settings.scheduler_backend:
            blockers.append("SCHEDULER_BACKEND is required for reminder scheduling.")
        elif self.settings.scheduler_backend.lower() != "apscheduler":
            blockers.append(
                f"Unsupported scheduler backend '{self.settings.scheduler_backend}'. Use 'apscheduler'."
            )
        if _APSCHEDULER_IMPORT_ERROR is not None:
            blockers.append("APScheduler is not installed, so reminder scheduling cannot start.")
        blockers.extend(self.outbound_adapter.readiness_blockers())
        return ReminderServiceHealth(
            live=not blockers,
            scheduler_started=self._started,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def start(self) -> bool:
        health = self.health()
        if not health.live:
            return False

        with self._lock:
            if self._started:
                return True
            resolved_timezone = _resolve_timezone(self.settings.scheduler_timezone)
            self._scheduler = BackgroundScheduler(timezone=resolved_timezone)
            self._scheduler.start()
            self._started = True
            self._rehydrate_pending_reminders()
            return True

    def shutdown(self) -> None:
        with self._lock:
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
            self._scheduler = None
            self._started = False

    def schedule_one_time_reminder(
        self,
        *,
        summary: str,
        deliver_at: datetime,
        channel_id: str,
        user_id: str | None = None,
        source: str = "reminder_agent",
        metadata: dict[str, str] | None = None,
    ) -> tuple[bool, str, ReminderRecord | None, list[str]]:
        health = self.health()
        if not health.live:
            return (
                False,
                "Reminder scheduling is not available in this runtime.",
                None,
                list(health.blockers),
            )

        resolved_timezone = _resolve_timezone(self.settings.scheduler_timezone)
        now = datetime.now(resolved_timezone)
        scheduled_for = deliver_at.astimezone(resolved_timezone)
        if scheduled_for <= now:
            return (
                False,
                "Reminder scheduling requires a future delivery time.",
                None,
                ["The requested reminder time is not in the future."],
            )

        self.start()
        reminder_id = str(uuid4())
        record = self.memory_store.upsert_reminder(
            reminder_id=reminder_id,
            summary=summary,
            deliver_at=scheduled_for.isoformat(),
            channel=channel_id,
            recipient=user_id,
            delivery_channel="slack",
            status=ReminderStatus.PENDING,
            schedule_kind=ReminderScheduleKind.ONE_TIME,
            timezone_name=self.settings.scheduler_timezone,
            source=source,
            metadata=metadata or {},
        )
        self.memory_store.upsert_open_loop(
            key=f"reminder:{reminder_id}",
            summary=f"Pending reminder: {summary}",
            status=ReminderStatus.PENDING.value,
            source=source,
        )
        self.memory_store.record_action(
            f"Scheduled reminder for {summary}",
            status=ReminderStatus.PENDING.value,
            kind="reminder",
            task_id=reminder_id,
        )
        self._schedule_job(record)
        return True, "Reminder scheduled successfully.", record, []

    def schedule_recurring_reminder(
        self,
        *,
        summary: str,
        schedule: RecurringReminderSchedule,
        channel_id: str,
        user_id: str | None = None,
        source: str = "reminder_agent",
        metadata: dict[str, str] | None = None,
    ) -> tuple[bool, str, ReminderRecord | None, list[str]]:
        health = self.health()
        if not health.live:
            return (
                False,
                "Recurring reminder scheduling is not available in this runtime.",
                None,
                list(health.blockers),
            )

        try:
            next_run = schedule.next_occurrence()
        except ValueError as exc:
            return (
                False,
                "Recurring reminder scheduling could not compute the next run time.",
                None,
                [str(exc)],
            )

        self.start()
        reminder_id = str(uuid4())
        record = self.memory_store.upsert_reminder(
            reminder_id=reminder_id,
            summary=summary,
            deliver_at=next_run.isoformat(),
            channel=channel_id,
            recipient=user_id,
            delivery_channel="slack",
            status=ReminderStatus.PENDING,
            schedule_kind=ReminderScheduleKind.RECURRING,
            recurrence_rule=schedule.to_rule(),
            recurrence_description=schedule.describe(),
            timezone_name=schedule.timezone_name,
            source=source,
            metadata=metadata or {},
        )
        self.memory_store.upsert_open_loop(
            key=f"reminder:{reminder_id}",
            summary=f"Active reminder: {summary}",
            status=ReminderStatus.PENDING.value,
            source=source,
        )
        self.memory_store.record_action(
            f"Scheduled recurring reminder for {summary}",
            status=ReminderStatus.PENDING.value,
            kind="reminder",
            task_id=reminder_id,
        )
        self._schedule_job(record)
        return True, "Recurring reminder scheduled successfully.", record, []

    def cancel_reminder(self, reminder_id: str, *, reason: str | None = None) -> ReminderRecord | None:
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(reminder_id)
            except Exception:
                pass
        canceled = self.memory_store.cancel_reminder(reminder_id, reason=reason)
        if canceled is not None:
            self.memory_store.close_open_loop(f"reminder:{reminder_id}")
            self.memory_store.record_action(
                f"Canceled reminder for {canceled.summary}",
                status=ReminderStatus.CANCELED.value,
                kind="reminder",
                task_id=reminder_id,
            )
        return canceled

    def cancel_matching_reminder(
        self,
        query: str,
        *,
        recurring_only: bool = False,
    ) -> tuple[ReminderRecord | None, list[ReminderRecord]]:
        matches = self.find_matching_reminders(query, recurring_only=recurring_only)
        if not matches:
            return None, []
        primary = matches[0]
        canceled = self.cancel_reminder(primary.reminder_id, reason="Canceled by user request.")
        return canceled, matches

    def find_matching_reminders(
        self,
        query: str,
        *,
        recurring_only: bool = False,
        statuses: tuple[ReminderStatus | str, ...] = (ReminderStatus.PENDING,),
    ) -> list[ReminderRecord]:
        candidates = self.memory_store.list_reminders(statuses=statuses)
        if recurring_only:
            candidates = [
                item for item in candidates if item.schedule_kind == ReminderScheduleKind.RECURRING.value
            ]
        query_terms = _tokenize(query)
        if not query_terms:
            return candidates[:5]
        ranked = sorted(
            candidates,
            key=lambda item: (
                _match_score(query_terms, item),
                item.updated_at,
            ),
            reverse=True,
        )
        return [item for item in ranked if _match_score(query_terms, item) > 0][:5]

    def list_active_reminders(
        self,
        *,
        recurring_only: bool = False,
    ) -> list[ReminderRecord]:
        reminders = self.memory_store.list_reminders(statuses=(ReminderStatus.PENDING,))
        if recurring_only:
            reminders = [
                item for item in reminders if item.schedule_kind == ReminderScheduleKind.RECURRING.value
            ]
        return reminders

    def reschedule_reminder(
        self,
        reminder_id: str,
        *,
        deliver_at: datetime | None = None,
        recurring_schedule: RecurringReminderSchedule | None = None,
    ) -> ReminderRecord | None:
        existing = self.memory_store.get_reminder(reminder_id)
        if existing is None or existing.status != ReminderStatus.PENDING.value:
            return None
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(reminder_id)
            except Exception:
                pass

        if recurring_schedule is not None:
            next_run = recurring_schedule.next_occurrence()
            updated = self.memory_store.upsert_reminder(
                reminder_id=existing.reminder_id,
                summary=existing.summary,
                deliver_at=next_run.isoformat(),
                channel=existing.channel,
                recipient=existing.recipient,
                delivery_channel=existing.delivery_channel,
                status=ReminderStatus.PENDING,
                schedule_kind=ReminderScheduleKind.RECURRING,
                recurrence_rule=recurring_schedule.to_rule(),
                recurrence_description=recurring_schedule.describe(),
                timezone_name=recurring_schedule.timezone_name,
                source=existing.source,
                metadata=existing.metadata,
            )
        else:
            if deliver_at is None:
                return None
            updated = self.memory_store.upsert_reminder(
                reminder_id=existing.reminder_id,
                summary=existing.summary,
                deliver_at=deliver_at.isoformat(),
                channel=existing.channel,
                recipient=existing.recipient,
                delivery_channel=existing.delivery_channel,
                status=ReminderStatus.PENDING,
                schedule_kind=ReminderScheduleKind.ONE_TIME,
                recurrence_rule=None,
                recurrence_description=None,
                timezone_name=existing.timezone_name or self.settings.scheduler_timezone,
                source=existing.source,
                metadata=existing.metadata,
            )
        self._schedule_job(updated)
        self.memory_store.record_action(
            f"Updated reminder for {existing.summary}",
            status=ReminderStatus.PENDING.value,
            kind="reminder",
            task_id=existing.reminder_id,
        )
        return updated

    def scheduler_started(self) -> bool:
        return self._started

    def _rehydrate_pending_reminders(self) -> None:
        for reminder in self.memory_store.list_reminders(statuses=(ReminderStatus.PENDING,)):
            self._schedule_job(reminder)

    def _schedule_job(self, reminder: ReminderRecord) -> None:
        if self._scheduler is None:
            return
        if reminder.schedule_kind == ReminderScheduleKind.RECURRING.value and reminder.recurrence_rule:
            try:
                schedule = RecurringReminderSchedule.from_rule(
                    reminder.recurrence_rule,
                    timezone_name=reminder.timezone_name or self.settings.scheduler_timezone,
                )
            except (ValueError, TypeError, KeyError):
                self.memory_store.mark_reminder_failed(
                    reminder.reminder_id,
                    reason="Stored recurring reminder rule could not be parsed for scheduling.",
                )
                return
            try:
                next_run = schedule.next_occurrence()
            except ValueError:
                self.memory_store.mark_reminder_failed(
                    reminder.reminder_id,
                    reason="Stored recurring reminder rule could not compute the next run.",
                )
                return
            self.memory_store.upsert_reminder(
                reminder_id=reminder.reminder_id,
                summary=reminder.summary,
                deliver_at=next_run.isoformat(),
                channel=reminder.channel,
                recipient=reminder.recipient,
                delivery_channel=reminder.delivery_channel,
                status=ReminderStatus.PENDING,
                schedule_kind=ReminderScheduleKind.RECURRING,
                recurrence_rule=reminder.recurrence_rule,
                recurrence_description=reminder.recurrence_description,
                timezone_name=schedule.timezone_name,
                source=reminder.source,
                metadata=reminder.metadata,
            )
            trigger_kwargs = schedule.to_trigger_kwargs()
            self._scheduler.add_job(
                self._deliver_reminder_job,
                trigger="cron",
                id=reminder.reminder_id,
                replace_existing=True,
                args=[reminder.reminder_id],
                misfire_grace_time=300,
                **trigger_kwargs,
            )
            return

        run_at = _parse_reminder_datetime(
            reminder.deliver_at,
            timezone_name=self.settings.scheduler_timezone,
        )
        if run_at is None:
            self.memory_store.mark_reminder_failed(
                reminder.reminder_id,
                reason="Stored reminder time could not be parsed for scheduling.",
            )
            return
        resolved_timezone = _resolve_timezone(self.settings.scheduler_timezone)
        if run_at <= datetime.now(resolved_timezone):
            run_at = datetime.now(resolved_timezone)
        self._scheduler.add_job(
            self._deliver_reminder_job,
            trigger="date",
            run_date=run_at,
            id=reminder.reminder_id,
            replace_existing=True,
            args=[reminder.reminder_id],
            misfire_grace_time=300,
        )

    def _deliver_reminder_job(self, reminder_id: str) -> None:
        reminder = self.memory_store.get_reminder(reminder_id)
        if reminder is None or reminder.status != ReminderStatus.PENDING.value:
            return

        message = reminder.metadata.get("delivery_text") or f"Reminder: {reminder.summary}"
        result = self.outbound_adapter.send(
            MessagingRequest(
                channel="slack",
                recipient=reminder.channel,
                message=message,
                metadata={
                    "channel_id": reminder.channel,
                    "user_id": reminder.recipient or "",
                    "reminder_id": reminder.reminder_id,
                },
            )
        )
        if result.success:
            if reminder.schedule_kind == ReminderScheduleKind.RECURRING.value and reminder.recurrence_rule:
                schedule = RecurringReminderSchedule.from_rule(
                    reminder.recurrence_rule,
                    timezone_name=reminder.timezone_name or self.settings.scheduler_timezone,
                )
                next_run = schedule.next_occurrence(
                    after=datetime.now(_resolve_timezone(schedule.timezone_name)) + timedelta(seconds=1)
                )
                self.memory_store.mark_recurring_reminder_delivered(
                    reminder.reminder_id,
                    next_deliver_at=next_run.isoformat(),
                    delivery_id=result.delivery_id,
                )
                self.memory_store.upsert_open_loop(
                    key=f"reminder:{reminder.reminder_id}",
                    summary=f"Active reminder: {reminder.summary}",
                    status=ReminderStatus.PENDING.value,
                    source="reminder_delivery",
                )
                self.memory_store.record_action(
                    f"Delivered recurring reminder: {reminder.summary}",
                    status=ReminderStatus.PENDING.value,
                    kind="reminder",
                    task_id=reminder.reminder_id,
                )
                return
            self.memory_store.mark_reminder_delivered(
                reminder.reminder_id,
                delivery_id=result.delivery_id,
            )
            self.memory_store.close_open_loop(f"reminder:{reminder.reminder_id}")
            self.memory_store.record_action(
                f"Delivered reminder: {reminder.summary}",
                status=ReminderStatus.DELIVERED.value,
                kind="reminder",
                task_id=reminder.reminder_id,
            )
            return

        reason = result.blockers[0] if result.blockers else result.summary
        self.memory_store.mark_reminder_failed(reminder.reminder_id, reason=reason)
        self.memory_store.upsert_open_loop(
            key=f"reminder:{reminder.reminder_id}",
            summary=f"Failed reminder: {reminder.summary}",
            status=ReminderStatus.FAILED.value,
            source="reminder_delivery",
        )
        self.memory_store.record_action(
            f"Reminder delivery failed: {reminder.summary}",
            status=ReminderStatus.FAILED.value,
            kind="reminder",
            task_id=reminder.reminder_id,
        )


def _parse_reminder_datetime(value: str, *, timezone_name: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_resolve_timezone(timezone_name))
    return parsed


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        local_tz = datetime.now().astimezone().tzinfo
        return local_tz or timezone.utc


reminder_scheduler_service = ReminderSchedulerService()


def _tokenize(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2]


def _match_score(query_terms: list[str], reminder: ReminderRecord) -> int:
    haystack = " ".join(
        [
            reminder.summary,
            reminder.recurrence_description or "",
            reminder.metadata.get("schedule_phrase", ""),
        ]
    ).lower()
    return sum(1 for term in query_terms if term in haystack)
