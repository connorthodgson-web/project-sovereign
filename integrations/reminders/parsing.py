"""Deterministic parsing for lightweight one-time reminder requests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from core.model_routing import ModelRequestContext
from integrations.openrouter_client import OpenRouterClient


@dataclass(frozen=True)
class ParsedReminderRequest:
    summary: str
    deliver_at: datetime
    schedule_phrase: str
    confidence: float = 1.0
    parser: str = "deterministic"


@dataclass(frozen=True)
class ReminderParseOutcome:
    parsed: ParsedReminderRequest | None
    failure_reason: str | None = None
    attempted_llm_fallback: bool = False


_IN_RELATIVE_PATTERN = re.compile(
    r"\b(?:remind me|set a reminder)\s+in\s+(?P<count>\d+|a\s+couple|couple|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?P<unit>"
    r"second|seconds|sec|secs|s|minute|minutes|min|mins|m|hour|hours|hr|hrs|h|day|days|d"
    r")\b(?:\s+(?P<linker>to|that|about))?\s+(?P<summary>.+)$",
    re.IGNORECASE,
)
_AT_TIME_PATTERN = re.compile(
    r"\b(?:remind me|set a reminder)\s+(?:at|on)\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"(?:\s+(?P<linker>to|that|about))?\s+(?P<summary>.+)$",
    re.IGNORECASE,
)
_TOMORROW_PATTERN = re.compile(
    r"\b(?:remind me|set a reminder)\s+tomorrow\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"(?:\s+(?P<linker>to|that|about))?\s+(?P<summary>.+)$",
    re.IGNORECASE,
)
_AFTER_RELATIVE_PATTERN = re.compile(
    r"\b(?:remind me|set a reminder)\s+(?:after|within)\s+(?P<count>\d+|a\s+couple|couple|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?P<unit>"
    r"second|seconds|sec|secs|s|minute|minutes|min|mins|m|hour|hours|hr|hrs|h|day|days|d"
    r")\b(?:\s+(?P<linker>to|that|about))?\s+(?P<summary>.+)$",
    re.IGNORECASE,
)
_GO_OFF_RELATIVE_PATTERN = re.compile(
    r"\b(?:set a reminder|schedule a reminder|remind me)\s+(?:to\s+)?go off\s+in\s+"
    r"(?P<count>\d+|a\s+couple|couple|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?P<unit>second|seconds|sec|secs|s|minute|minutes|min|mins|m|hour|hours|hr|hrs|h|day|days|d)"
    r"\b(?:\s+(?:telling me|tell me|to tell me|saying|that|to|about))?\s+(?P<summary>.+)$",
    re.IGNORECASE,
)
_UNIT_ALIASES = {
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
    "d": "days",
    "day": "days",
    "days": "days",
}
_COUNT_ALIASES = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "couple": 2,
    "a couple": 2,
}
_EDGE_FILLER_PATTERNS = (
    r"please",
    r"plz",
    r"thanks",
    r"thank you",
    r"for me",
    r"real quick",
    r"if you can",
    r"okay",
    r"ok",
)


def parse_one_time_reminder_request(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
    openrouter_client: OpenRouterClient | None = None,
) -> ParsedReminderRequest | None:
    outcome = parse_one_time_reminder_request_with_fallback(
        message,
        now=now,
        timezone_name=timezone_name,
        openrouter_client=openrouter_client,
    )
    return outcome.parsed


def parse_one_time_reminder_request_with_fallback(
    message: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
    openrouter_client: OpenRouterClient | None = None,
) -> ReminderParseOutcome:
    normalized = " ".join(message.strip().split())
    if not normalized:
        return ReminderParseOutcome(
            parsed=None,
            failure_reason="The reminder request was empty.",
        )

    tz = _resolve_timezone(timezone_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)

    for pattern, preposition in (
        (_IN_RELATIVE_PATTERN, "in"),
        (_AFTER_RELATIVE_PATTERN, "after"),
        (_GO_OFF_RELATIVE_PATTERN, "in"),
    ):
        relative_match = pattern.search(normalized)
        if relative_match:
            count = _normalize_count(relative_match.group("count"))
            unit = _normalize_unit(relative_match.group("unit"))
            summary = _normalize_summary(relative_match.group("summary"))
            delta = _build_relative_delta(count, unit)
            if summary is None:
                return ReminderParseOutcome(
                    parsed=None,
                    failure_reason="I couldn't tell what you wanted me to remind you about.",
                )
            return ReminderParseOutcome(
                parsed=ParsedReminderRequest(
                    summary=summary,
                    deliver_at=current + delta,
                    schedule_phrase=f"{preposition} {count} {unit}",
                )
            )

    tomorrow_match = _TOMORROW_PATTERN.search(normalized)
    if tomorrow_match:
        deliver_at = _parse_clock_time(
            tomorrow_match.group("time"),
            now=current + timedelta(days=1),
            timezone_name=timezone_name,
        )
        if deliver_at is None:
            return ReminderParseOutcome(
                parsed=None,
                failure_reason="I found a reminder request for tomorrow, but I couldn't parse the time.",
            )
        summary = _normalize_summary(tomorrow_match.group("summary"))
        if summary is None:
            return ReminderParseOutcome(
                parsed=None,
                failure_reason="I found the reminder time, but I couldn't tell what the reminder message should be.",
            )
        return ReminderParseOutcome(
            parsed=ParsedReminderRequest(
                summary=summary,
                deliver_at=deliver_at,
                schedule_phrase=f"tomorrow at {tomorrow_match.group('time').strip()}",
            )
        )

    at_time_match = _AT_TIME_PATTERN.search(normalized)
    if at_time_match:
        deliver_at = _parse_clock_time(
            at_time_match.group("time"),
            now=current,
            timezone_name=timezone_name,
        )
        if deliver_at is None:
            return ReminderParseOutcome(
                parsed=None,
                failure_reason="I recognized a reminder request, but I couldn't parse the requested clock time.",
            )
        if deliver_at <= current:
            deliver_at = deliver_at + timedelta(days=1)
        summary = _normalize_summary(at_time_match.group("summary"))
        if summary is None:
            return ReminderParseOutcome(
                parsed=None,
                failure_reason="I found the reminder time, but I couldn't tell what the reminder message should be.",
            )
        return ReminderParseOutcome(
            parsed=ParsedReminderRequest(
                summary=summary,
                deliver_at=deliver_at,
                schedule_phrase=f"at {at_time_match.group('time').strip()}",
            )
        )

    llm_outcome = _parse_with_llm_fallback(
        normalized,
        now=current,
        timezone_name=timezone_name,
        openrouter_client=openrouter_client,
    )
    if llm_outcome is not None:
        return llm_outcome

    return ReminderParseOutcome(
        parsed=None,
        failure_reason=(
            "I couldn't confidently parse a one-time reminder from that. "
            "Try something like 'remind me in 10 mins to stretch' or 'remind me at 6 pm that class starts soon'."
        ),
        attempted_llm_fallback=bool(openrouter_client and openrouter_client.is_configured()),
    )


def _build_relative_delta(count: int, unit: str) -> timedelta:
    if unit.startswith("second"):
        return timedelta(seconds=count)
    if unit.startswith("minute"):
        return timedelta(minutes=count)
    if unit.startswith("hour"):
        return timedelta(hours=count)
    return timedelta(days=count)


def _parse_clock_time(
    value: str,
    *,
    now: datetime,
    timezone_name: str,
) -> datetime | None:
    cleaned = " ".join(value.strip().lower().split())
    tz = ZoneInfo(timezone_name)
    formats = ["%I %p", "%I:%M %p", "%H:%M", "%H"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return now.replace(
            hour=parsed.hour,
            minute=parsed.minute,
            second=0,
            microsecond=0,
            tzinfo=tz,
        )
    return None


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        local_tz = datetime.now().astimezone().tzinfo
        return local_tz or timezone.utc


def _normalize_unit(value: str) -> str:
    return _UNIT_ALIASES.get(value.lower(), value.lower())


def _normalize_count(value: str) -> int:
    cleaned = " ".join(value.lower().split())
    if cleaned.isdigit():
        return int(cleaned)
    return _COUNT_ALIASES.get(cleaned, 1)


def _normalize_summary(value: str) -> str | None:
    summary = value.strip().rstrip(".")
    if not summary:
        return None
    for prefix in ("to ", "that ", "about "):
        if summary.lower().startswith(prefix):
            summary = summary[len(prefix):].strip()
            break
    summary = normalize_reminder_summary_text(summary) or ""
    if not summary:
        return None
    return summary


def normalize_reminder_summary_text(value: str) -> str | None:
    summary = " ".join(value.strip().split())
    if not summary:
        return None

    edge_pattern = "|".join(_EDGE_FILLER_PATTERNS)
    leading = re.compile(rf"^(?:{edge_pattern})(?:[,\s]+|$)", re.IGNORECASE)
    trailing = re.compile(rf"(?:[,\s]+|^)(?:{edge_pattern})[.!?,\s]*$", re.IGNORECASE)

    previous = None
    while summary and summary != previous:
        previous = summary
        summary = leading.sub("", summary).strip()
        summary = trailing.sub("", summary).strip(" ,.!?\t")

    summary = re.sub(r"\s{2,}", " ", summary).strip(" ,.!?\t")
    return summary or None


def _parse_with_llm_fallback(
    message: str,
    *,
    now: datetime,
    timezone_name: str,
    openrouter_client: OpenRouterClient | None,
) -> ReminderParseOutcome | None:
    if openrouter_client is None or not openrouter_client.is_configured():
        return None

    prompt = (
        "Extract a one-time reminder request.\n"
        "Return strict JSON with this shape:\n"
        '{"summary":"...","deliver_at":"ISO-8601 timestamp with timezone","schedule_phrase":"...","confidence":0.0}\n'
        "Rules:\n"
        "- Only extract one-time reminders.\n"
        "- Resolve relative times against the provided current time.\n"
        "- Use the provided timezone.\n"
        "- If the request is not a clear one-time reminder, return confidence below 0.7.\n"
        f"Current time: {now.isoformat()}\n"
        f"Timezone: {timezone_name}\n"
        f"User message: {message}"
    )

    try:
        response = openrouter_client.prompt(
            prompt,
            system_prompt=(
                "You extract reminder scheduling fields. Return only valid JSON with no markdown."
            ),
            label="reminder_parse",
            context=ModelRequestContext(
                intent_label="reminder_action",
                request_mode="act",
                selected_lane="fast_action",
                selected_agent="reminder_agent",
                task_complexity="low",
                risk_level="low",
                requires_tool_use=False,
                requires_review=False,
                evidence_quality="unknown",
                user_visible_latency_sensitivity="high",
                cost_sensitivity="high",
            ),
        )
        payload = json.loads(response)
        summary = _normalize_summary(str(payload.get("summary", "")))
        deliver_at_raw = str(payload.get("deliver_at", "")).strip()
        schedule_phrase = str(payload.get("schedule_phrase", "")).strip()
        confidence = float(payload.get("confidence", 0.0))
        if not summary or not deliver_at_raw or not schedule_phrase or confidence < 0.7:
            return ReminderParseOutcome(
                parsed=None,
                failure_reason=(
                    "I recognized the reminder intent, but I still couldn't confidently extract both the time and the reminder message."
                ),
                attempted_llm_fallback=True,
            )
        deliver_at = datetime.fromisoformat(deliver_at_raw)
        if deliver_at.tzinfo is None:
            deliver_at = deliver_at.replace(tzinfo=_resolve_timezone(timezone_name))
        if deliver_at <= now:
            return ReminderParseOutcome(
                parsed=None,
                failure_reason="I parsed a reminder time, but it was not in the future.",
                attempted_llm_fallback=True,
            )
        return ReminderParseOutcome(
            parsed=ParsedReminderRequest(
                summary=summary,
                deliver_at=deliver_at,
                schedule_phrase=schedule_phrase,
                confidence=confidence,
                parser="llm",
            ),
            attempted_llm_fallback=True,
        )
    except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        return ReminderParseOutcome(
            parsed=None,
            failure_reason=(
                "I recognized the reminder request, but the fallback parser couldn't extract a reliable one-time schedule."
            ),
            attempted_llm_fallback=True,
        )
