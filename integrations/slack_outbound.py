"""Outbound Slack delivery adapter for proactive assistant messages."""

from __future__ import annotations

from typing import Any

from app.config import Settings, settings
from core.logging import get_logger
from integrations.messaging.contracts import MessagingAdapter, MessagingRequest, MessagingResult

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError as exc:  # pragma: no cover - dependency is exercised in runtime environments
    WebClient = Any  # type: ignore[assignment]
    SlackApiError = Exception  # type: ignore[assignment]
    _SLACK_SDK_IMPORT_ERROR: Exception | None = exc
else:
    _SLACK_SDK_IMPORT_ERROR = None


class SlackOutboundAdapter(MessagingAdapter):
    """Dedicated adapter for posting outbound messages back into Slack."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        client: WebClient | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.logger = get_logger(__name__)
        self._client = client

    def is_configured(self) -> bool:
        return bool(self.settings.slack_enabled and self.settings.slack_bot_token)

    def readiness_blockers(self) -> list[str]:
        blockers: list[str] = []
        if not self.settings.slack_enabled:
            blockers.append("Slack outbound delivery is disabled in this runtime.")
        if not self.settings.slack_bot_token:
            blockers.append("SLACK_BOT_TOKEN is required for outbound Slack delivery.")
        if _SLACK_SDK_IMPORT_ERROR is not None and self._client is None:
            blockers.append("slack-sdk is not installed, so outbound Slack delivery cannot start.")
        return blockers

    def send(self, request: MessagingRequest) -> MessagingResult:
        channel = self._resolve_channel(request)
        if not channel:
            return MessagingResult(
                success=False,
                summary="Slack outbound delivery could not determine where to send the message.",
                blockers=["A Slack channel id is required for outbound delivery."],
                metadata={"channel": "slack"},
            )
        return self.send_channel_message(channel=channel, message=request.message)

    def send_channel_message(self, *, channel: str, message: str) -> MessagingResult:
        blockers = self.readiness_blockers()
        if blockers:
            return MessagingResult(
                success=False,
                summary="Slack outbound delivery is not available in this runtime.",
                blockers=blockers,
                metadata={"channel": "slack", "target_channel": channel},
            )

        try:
            response = self._get_client().chat_postMessage(channel=channel, text=message)
        except SlackApiError as exc:
            error_code = getattr(exc.response, "get", lambda *_args, **_kwargs: None)("error")
            error_text = str(error_code or exc)
            self.logger.warning("Slack outbound delivery failed: %s", error_text)
            return MessagingResult(
                success=False,
                summary="Slack outbound delivery failed.",
                blockers=[f"Slack API error: {error_text}"],
                metadata={"channel": "slack", "target_channel": channel},
            )
        except Exception as exc:
            self.logger.warning("Slack outbound delivery failed: %s", exc)
            return MessagingResult(
                success=False,
                summary="Slack outbound delivery failed.",
                blockers=[str(exc)],
                metadata={"channel": "slack", "target_channel": channel},
            )

        delivery_id = str(response.get("ts") or "")
        return MessagingResult(
            success=bool(response.get("ok", True)),
            summary="Slack outbound delivery succeeded.",
            delivery_id=delivery_id or None,
            metadata={
                "channel": "slack",
                "target_channel": str(response.get("channel") or channel),
                "timestamp": delivery_id,
            },
        )

    def send_dm(
        self,
        *,
        message: str,
        channel_id: str | None = None,
        user_id: str | None = None,
    ) -> MessagingResult:
        if channel_id:
            return self.send_channel_message(channel=channel_id, message=message)

        blockers = self.readiness_blockers()
        if blockers:
            return MessagingResult(
                success=False,
                summary="Slack outbound delivery is not available in this runtime.",
                blockers=blockers,
                metadata={"channel": "slack", "target_user": user_id or ""},
            )

        if not user_id:
            return MessagingResult(
                success=False,
                summary="Slack DM delivery could not determine who to message.",
                blockers=["A Slack user id or DM channel id is required for outbound DM delivery."],
                metadata={"channel": "slack"},
            )

        try:
            open_result = self._get_client().conversations_open(users=user_id)
            channel = str(open_result.get("channel", {}).get("id") or "")
            if not channel:
                return MessagingResult(
                    success=False,
                    summary="Slack DM delivery could not open a direct-message channel.",
                    blockers=["Slack did not return a DM channel id for the requested user."],
                    metadata={"channel": "slack", "target_user": user_id},
            )
            return self.send_channel_message(channel=channel, message=message)
        except SlackApiError as exc:
            error_code = getattr(exc.response, "get", lambda *_args, **_kwargs: None)("error")
            error_text = str(error_code or exc)
            self.logger.warning("Slack DM delivery failed: %s", error_text)
            return MessagingResult(
                success=False,
                summary="Slack DM delivery failed.",
                blockers=[f"Slack API error: {error_text}"],
                metadata={"channel": "slack", "target_user": user_id},
            )
        except Exception as exc:
            self.logger.warning("Slack DM delivery failed: %s", exc)
            return MessagingResult(
                success=False,
                summary="Slack DM delivery failed.",
                blockers=[str(exc)],
                metadata={"channel": "slack", "target_user": user_id},
            )

    def _get_client(self) -> WebClient:
        if self._client is not None:
            return self._client
        if _SLACK_SDK_IMPORT_ERROR is not None:
            raise RuntimeError(
                "slack-sdk is not installed. Add project dependencies before using outbound Slack delivery."
            ) from _SLACK_SDK_IMPORT_ERROR
        self._client = WebClient(token=self.settings.slack_bot_token)
        return self._client

    def _resolve_channel(self, request: MessagingRequest) -> str | None:
        metadata = request.metadata
        candidate = metadata.get("channel_id") or request.recipient
        normalized = " ".join(str(candidate or "").split())
        return normalized or None
