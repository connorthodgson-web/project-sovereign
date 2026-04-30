"""Google Calendar client with local OAuth token handling and safe evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
import re
from typing import Any

from app.config import Settings, settings


@dataclass(frozen=True)
class GoogleCalendarReadiness:
    enabled: bool
    configured: bool
    can_run_local_auth: bool
    live: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedGoogleCalendarEvent:
    event_id: str
    calendar_id: str
    title: str
    start: str
    end: str
    timezone: str | None = None
    location: str | None = None
    description_snippet: str | None = None
    attendees_count: int = 0
    attendees: tuple[str, ...] = ()
    htmlLink: str | None = None
    source: str = "google_calendar"

    @property
    def start_datetime(self) -> datetime | None:
        return _parse_datetime_value(self.start)

    @property
    def end_datetime(self) -> datetime | None:
        return _parse_datetime_value(self.end)


class GoogleCalendarClient:
    """Thin Google Calendar API adapter that never logs or returns secrets."""

    def __init__(self, *, runtime_settings: Settings | None = None, calendar_id: str | None = None) -> None:
        self.settings = runtime_settings or settings
        self.calendar_id = calendar_id or self.settings.calendar_id or "primary"

    def readiness(self) -> GoogleCalendarReadiness:
        blockers: list[str] = []
        deps_available = _google_deps_available()
        credentials_path = self._credentials_path()
        token_path = self._token_path()

        if not self.settings.google_calendar_enabled:
            blockers.append("GOOGLE_CALENDAR_ENABLED is false.")
        if not deps_available:
            blockers.append(
                "Google Calendar dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib."
            )
        if not credentials_path.exists():
            blockers.append("GOOGLE_CALENDAR_CREDENTIALS_PATH does not point to an existing credentials file.")

        can_run_local_auth = (
            self.settings.google_calendar_enabled
            and deps_available
            and credentials_path.exists()
        )
        token_exists = token_path.exists()
        configured = can_run_local_auth and token_exists
        live = configured and not blockers
        if can_run_local_auth and not token_exists:
            blockers.append("GOOGLE_CALENDAR_TOKEN_PATH does not exist yet; run the local OAuth flow once.")

        return GoogleCalendarReadiness(
            enabled=self.settings.google_calendar_enabled,
            configured=configured,
            can_run_local_auth=can_run_local_auth,
            live=live,
            blockers=tuple(blockers),
        )

    def setup_needed_message(self) -> str:
        readiness = self.readiness()
        blockers = " ".join(_humanize_readiness_blocker(item) for item in readiness.blockers)
        if not blockers:
            blockers = "One more setup step is needed before I can connect to your calendar."
        return f"Google Calendar setup is needed before I can use your calendar. {blockers}"

    def run_local_auth_flow(self) -> Path:
        """Run the first-time OAuth flow locally and write the token file."""

        self._ensure_google_deps()
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials_path = self._credentials_path()
        if not credentials_path.exists():
            raise RuntimeError("GOOGLE_CALENDAR_CREDENTIALS_PATH does not point to an existing credentials file.")

        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), self._scopes())
        credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        return token_path

    def list_calendars(self) -> list[dict[str, str]]:
        service = self._service()
        payload = service.calendarList().list().execute()
        calendars = []
        for item in payload.get("items", []):
            calendars.append(
                {
                    "calendar_id": str(item.get("id", "")),
                    "summary": str(item.get("summary", "")),
                    "timeZone": str(item.get("timeZone", "")),
                }
            )
        return calendars

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
        max_results: int = 20,
    ) -> list[NormalizedGoogleCalendarEvent]:
        resolved_calendar_id = calendar_id or self.calendar_id
        service = self._service()
        payload = (
            service.events()
            .list(
                calendarId=resolved_calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
        return [
            self.normalize_event(item, calendar_id=resolved_calendar_id)
            for item in payload.get("items", [])
            if isinstance(item, dict)
        ]

    def get_event(self, *, event_id: str, calendar_id: str | None = None) -> NormalizedGoogleCalendarEvent:
        resolved_calendar_id = calendar_id or self.calendar_id
        service = self._service()
        payload = service.events().get(calendarId=resolved_calendar_id, eventId=event_id).execute()
        return self.normalize_event(payload, calendar_id=resolved_calendar_id)

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
        timezone: str | None = None,
        location: str | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
        send_updates: bool = False,
    ) -> NormalizedGoogleCalendarEvent:
        resolved_calendar_id = calendar_id or self.calendar_id
        service = self._service()
        body = self._event_body(
            title=title,
            start=start,
            end=end,
            timezone=timezone,
            location=location,
            description=description,
            attendees=attendees,
        )
        payload = (
            service.events()
            .insert(
                calendarId=resolved_calendar_id,
                body=body,
                sendUpdates="all" if send_updates else "none",
            )
            .execute()
        )
        return self.normalize_event(payload, calendar_id=resolved_calendar_id)

    def update_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
        updates: dict[str, Any],
        send_updates: bool = False,
    ) -> NormalizedGoogleCalendarEvent:
        if not updates:
            raise ValueError("Google Calendar update payload must not be empty.")
        resolved_calendar_id = calendar_id or self.calendar_id
        service = self._service()
        existing = service.events().get(calendarId=resolved_calendar_id, eventId=event_id).execute()
        body = {**existing, **updates}
        payload = (
            service.events()
            .update(
                calendarId=resolved_calendar_id,
                eventId=event_id,
                body=body,
                sendUpdates="all" if send_updates else "none",
            )
            .execute()
        )
        return self.normalize_event(payload, calendar_id=resolved_calendar_id)

    def delete_event(
        self,
        *,
        event_id: str,
        calendar_id: str | None = None,
        send_updates: bool = False,
    ) -> dict[str, str]:
        resolved_calendar_id = calendar_id or self.calendar_id
        service = self._service()
        service.events().delete(
            calendarId=resolved_calendar_id,
            eventId=event_id,
            sendUpdates="all" if send_updates else "none",
        ).execute()
        return {
            "event_id": event_id,
            "calendar_id": resolved_calendar_id,
            "source": "google_calendar",
            "status": "deleted",
        }

    def normalize_event(self, event: dict[str, Any], *, calendar_id: str | None = None) -> NormalizedGoogleCalendarEvent:
        start_payload = event.get("start") if isinstance(event.get("start"), dict) else {}
        end_payload = event.get("end") if isinstance(event.get("end"), dict) else {}
        attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
        attendee_emails = tuple(
            str(item.get("email", "")).strip()
            for item in attendees
            if isinstance(item, dict) and str(item.get("email", "")).strip()
        )
        description = str(event.get("description") or "").strip()
        timezone = (
            str(start_payload.get("timeZone") or "").strip()
            or str(end_payload.get("timeZone") or "").strip()
            or None
        )
        return NormalizedGoogleCalendarEvent(
            event_id=str(event.get("id", "")),
            calendar_id=calendar_id or self.calendar_id,
            title=str(event.get("summary") or "Untitled event"),
            start=str(start_payload.get("dateTime") or start_payload.get("date") or ""),
            end=str(end_payload.get("dateTime") or end_payload.get("date") or ""),
            timezone=timezone,
            location=str(event.get("location") or "").strip() or None,
            description_snippet=description[:160] if description else None,
            attendees_count=len(attendee_emails),
            attendees=attendee_emails,
            htmlLink=event.get("htmlLink"),
        )

    def _service(self):
        self._ensure_google_deps()
        readiness = self.readiness()
        if not readiness.live:
            raise RuntimeError(self.setup_needed_message())
        from googleapiclient.discovery import build

        return build("calendar", "v3", credentials=self._credentials())

    def _credentials(self):
        self._ensure_google_deps()
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        token_path = self._token_path()
        credentials = Credentials.from_authorized_user_file(str(token_path), self._scopes())
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials or not credentials.valid:
            raise RuntimeError("Google Calendar token is invalid; rerun the local OAuth flow.")
        return credentials

    def _event_body(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        timezone: str | None,
        location: str | None,
        description: str | None,
        attendees: list[str] | None,
    ) -> dict[str, Any]:
        start_payload: dict[str, str] = {"dateTime": start.isoformat()}
        end_payload: dict[str, str] = {"dateTime": end.isoformat()}
        if timezone:
            start_payload["timeZone"] = timezone
            end_payload["timeZone"] = timezone
        body: dict[str, Any] = {
            "summary": title,
            "start": start_payload,
            "end": end_payload,
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]
        return body

    def _credentials_path(self) -> Path:
        return _resolve_repo_relative_path(self.settings.google_calendar_credentials_path)

    def _token_path(self) -> Path:
        return _resolve_repo_relative_path(self.settings.google_calendar_token_path)

    def _scopes(self) -> list[str]:
        raw = self.settings.google_calendar_scopes or "https://www.googleapis.com/auth/calendar"
        return [item.strip() for item in raw.replace(",", " ").split() if item.strip()]

    def _ensure_google_deps(self) -> None:
        if not _google_deps_available():
            raise RuntimeError(
                "Google Calendar dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib."
            )


def _resolve_repo_relative_path(value: str | None) -> Path:
    raw = value or ""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent.parent / path


def _google_deps_available() -> bool:
    return all(
        find_spec(module_name) is not None
        for module_name in (
            "googleapiclient.discovery",
            "google_auth_oauthlib.flow",
            "google.oauth2.credentials",
            "google.auth.transport.requests",
        )
    )


def _humanize_readiness_blocker(text: str) -> str:
    cleaned = " ".join(text.strip().rstrip(".").split())
    replacements = {
        r"\bGOOGLE_CALENDAR_ENABLED\b is false": "Google Calendar is not enabled",
        r"\bGOOGLE_CALENDAR_CREDENTIALS_PATH\b does not point to an existing credentials file": "the Google Calendar credentials file is missing",
        r"\bGOOGLE_CALENDAR_TOKEN_PATH\b does not exist yet; run the local OAuth flow once": "saved Google Calendar access is missing; sign in to Google Calendar once from this machine",
        r"\bGoogle Calendar dependencies are missing: .+": "the Google Calendar Python packages are not installed",
        r"\bOAuth\b": "Google sign-in",
        r"\btoken file\b": "saved calendar access",
    }
    for source, target in replacements.items():
        cleaned = re.sub(source, target, cleaned, flags=re.IGNORECASE)
    return cleaned.rstrip(".") + "."


def _parse_datetime_value(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if "T" not in value:
            return datetime.fromisoformat(f"{value}T00:00:00+00:00")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
