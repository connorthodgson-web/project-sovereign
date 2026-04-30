"""Recurring reminder parsing and schedule helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from integrations.reminders.parsing import normalize_reminder_summary_text

_RECURRING_PREFIX = re.compile(
    r"^(?:please\s+)?(?:remind me|set a reminder|schedule a reminder)\s+",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(
    r"\bat\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.IGNORECASE,
)
_TARGET_SPLIT = re.compile(r"\s+(?:to|that|about)\s+", re.IGNORECASE)
_WEEKDAY_ALIASES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}
@dataclass(frozen=True)
class RecurringReminderSchedule:
    frequency: str
    hour: int | None
    minute: int | None
    timezone_name: str
    day_of_week: str | None = None
    day_of_month: int | None = None
    part_of_day: str | None = None

    def requires_time(self) -> bool:
        return self.hour is None or self.minute is None

    def describe(self) -> str:
        if self.frequency == "daily":
            prefix = "every day"
        elif self.frequency == "weekdays":
            prefix = "every weekday"
        elif self.frequency == "weekly" and self.day_of_week:
            weekday = next(
                (name for name, alias in _WEEKDAY_ALIASES.items() if alias == self.day_of_week),
                self.day_of_week,
            )
            prefix = f"every {weekday.capitalize()}"
        elif self.frequency == "weekly":
            prefix = "every week"
        else:
            prefix = "every month"

        if self.part_of_day and self.requires_time():
            return f"{prefix} in the {self.part_of_day}"
        if self.frequency == "monthly" and self.day_of_month is not None:
            prefix = f"every month on day {self.day_of_month}"
        if self.requires_time():
            return prefix
        return f"{prefix} at {self.formatted_time()}"

    def formatted_time(self) -> str:
        if self.hour is None or self.minute is None:
            return "an unknown time"
        local = datetime.now(_resolve_timezone(self.timezone_name)).replace(
            hour=self.hour,
            minute=self.minute,
            second=0,
            microsecond=0,
        )
        return local.strftime("%I:%M %p").lstrip("0")

    def to_trigger_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timezone": _resolve_timezone(self.timezone_name),
            "minute": self.minute if self.minute is not None else 0,
        }
        if self.frequency == "daily":
            kwargs["hour"] = self.hour if self.hour is not None else 9
        elif self.frequency == "weekdays":
            kwargs["day_of_week"] = "mon-fri"
            kwargs["hour"] = self.hour if self.hour is not None else 9
        elif self.frequency == "weekly":
            kwargs["day_of_week"] = self.day_of_week or "mon"
            kwargs["hour"] = self.hour if self.hour is not None else 9
        elif self.frequency == "monthly":
            kwargs["day"] = self.day_of_month or 1
            kwargs["hour"] = self.hour if self.hour is not None else 9
        else:
            kwargs["hour"] = self.hour if self.hour is not None else 9
        return kwargs

    def build_trigger(self) -> CronTrigger:
        return CronTrigger(**self.to_trigger_kwargs())

    def next_occurrence(self, *, after: datetime | None = None) -> datetime:
        baseline = after or datetime.now(_resolve_timezone(self.timezone_name))
        next_time = self.build_trigger().get_next_fire_time(None, baseline)
        if next_time is None:
            raise ValueError("Could not compute the next recurring reminder occurrence.")
        return next_time

    def with_time(self, hour: int, minute: int) -> "RecurringReminderSchedule":
        return RecurringReminderSchedule(
            frequency=self.frequency,
            hour=hour,
            minute=minute,
            timezone_name=self.timezone_name,
            day_of_week=self.day_of_week,
            day_of_month=self.day_of_month,
            part_of_day=self.part_of_day,
        )

    def to_rule(self) -> str:
        return json.dumps(
            {
                "frequency": self.frequency,
                "hour": self.hour,
                "minute": self.minute,
                "timezone_name": self.timezone_name,
                "day_of_week": self.day_of_week,
                "day_of_month": self.day_of_month,
                "part_of_day": self.part_of_day,
            },
            ensure_ascii=True,
            sort_keys=True,
        )

    @classmethod
    def from_rule(cls, rule: str, *, timezone_name: str | None = None) -> "RecurringReminderSchedule":
        payload = json.loads(rule)
        return cls(
            frequency=str(payload["frequency"]),
            hour=payload.get("hour"),
            minute=payload.get("minute"),
            timezone_name=str(payload.get("timezone_name") or timezone_name or "America/New_York"),
            day_of_week=payload.get("day_of_week"),
            day_of_month=payload.get("day_of_month"),
            part_of_day=payload.get("part_of_day"),
        )


@dataclass(frozen=True)
class RecurringReminderParseOutcome:
    summary: str | None
    schedule: RecurringReminderSchedule | None
    follow_up_question: str | None = None
    failure_reason: str | None = None


def parse_recurring_reminder_request(
    message: str,
    *,
    timezone_name: str = "America/New_York",
) -> RecurringReminderParseOutcome | None:
    normalized = " ".join(message.strip().split())
    if not normalized:
        return None

    lowered = normalized.lower()
    if not _looks_recurring(lowered):
        return None

    body = _RECURRING_PREFIX.sub("", normalized, count=1).strip()
    schedule = _parse_schedule(body, timezone_name=timezone_name)
    if schedule is None:
        return RecurringReminderParseOutcome(
            summary=None,
            schedule=None,
            failure_reason="I couldn't tell what recurring schedule you wanted.",
        )

    summary = _parse_summary(body)
    if not summary:
        return RecurringReminderParseOutcome(
            summary=None,
            schedule=schedule,
            failure_reason="I couldn't tell what you wanted the recurring reminder to say.",
        )

    if schedule.requires_time():
        if schedule.part_of_day == "morning":
            question = "Sure — what time in the morning?"
        elif schedule.part_of_day == "night":
            question = "Sure — what time at night?"
        elif schedule.part_of_day:
            question = f"Sure — what time in the {schedule.part_of_day}?"
        elif schedule.frequency == "monthly":
            question = "Sure — what day of the month and what time should I use?"
        else:
            question = "Sure — what time should I use?"
        return RecurringReminderParseOutcome(
            summary=summary,
            schedule=schedule,
            follow_up_question=question,
        )

    return RecurringReminderParseOutcome(summary=summary, schedule=schedule)


def _looks_recurring(message: str) -> bool:
    recurring_markers = (
        " every ",
        " every day",
        " every weekday",
        " every week",
        " every month",
        " every monday",
        " every tuesday",
        " every wednesday",
        " every thursday",
        " every friday",
        " every saturday",
        " every sunday",
        " each day",
        " daily",
        " weekly",
        " monthly",
        " every morning",
        " every night",
    )
    padded = f" {message} "
    return any(marker in padded for marker in recurring_markers)


def _parse_schedule(body: str, *, timezone_name: str) -> RecurringReminderSchedule | None:
    lowered = body.lower()
    time_match = _TIME_PATTERN.search(lowered)
    hour: int | None = None
    minute: int | None = None
    if time_match:
        parsed_time = _parse_clock_time(
            time_match.group("time"),
            timezone_name=timezone_name,
        )
        if parsed_time is None:
            return None
        hour, minute = parsed_time
    if "every weekday" in lowered:
        return RecurringReminderSchedule(
            frequency="weekdays",
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
        )
    if "every morning" in lowered:
        return RecurringReminderSchedule(
            frequency="daily",
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
            part_of_day="morning",
        )
    if "every night" in lowered:
        return RecurringReminderSchedule(
            frequency="daily",
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
            part_of_day="night",
        )
    for weekday, alias in _WEEKDAY_ALIASES.items():
        if f"every {weekday}" in lowered:
            return RecurringReminderSchedule(
                frequency="weekly",
                hour=hour,
                minute=minute,
                timezone_name=timezone_name,
                day_of_week=alias,
            )
    if "every week" in lowered or "weekly" in lowered:
        weekday = datetime.now(_resolve_timezone(timezone_name)).strftime("%a").lower()
        return RecurringReminderSchedule(
            frequency="weekly",
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
            day_of_week=weekday,
        )
    if "every month" in lowered or "monthly" in lowered:
        day_match = re.search(r"\b(?:on\s+day\s+|on\s+the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)?\b", lowered)
        return RecurringReminderSchedule(
            frequency="monthly",
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
            day_of_month=int(day_match.group("day")) if day_match else None,
        )
    if "every day" in lowered or "daily" in lowered or "each day" in lowered:
        return RecurringReminderSchedule(
            frequency="daily",
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
        )
    return None


def _parse_summary(body: str) -> str | None:
    summary_body = _TIME_PATTERN.sub("", body)
    summary_body = re.sub(
        r"\b(every day|each day|daily|every weekday|every week|weekly|every month|monthly|every morning|every night)\b",
        "",
        summary_body,
        flags=re.IGNORECASE,
    )
    for weekday in _WEEKDAY_ALIASES:
        summary_body = re.sub(
            rf"\bevery {weekday}\b",
            "",
            summary_body,
            flags=re.IGNORECASE,
        )
    if _TARGET_SPLIT.search(summary_body):
        summary = _TARGET_SPLIT.split(summary_body, maxsplit=1)[-1]
    else:
        summary = summary_body
    return normalize_reminder_summary_text(summary.strip(" ,.")) or None


def _parse_clock_time(value: str, *, timezone_name: str) -> tuple[int, int] | None:
    cleaned = " ".join(value.strip().lower().split())
    formats = ["%I %p", "%I:%M %p", "%H:%M", "%H"]
    resolved = _resolve_timezone(timezone_name)
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        hour = parsed.hour
        minute = parsed.minute
        if fmt == "%H" and cleaned.isdigit():
            raw_hour = int(cleaned)
            if 1 <= raw_hour <= 7:
                hour = raw_hour + 12
                minute = 0
        _ = resolved
        return hour, minute
    return None


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        local_tz = datetime.now().astimezone().tzinfo
        return local_tz or timezone.utc
