"""Compatibility provider over the Google Calendar client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import Settings, settings
from integrations.google_calendar_client import GoogleCalendarClient, NormalizedGoogleCalendarEvent


@dataclass(frozen=True)
class GoogleCalendarEvent:
    event_id: str
    title: str
    start: datetime
    end: datetime
    calendar_id: str = "primary"
    timezone: str | None = None
    location: str | None = None
    description_snippet: str | None = None
    attendees_count: int = 0
    attendees: tuple[str, ...] = ()
    html_link: str | None = None
    source: str = "google_calendar"


class GoogleCalendarProvider:
    """Google Calendar provider used by existing assistant services."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        client: GoogleCalendarClient | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.client = client or GoogleCalendarClient(runtime_settings=self.settings)

    def readiness_blockers(self) -> list[str]:
        return list(self.client.readiness().blockers)

    def list_events(self, *, start: datetime, end: datetime, max_results: int = 20) -> list[GoogleCalendarEvent]:
        return [
            _to_google_calendar_event(item)
            for item in self.client.list_events(start=start, end=end, max_results=max_results)
            if item.start_datetime is not None and item.end_datetime is not None
        ]

    def get_event(self, *, event_id: str, calendar_id: str | None = None) -> GoogleCalendarEvent:
        event = self.client.get_event(event_id=event_id, calendar_id=calendar_id)
        return _to_google_calendar_event(event)

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        attendees: list[str] | None = None,
        send_updates: bool = False,
    ) -> GoogleCalendarEvent:
        event = self.client.create_event(
            title=title,
            start=start,
            end=end,
            description=description,
            attendees=attendees,
            send_updates=send_updates,
        )
        return _to_google_calendar_event(event)

    def update_event(
        self,
        *,
        event_id: str,
        updates: dict,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> GoogleCalendarEvent:
        event = self.client.update_event(
            event_id=event_id,
            calendar_id=calendar_id,
            updates=updates,
            send_updates=send_updates,
        )
        return _to_google_calendar_event(event)

    def delete_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> dict[str, str]:
        return self.client.delete_event(
            event_id=event_id,
            calendar_id=calendar_id,
            send_updates=send_updates,
        )


def _to_google_calendar_event(event: NormalizedGoogleCalendarEvent) -> GoogleCalendarEvent:
    start = event.start_datetime
    end = event.end_datetime
    if start is None or end is None:
        raise RuntimeError("Google Calendar returned an event without valid start and end times.")
    return GoogleCalendarEvent(
        event_id=event.event_id,
        title=event.title,
        start=start,
        end=end,
        calendar_id=event.calendar_id,
        timezone=event.timezone,
        location=event.location,
        description_snippet=event.description_snippet,
        attendees_count=event.attendees_count,
        attendees=event.attendees,
        html_link=event.htmlLink,
        source=event.source,
    )
