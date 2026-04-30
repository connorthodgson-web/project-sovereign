"""Gmail client with local OAuth token handling and safe normalized outputs."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from app.config import Settings, settings


@dataclass(frozen=True)
class GmailReadiness:
    enabled: bool
    configured: bool
    can_run_local_auth: bool
    live: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedGmailMessage:
    message_id: str
    thread_id: str | None
    subject: str
    from_: str
    to: str
    date: str
    snippet: str
    labels: tuple[str, ...] = ()
    body_text: str | None = None
    source: str = "gmail"

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": self.from_,
            "to": self.to,
            "date": self.date,
            "snippet": self.snippet,
            "body_text": self.body_text,
            "labels": list(self.labels),
            "source": self.source,
        }


class GmailClient:
    """Thin Gmail API adapter that never logs or returns credential contents."""

    def __init__(self, *, runtime_settings: Settings | None = None) -> None:
        self.settings = runtime_settings or settings

    def readiness(self) -> GmailReadiness:
        blockers: list[str] = []
        deps_available = _google_deps_available()
        credentials_path = self._credentials_path()
        token_path = self._token_path()

        if not self.settings.gmail_enabled:
            blockers.append("GMAIL_ENABLED is false.")
        if not deps_available:
            blockers.append(
                "Gmail dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib."
            )
        if not credentials_path.exists():
            blockers.append("GMAIL_CREDENTIALS_PATH does not point to an existing credentials file.")

        can_run_local_auth = self.settings.gmail_enabled and deps_available and credentials_path.exists()
        token_exists = token_path.exists()
        configured = can_run_local_auth and token_exists
        live = configured and not blockers
        if can_run_local_auth and not token_exists:
            blockers.append("GMAIL_TOKEN_PATH does not exist yet; run the local OAuth flow once.")

        return GmailReadiness(
            enabled=self.settings.gmail_enabled,
            configured=configured,
            can_run_local_auth=can_run_local_auth,
            live=live,
            blockers=tuple(blockers),
        )

    def setup_needed_message(self) -> str:
        readiness = self.readiness()
        blockers = " ".join(readiness.blockers) if readiness.blockers else "Gmail is not ready."
        return f"Gmail setup is needed before I can use your mailbox. {blockers}"

    def run_local_auth_flow(self) -> Path:
        """Run the first-time Gmail OAuth flow locally and write the token file."""

        self._ensure_google_deps()
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials_path = self._credentials_path()
        if not credentials_path.exists():
            raise RuntimeError("GMAIL_CREDENTIALS_PATH does not point to an existing credentials file.")

        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), self._scopes())
        credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        return token_path

    def list_labels(self) -> list[dict[str, str]]:
        service = self._service()
        payload = service.users().labels().list(userId="me").execute()
        return [
            {"label_id": str(item.get("id", "")), "name": str(item.get("name", "")), "source": "gmail"}
            for item in payload.get("labels", [])
            if isinstance(item, dict)
        ]

    def search_messages(
        self,
        query: str,
        *,
        max_results: int = 10,
        include_body: bool = False,
    ) -> list[NormalizedGmailMessage]:
        service = self._service()
        payload = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = payload.get("messages", [])
        return [
            self.get_message(str(item.get("id", "")), include_body=include_body)
            for item in messages
            if isinstance(item, dict) and item.get("id")
        ]

    def get_message(self, message_id: str, *, include_body: bool = False) -> NormalizedGmailMessage:
        service = self._service()
        payload = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full" if include_body else "metadata",
                metadataHeaders=["Subject", "From", "To", "Date"],
            )
            .execute()
        )
        return self.normalize_message(payload, include_body=include_body)

    def create_draft(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        service = self._service()
        raw = self._encoded_message(to=to, subject=subject, body_text=body_text)
        body: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        payload = service.users().drafts().create(userId="me", body=body).execute()
        message = payload.get("message", {}) if isinstance(payload.get("message"), dict) else {}
        return {
            "draft_id": str(payload.get("id", "")),
            "message_id": str(message.get("id", "")),
            "thread_id": str(message.get("threadId", thread_id or "")) or None,
            "to": to,
            "subject": subject,
            "snippet": body_text[:160],
            "source": "gmail",
            "status": "draft_created",
        }

    def send_draft(self, *, draft_id: str) -> dict[str, Any]:
        service = self._service()
        payload = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return {
            "message_id": str(payload.get("id", "")),
            "thread_id": str(payload.get("threadId", "")) or None,
            "source": "gmail",
            "status": "sent",
        }

    def send_email(self, *, to: str, subject: str, body_text: str) -> dict[str, Any]:
        service = self._service()
        payload = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": self._encoded_message(to=to, subject=subject, body_text=body_text)})
            .execute()
        )
        return {
            "message_id": str(payload.get("id", "")),
            "thread_id": str(payload.get("threadId", "")) or None,
            "to": to,
            "subject": subject,
            "source": "gmail",
            "status": "sent",
        }

    def archive_message(self, *, message_id: str) -> dict[str, Any]:
        service = self._service()
        payload = (
            service.users()
            .messages()
            .modify(userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]})
            .execute()
        )
        return {
            "message_id": str(payload.get("id", message_id)),
            "thread_id": str(payload.get("threadId", "")) or None,
            "labels": list(payload.get("labelIds", [])),
            "source": "gmail",
            "status": "archived",
        }

    def trash_message(self, *, message_id: str) -> dict[str, Any]:
        service = self._service()
        payload = service.users().messages().trash(userId="me", id=message_id).execute()
        return {
            "message_id": str(payload.get("id", message_id)),
            "thread_id": str(payload.get("threadId", "")) or None,
            "source": "gmail",
            "status": "trashed",
        }

    def delete_message(self, *, message_id: str) -> dict[str, Any]:
        service = self._service()
        service.users().messages().delete(userId="me", id=message_id).execute()
        return {"message_id": message_id, "source": "gmail", "status": "deleted"}

    def normalize_message(self, message: dict[str, Any], *, include_body: bool = False) -> NormalizedGmailMessage:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        headers = payload.get("headers") if isinstance(payload.get("headers"), list) else []
        header_map = {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in headers
            if isinstance(item, dict)
        }
        body_text = self._extract_body_text(payload) if include_body else None
        return NormalizedGmailMessage(
            message_id=str(message.get("id", "")),
            thread_id=str(message.get("threadId", "")) or None,
            subject=header_map.get("subject", ""),
            from_=header_map.get("from", ""),
            to=header_map.get("to", ""),
            date=header_map.get("date", ""),
            snippet=str(message.get("snippet", "")),
            labels=tuple(str(item) for item in message.get("labelIds", []) if item),
            body_text=body_text,
        )

    def _service(self):
        self._ensure_google_deps()
        readiness = self.readiness()
        if not readiness.live:
            raise RuntimeError(self.setup_needed_message())
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=self._credentials())

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
            raise RuntimeError("Gmail token is invalid; rerun the local OAuth flow.")
        return credentials

    def _encoded_message(self, *, to: str, subject: str, body_text: str) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body_text)
        return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    def _extract_body_text(self, payload: dict[str, Any]) -> str | None:
        mime_type = str(payload.get("mimeType", ""))
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        data = body.get("data")
        if data and mime_type in {"text/plain", ""}:
            return _decode_base64url(str(data))
        for part in payload.get("parts", []) if isinstance(payload.get("parts"), list) else []:
            if not isinstance(part, dict):
                continue
            text = self._extract_body_text(part)
            if text:
                return text
        return None

    def _credentials_path(self) -> Path:
        return _resolve_repo_relative_path(self.settings.gmail_credentials_path)

    def _token_path(self) -> Path:
        return _resolve_repo_relative_path(self.settings.gmail_token_path)

    def _scopes(self) -> list[str]:
        raw = self.settings.gmail_scopes or "https://mail.google.com/"
        return [item.strip() for item in raw.replace(",", " ").split() if item.strip()]

    def _ensure_google_deps(self) -> None:
        if not _google_deps_available():
            raise RuntimeError(
                "Gmail dependencies are missing: google-api-python-client, google-auth-httplib2, google-auth-oauthlib."
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


def _decode_base64url(value: str) -> str | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return None
