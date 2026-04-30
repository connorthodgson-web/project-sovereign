"""Google Tasks client with local token handling and safe readiness messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings, settings


@dataclass(frozen=True)
class GoogleTasksReadiness:
    enabled: bool
    configured: bool
    can_run_local_auth: bool
    live: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedGoogleTask:
    task_id: str
    title: str
    status: str
    task_list_id: str = "@default"
    due: str | None = None
    notes: str | None = None
    updated: str | None = None
    completed: str | None = None
    position: str | None = None
    source: str = "google_tasks"

    @property
    def due_datetime(self) -> datetime | None:
        return _parse_datetime_value(self.due)


class GoogleTasksClient:
    """Thin Google Tasks API adapter that never logs or returns secrets."""

    def __init__(self, *, runtime_settings: Settings | None = None, task_list_id: str | None = None) -> None:
        self.settings = runtime_settings or settings
        self.task_list_id = task_list_id or self.settings.google_tasks_list_id or "@default"

    def readiness(self) -> GoogleTasksReadiness:
        blockers: list[str] = []
        deps_available = _google_deps_available()
        credentials_path = self._credentials_path()
        token_path = self._token_path()

        if not self.settings.google_tasks_enabled:
            blockers.append("GOOGLE_TASKS_ENABLED is false.")
        if not deps_available:
            blockers.append(
                "Google Tasks dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib."
            )
        if not credentials_path.exists():
            blockers.append("GOOGLE_TASKS_CREDENTIALS_PATH does not point to an existing credentials file.")

        can_run_local_auth = self.settings.google_tasks_enabled and deps_available and credentials_path.exists()
        token_exists = token_path.exists()
        configured = can_run_local_auth and token_exists
        live = configured and not blockers
        if can_run_local_auth and not token_exists:
            blockers.append("GOOGLE_TASKS_TOKEN_PATH does not exist yet; run the local OAuth flow once.")

        return GoogleTasksReadiness(
            enabled=self.settings.google_tasks_enabled,
            configured=configured,
            can_run_local_auth=can_run_local_auth,
            live=live,
            blockers=tuple(blockers),
        )

    def setup_needed_message(self) -> str:
        readiness = self.readiness()
        blockers = " ".join(_humanize_readiness_blocker(item) for item in readiness.blockers)
        if not blockers:
            blockers = "One more setup step is needed before I can connect to your tasks."
        return f"Google Tasks setup is needed before I can use your tasks. {blockers}"

    def run_local_auth_flow(self) -> Path:
        self._ensure_google_deps()
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials_path = self._credentials_path()
        if not credentials_path.exists():
            raise RuntimeError("GOOGLE_TASKS_CREDENTIALS_PATH does not point to an existing credentials file.")

        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), self._scopes())
        credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        return token_path

    def list_tasks(
        self,
        *,
        task_list_id: str | None = None,
        show_completed: bool = False,
        max_results: int = 20,
    ) -> list[NormalizedGoogleTask]:
        resolved_list_id = task_list_id or self.task_list_id
        service = self._service()
        payload = (
            service.tasks()
            .list(
                tasklist=resolved_list_id,
                showCompleted=show_completed,
                showHidden=False,
                maxResults=max_results,
            )
            .execute()
        )
        return [
            self.normalize_task(item, task_list_id=resolved_list_id)
            for item in payload.get("items", [])
            if isinstance(item, dict)
        ]

    def create_task(
        self,
        *,
        title: str,
        due: datetime | None = None,
        notes: str | None = None,
        task_list_id: str | None = None,
    ) -> NormalizedGoogleTask:
        resolved_list_id = task_list_id or self.task_list_id
        body: dict[str, Any] = {"title": title}
        if due is not None:
            body["due"] = due.isoformat()
        if notes:
            body["notes"] = notes
        service = self._service()
        task_payload = service.tasks().insert(tasklist=resolved_list_id, body=body).execute()
        return self.normalize_task(task_payload, task_list_id=resolved_list_id)

    def complete_task(
        self,
        *,
        task_id: str,
        task_list_id: str | None = None,
    ) -> NormalizedGoogleTask:
        resolved_list_id = task_list_id or self.task_list_id
        service = self._service()
        now = datetime.now().astimezone().isoformat()
        payload = (
            service.tasks()
            .patch(
                tasklist=resolved_list_id,
                task=task_id,
                body={"status": "completed", "completed": now},
            )
            .execute()
        )
        return self.normalize_task(payload, task_list_id=resolved_list_id)

    def normalize_task(self, task: dict[str, Any], *, task_list_id: str | None = None) -> NormalizedGoogleTask:
        return NormalizedGoogleTask(
            task_id=str(task.get("id", "")),
            title=str(task.get("title") or "Untitled task"),
            status=str(task.get("status") or "needsAction"),
            task_list_id=task_list_id or self.task_list_id,
            due=str(task.get("due") or "").strip() or None,
            notes=str(task.get("notes") or "").strip() or None,
            updated=str(task.get("updated") or "").strip() or None,
            completed=str(task.get("completed") or "").strip() or None,
            position=str(task.get("position") or "").strip() or None,
        )

    def _service(self):
        self._ensure_google_deps()
        readiness = self.readiness()
        if not readiness.live:
            raise RuntimeError(self.setup_needed_message())
        from googleapiclient.discovery import build

        return build("tasks", "v1", credentials=self._credentials())

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
            raise RuntimeError("Google Tasks saved access is invalid; sign in again from this machine.")
        return credentials

    def _credentials_path(self) -> Path:
        return _resolve_repo_relative_path(self.settings.google_tasks_credentials_path)

    def _token_path(self) -> Path:
        return _resolve_repo_relative_path(self.settings.google_tasks_token_path)

    def _scopes(self) -> list[str]:
        raw = self.settings.google_tasks_scopes or "https://www.googleapis.com/auth/tasks"
        return [item.strip() for item in raw.replace(",", " ").split() if item.strip()]

    def _ensure_google_deps(self) -> None:
        if not _google_deps_available():
            raise RuntimeError(
                "Google Tasks dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib."
            )


def _resolve_repo_relative_path(value: str | None) -> Path:
    raw = value or ""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent.parent.parent / path


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
        r"\bGOOGLE_TASKS_ENABLED\b is false": "Google Tasks is not enabled",
        r"\bGOOGLE_TASKS_CREDENTIALS_PATH\b does not point to an existing credentials file": "the Google Tasks credentials file is missing",
        r"\bGOOGLE_TASKS_TOKEN_PATH\b does not exist yet; run the local OAuth flow once": "saved Google Tasks access is missing; sign in to Google Tasks once from this machine",
        r"\bGoogle Tasks dependencies are missing: .+": "the Google Tasks Python packages are not installed",
        r"\bOAuth\b": "Google sign-in",
        r"\btoken file\b": "saved task access",
    }
    for source, target in replacements.items():
        cleaned = re.sub(source, target, cleaned, flags=re.IGNORECASE)
    return cleaned.rstrip(".") + "."


def _parse_datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None
    date_only_match = re.match(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:T00:00:00(?:\.000)?(?:Z|\+00:00)?)?$", value)
    if date_only_match:
        timezone = ZoneInfo(settings.scheduler_timezone)
        return datetime.fromisoformat(date_only_match.group("date")).replace(tzinfo=timezone)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
