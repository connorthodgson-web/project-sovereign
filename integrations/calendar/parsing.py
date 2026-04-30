"""Natural calendar query and event parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CalendarQuery:
    day: datetime
    label: str
    end_day: datetime | None = None
    mode: str = "range"
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True)
class CalendarEventDraft:
    title: str
    start: datetime
    end: datetime
    attendees: tuple[str, ...] = ()
    location: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class CalendarEventUpdateDraft:
    event_id: str
    updates: dict[str, Any]
    description: str


def parse_calendar_query(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
) -> CalendarQuery | None:
    normalized = " ".join(message.lower().split())
    if not normalized:
        return None
    current = (now or datetime.now(_resolve_timezone(timezone_name))).astimezone(
        _resolve_timezone(timezone_name)
    )
    availability = _parse_availability_query(normalized, current=current, timezone_name=timezone_name)
    if availability is not None:
        return availability
    if any(phrase in normalized for phrase in ("what do i have today", "what's on my calendar today", "check my calendar", "what's on my calendar")) and "tomorrow" not in normalized and "week" not in normalized:
        return CalendarQuery(day=current, label="today")
    if any(phrase in normalized for phrase in ("what do i have tomorrow", "what's on my calendar tomorrow")):
        return CalendarQuery(day=current + timedelta(days=1), label="tomorrow")
    if any(
        phrase in normalized
        for phrase in (
            "what do i have this week",
            "what events do i have this week",
            "what events are on my calendar this week",
            "what's on my calendar this week",
            "what is on my calendar this week",
            "what events are this week",
        )
    ):
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return CalendarQuery(day=start, end_day=start + timedelta(days=7), label="this week")
    if "next event" in normalized or "next thing on my calendar" in normalized:
        return CalendarQuery(day=current, end_day=current + timedelta(days=30), label="next event", mode="next")
    weekday = _extract_day_token(normalized)
    if weekday is not None and normalized.startswith(("what do i have", "what's on my calendar", "what is on my calendar")):
        target = _resolve_day_token(weekday, current=current, timezone_name=timezone_name)
        return CalendarQuery(day=target, label=weekday.replace("this ", "").replace("next ", ""))
    if weekday is not None and normalized.startswith(("what about", "how about")):
        target = _resolve_day_token(weekday, current=current, timezone_name=timezone_name)
        return CalendarQuery(day=target, label=weekday.replace("this ", "").replace("next ", ""))
    return None


def parse_calendar_event_request(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
) -> CalendarEventDraft | None:
    normalized = " ".join(message.strip().split())
    lowered = normalized.lower()
    if not any(lowered.startswith(prefix) for prefix in ("add ", "create ", "schedule ", "put ")):
        return None

    action_match = re.match(r"^(?:add|create|schedule|put)\s+(?P<body>.+)$", normalized, re.IGNORECASE)
    if not action_match:
        return None

    current = (now or datetime.now(_resolve_timezone(timezone_name))).astimezone(
        _resolve_timezone(timezone_name)
    )
    body = action_match.group("body").strip()
    explicit_title: str | None = None
    title_match = re.search(r"\s+(?:called|named|titled)\s+(?P<title>.+)$", body, re.IGNORECASE)
    if title_match:
        explicit_title = title_match.group("title").strip()
        body = body[: title_match.start()].strip()

    day_match = re.search(
        r"\b(?:(?:for|on)\s+)?(?P<day>today|tomorrow|this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        body,
        re.IGNORECASE,
    )
    if day_match is None:
        return None

    raw_title = body[: day_match.start()].strip()
    remainder = body[day_match.end() :].strip()
    raw_title = re.sub(r"^(?:an?\s+)?event\s*$", "", raw_title, flags=re.IGNORECASE).strip()
    raw_title = re.sub(r"^(?:an?\s+)?event\s+", "", raw_title, flags=re.IGNORECASE).strip()
    raw_title = re.sub(r"\s+(?:for|on)$", "", raw_title, flags=re.IGNORECASE).strip()

    time_range = re.search(
        r"\bfrom\s+(?P<start>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:to|-)\s+(?P<end>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        remainder,
        re.IGNORECASE,
    )
    single_time = re.search(
        r"\bat\s+(?P<start>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        remainder,
        re.IGNORECASE,
    )
    part_of_day = re.search(r"\b(?P<part>morning|afternoon|evening|tonight|night)\b", remainder, re.IGNORECASE)
    if time_range is None and single_time is None and part_of_day is None:
        return None

    day_value = _resolve_day_token(day_match.group("day"), current=current, timezone_name=timezone_name)
    if part_of_day is not None and time_range is None and single_time is None:
        start = _default_time_for_part_of_day(part_of_day.group("part"), reference_day=day_value, timezone_name=timezone_name)
        end = start + timedelta(hours=1)
    else:
        start_text = (time_range or single_time).group("start")
        start = _parse_clock_time(start_text, reference_day=day_value, timezone_name=timezone_name)
        if start is None:
            return None
        if time_range is not None:
            end = _parse_end_clock_time(time_range.group("end"), start=start, timezone_name=timezone_name)
            if end is None:
                return None
        else:
            end = start + timedelta(hours=1)
    if time_range is not None:
        end = _parse_end_clock_time(time_range.group("end"), start=start, timezone_name=timezone_name)
        if end is None:
            return None
    raw_title = explicit_title or raw_title or "New event"
    title = _clean_title(raw_title)
    attendees = tuple(re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", normalized))
    return CalendarEventDraft(
        title=title,
        start=start,
        end=end,
        attendees=attendees,
    )


def parse_calendar_event_reference(message: str) -> str | None:
    match = re.search(r"\b(?:event|appointment|meeting)\s+(?:id\s*)?(?P<event_id>[A-Za-z0-9_\-@.]+)", message, re.IGNORECASE)
    if match:
        return match.group("event_id")
    match = re.search(r"\b(?:id\s+)(?P<event_id>[A-Za-z0-9_\-@.]+)", message, re.IGNORECASE)
    return match.group("event_id") if match else None


def parse_calendar_event_update_request(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
) -> CalendarEventUpdateDraft | None:
    event_id = parse_calendar_event_reference(message)
    if event_id is None:
        return None

    normalized = " ".join(message.strip().split())
    updates: dict[str, Any] = {}
    descriptions: list[str] = []

    title_match = re.search(
        r"\b(?:title|name|rename|called|summary)\s+(?:to|as)\s+(?P<title>.+)$",
        normalized,
        re.IGNORECASE,
    )
    if title_match:
        title = _clean_title(title_match.group("title"))
        if title:
            updates["summary"] = title
            descriptions.append(f"title to {title}")

    location_match = re.search(
        r"\blocation\s+(?:to|as)\s+(?P<location>.+)$",
        normalized,
        re.IGNORECASE,
    )
    if location_match:
        location = location_match.group("location").strip().strip("\"'")
        if location:
            updates["location"] = location
            descriptions.append(f"location to {location}")

    description_match = re.search(
        r"\b(?:description|notes?)\s+(?:to|as)\s+(?P<description>.+)$",
        normalized,
        re.IGNORECASE,
    )
    if description_match:
        description = description_match.group("description").strip().strip("\"'")
        if description:
            updates["description"] = description
            descriptions.append("description")

    time_match = re.search(
        r"\b(?:move|reschedule|change|update)\b.+?\bto\s+"
        r"(?:(?P<day>today|tomorrow)\s+)?(?:at\s+)?(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        normalized,
        re.IGNORECASE,
    )
    if time_match and not any(key in updates for key in ("summary", "location", "description")):
        day_text = time_match.group("day")
        current = (now or datetime.now(_resolve_timezone(timezone_name))).astimezone(
            _resolve_timezone(timezone_name)
        )
        reference_day = current + timedelta(days=1) if day_text and day_text.lower() == "tomorrow" else current
        start = _parse_clock_time(
            time_match.group("time"),
            reference_day=reference_day,
            timezone_name=timezone_name,
        )
        if start is not None:
            end = start + timedelta(hours=1)
            updates["start"] = {"dateTime": start.isoformat()}
            updates["end"] = {"dateTime": end.isoformat()}
            descriptions.append(f"time to {start.strftime('%I:%M %p').lstrip('0')}")

    if not updates:
        return None

    return CalendarEventUpdateDraft(
        event_id=event_id,
        updates=updates,
        description=", ".join(descriptions) if descriptions else "requested fields",
    )


def _clean_title(value: str) -> str:
    title = re.sub(r"\s+with\s+[\w.\-+]+@[\w.\-]+\.\w+", "", value, flags=re.IGNORECASE)
    title = re.sub(r"\s+(?:and\s+)?(?:send\s+)?(?:an\s+)?invite[s]?", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^(?:for|on)\s+", "", title, flags=re.IGNORECASE)
    return title.strip().strip("\"'") or "New event"


def _extract_day_token(value: str) -> str | None:
    match = re.search(
        r"\b(?P<day>today|tomorrow|this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        value,
        re.IGNORECASE,
    )
    return " ".join(match.group("day").lower().split()) if match else None


def _parse_availability_query(
    normalized: str,
    *,
    current: datetime,
    timezone_name: str,
) -> CalendarQuery | None:
    if not any(token in normalized for token in ("free", "anything", "busy", "available")):
        return None
    day_token = _extract_day_token(normalized) or "today"
    target_day = _resolve_day_token(day_token, current=current, timezone_name=timezone_name)

    if "after school" in normalized:
        start = target_day.replace(hour=15, minute=0, second=0, microsecond=0)
        end = target_day.replace(hour=23, minute=59, second=59, microsecond=0)
        label = f"after school {day_token}" if day_token != "today" else "after school today"
        return CalendarQuery(
            day=target_day,
            label=label,
            mode="availability",
            window_start=start,
            window_end=end,
        )

    time_match = re.search(
        r"\bat\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        normalized,
        re.IGNORECASE,
    )
    if time_match:
        start = _parse_clock_time(time_match.group("time"), reference_day=target_day, timezone_name=timezone_name)
        if start is None:
            return None
        end = start + timedelta(hours=1)
        day_label = day_token if day_token != "today" else "today"
        return CalendarQuery(
            day=target_day,
            label=f"at {start.strftime('%I:%M %p').lstrip('0')} {day_label}",
            mode="availability",
            window_start=start,
            window_end=end,
        )

    if "tomorrow" in normalized or "today" in normalized:
        return CalendarQuery(day=target_day, label=day_token, mode="availability")
    return None


def _resolve_day_token(day_text: str, *, current: datetime, timezone_name: str) -> datetime:
    normalized = " ".join(day_text.lower().split())
    if normalized == "today":
        return current
    if normalized == "tomorrow":
        return current + timedelta(days=1)
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    is_next = normalized.startswith("next ")
    weekday_name = normalized.replace("this ", "").replace("next ", "")
    target_weekday = weekdays[weekday_name]
    days_ahead = (target_weekday - current.weekday()) % 7
    if is_next and days_ahead == 0:
        days_ahead = 7
    target = current + timedelta(days=days_ahead)
    return target.astimezone(_resolve_timezone(timezone_name))


def _parse_end_clock_time(value: str, *, start: datetime, timezone_name: str) -> datetime | None:
    end = _parse_clock_time(value, reference_day=start, timezone_name=timezone_name)
    if end is None:
        return None
    if not re.search(r"\b(?:am|pm)\b", value, re.IGNORECASE) and start.hour >= 12 and end.hour < 12:
        end = end.replace(hour=end.hour + 12)
    if end <= start:
        end = end + timedelta(days=1)
    return end


def _parse_clock_time(value: str, *, reference_day: datetime, timezone_name: str) -> datetime | None:
    cleaned = " ".join(value.strip().lower().split())
    formats = ["%I %p", "%I:%M %p", "%H:%M", "%H"]
    tz = _resolve_timezone(timezone_name)
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        hour = parsed.hour
        if fmt == "%H" and cleaned.isdigit():
            raw_hour = int(cleaned)
            if 1 <= raw_hour <= 7:
                hour = raw_hour + 12
        return reference_day.replace(hour=hour, minute=parsed.minute, second=0, microsecond=0, tzinfo=tz)
    return None


def _default_time_for_part_of_day(part: str, *, reference_day: datetime, timezone_name: str) -> datetime:
    normalized = part.lower()
    hour = 9
    if normalized == "afternoon":
        hour = 13
    elif normalized in {"evening", "tonight", "night"}:
        hour = 18
    return reference_day.replace(hour=hour, minute=0, second=0, microsecond=0, tzinfo=_resolve_timezone(timezone_name))


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        local_tz = datetime.now().astimezone().tzinfo
        return local_tz or timezone.utc
