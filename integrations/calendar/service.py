"""Assistant-facing scheduling calendar service with honest readiness behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings, settings
from integrations.calendar.google_provider import GoogleCalendarEvent, GoogleCalendarProvider


@dataclass(frozen=True)
class CalendarServiceResult:
    success: bool
    summary: str
    events: tuple[GoogleCalendarEvent, ...] = ()
    blockers: tuple[str, ...] = ()
    created_event: GoogleCalendarEvent | None = None
    updated_event: GoogleCalendarEvent | None = None
    deleted_event_id: str | None = None


class CalendarService:
    """Small assistant-safe wrapper around configured calendar providers."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        provider: GoogleCalendarProvider | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.provider = provider or GoogleCalendarProvider(runtime_settings=self.settings)

    def readiness_blockers(self) -> list[str]:
        return self.provider.readiness_blockers()

    def list_events(self, *, start: datetime, end: datetime) -> CalendarServiceResult:
        blockers = self.readiness_blockers()
        if blockers:
            return CalendarServiceResult(
                success=False,
                summary="Calendar access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            events = tuple(self.provider.list_events(start=start, end=end))
        except (RuntimeError, httpx.HTTPError) as exc:
            return CalendarServiceResult(
                success=False,
                summary="I couldn't read Google Calendar right now.",
                blockers=(str(exc),),
            )
        return CalendarServiceResult(
            success=True,
            summary="Calendar events loaded.",
            events=events,
        )

    def events_for_day(self, *, target_day: datetime) -> CalendarServiceResult:
        tz = target_day.tzinfo or ZoneInfo(self.settings.scheduler_timezone)
        start = target_day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
        end = start + timedelta(days=1)
        return self.list_events(start=start, end=end)

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        attendees: list[str] | None = None,
        send_updates: bool = False,
    ) -> CalendarServiceResult:
        blockers = self.readiness_blockers()
        if blockers:
            return CalendarServiceResult(
                success=False,
                summary="Calendar access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            try:
                event = self.provider.create_event(
                    title=title,
                    start=start,
                    end=end,
                    description=description,
                    attendees=attendees,
                    send_updates=send_updates,
                )
            except TypeError:
                event = self.provider.create_event(
                    title=title,
                    start=start,
                    end=end,
                    description=description,
                )
        except (RuntimeError, httpx.HTTPError) as exc:
            return CalendarServiceResult(
                success=False,
                summary="I couldn't create that calendar event right now.",
                blockers=(str(exc),),
            )
        return CalendarServiceResult(
            success=True,
            summary="Calendar event created.",
            created_event=event,
            events=(event,),
        )

    def get_event(self, *, event_id: str, calendar_id: str | None = None) -> CalendarServiceResult:
        blockers = self.readiness_blockers()
        if blockers:
            return CalendarServiceResult(
                success=False,
                summary="Calendar access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            event = self.provider.get_event(event_id=event_id, calendar_id=calendar_id)
        except (RuntimeError, httpx.HTTPError) as exc:
            return CalendarServiceResult(
                success=False,
                summary="I couldn't read that Google Calendar event right now.",
                blockers=(str(exc),),
            )
        return CalendarServiceResult(success=True, summary="Calendar event loaded.", events=(event,))

    def update_event(
        self,
        *,
        event_id: str,
        updates: dict[str, Any],
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> CalendarServiceResult:
        if not updates:
            return CalendarServiceResult(
                success=False,
                summary="Calendar event update needs at least one concrete field to change.",
                blockers=("No calendar update fields were parsed from the request.",),
            )
        blockers = self.readiness_blockers()
        if blockers:
            return CalendarServiceResult(
                success=False,
                summary="Calendar access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            event = self.provider.update_event(
                event_id=event_id,
                calendar_id=calendar_id,
                updates=updates,
                send_updates=send_updates,
            )
        except (RuntimeError, httpx.HTTPError, ValueError) as exc:
            return CalendarServiceResult(
                success=False,
                summary="I couldn't update that Google Calendar event right now.",
                blockers=(str(exc),),
            )
        return CalendarServiceResult(
            success=True,
            summary="Calendar event updated.",
            updated_event=event,
            events=(event,),
        )

    def delete_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> CalendarServiceResult:
        blockers = self.readiness_blockers()
        if blockers:
            return CalendarServiceResult(
                success=False,
                summary="Calendar access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            self.provider.delete_event(
                event_id=event_id,
                calendar_id=calendar_id,
                send_updates=send_updates,
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            return CalendarServiceResult(
                success=False,
                summary="I couldn't delete that Google Calendar event right now.",
                blockers=(str(exc),),
            )
        return CalendarServiceResult(
            success=True,
            summary="Calendar event deleted.",
            deleted_event_id=event_id,
        )
