"""Thin Slack Socket Mode transport for the existing supervisor backend."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from collections import OrderedDict
import re
from typing import Any

from app.config import Settings, settings
from core.interaction_context import InteractionContext, bind_interaction_context
from core.logging import get_logger
from core.models import ChatResponse, RequestMode
from core.supervisor import supervisor
from core.transport import OperatorMessage, handle_operator_message, normalize_operator_message_text

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError as exc:  # pragma: no cover - exercised only when dependency is absent
    App = Any  # type: ignore[assignment]
    SocketModeHandler = Any  # type: ignore[assignment]
    _SLACK_IMPORT_ERROR: Exception | None = exc
else:
    _SLACK_IMPORT_ERROR = None


def is_direct_message_event(event: Mapping[str, Any]) -> bool:
    """Return True when the event is a user-authored direct message."""
    text = str(event.get("text") or "").strip()
    return (
        event.get("type") == "message"
        and event.get("channel_type") == "im"
        and not event.get("bot_id")
        and not event.get("subtype")
        and bool(text)
    )


def format_chat_response_for_slack(response: ChatResponse) -> str:
    """Convert the backend chat response into Slack text."""
    return _clean_slack_text(response.response)


def _clean_slack_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return normalized

    lines = normalized.split("\n")
    cleaned_lines: list[str] = []
    blank_streak = 0
    in_code_block = False
    for line in lines:
        stripped = line.rstrip()
        if stripped.strip().startswith("```"):
            in_code_block = not in_code_block
            cleaned_lines.append(stripped.strip())
            blank_streak = 0
            continue
        if in_code_block:
            cleaned_lines.append(stripped)
            continue
        compact = re.sub(r"[ \t]+", " ", stripped).strip()
        if not compact:
            blank_streak += 1
            if blank_streak <= 1:
                cleaned_lines.append("")
            continue
        blank_streak = 0
        cleaned_lines.append(compact)

    return "\n".join(cleaned_lines).strip()


class SlackOperatorBridge:
    """Small adapter from Slack text messages to the existing supervisor."""

    def __init__(
        self,
        *,
        backend_handler: Callable[[str], ChatResponse] | None = None,
        mode_decider: Callable[[str], RequestMode] | None = None,
        progress_decider: Callable[[str], bool] | None = None,
        logger_name: str = __name__,
    ) -> None:
        self.backend_handler = backend_handler or supervisor.handle_user_goal
        self.mode_decider = mode_decider or self._default_mode_decider
        self.progress_decider = progress_decider or self._default_progress_decider
        self.logger = get_logger(logger_name)

    def handle_user_message(
        self,
        message_text: str,
        *,
        channel_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Forward a user DM into the operator backend and format the reply."""
        normalized_text = normalize_operator_message_text(message_text)
        self.logger.info(
            "SLACK_MESSAGE_NORMALIZED raw_text=%r normalized_text=%r",
            message_text,
            normalized_text,
        )
        started_at = time.perf_counter()
        try:
            response = handle_operator_message(
                OperatorMessage(
                    message=message_text,
                    transport="slack",
                    channel_id=channel_id,
                    user_id=user_id,
                ),
                backend_handler=self.backend_handler,
            )
            formatted = format_chat_response_for_slack(response)
            self.logger.info(
                "SLACK_BRIDGE_COMPLETED planner_mode=%s request_mode=%s latency_ms=%s",
                response.planner_mode,
                response.request_mode.value,
                int((time.perf_counter() - started_at) * 1000),
            )
            return formatted
        except Exception:
            self.logger.exception("Slack bridge failed while handling incoming message")
            return (
                "I hit an internal error while processing that request. "
                "Please try again in a moment."
            )

    def should_send_progress(self, message_text: str) -> bool:
        normalized_text = normalize_operator_message_text(message_text)
        try:
            return self.progress_decider(normalized_text)
        except Exception:
            self.logger.exception("Slack bridge failed while classifying message mode")
            return False

    @staticmethod
    def _default_mode_decider(message_text: str) -> RequestMode:
        return supervisor.assistant_layer.decide_without_llm(message_text).mode

    @staticmethod
    def _default_progress_decider(message_text: str) -> bool:
        return supervisor.should_send_progress(message_text)


class SlackClient:
    """Socket Mode Slack app that keeps Slack as a thin transport layer."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        bridge: SlackOperatorBridge | None = None,
        background_dispatcher: Callable[[Callable[[], None]], None] | None = None,
        dedupe_window_seconds: float = 120.0,
    ) -> None:
        self.settings = runtime_settings or settings
        self.bridge = bridge or SlackOperatorBridge()
        self.logger = get_logger(__name__)
        self.bot_token = self.settings.slack_bot_token
        self.app_token = self.settings.slack_app_token
        self.background_dispatcher = background_dispatcher or self._dispatch_in_background
        self.dedupe_window_seconds = dedupe_window_seconds
        self._recent_event_ids: OrderedDict[str, float] = OrderedDict()
        self._dedupe_lock = threading.Lock()

    @staticmethod
    def _dispatch_in_background(task: Callable[[], None]) -> None:
        thread = threading.Thread(target=task, daemon=True)
        thread.start()

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.app_token)

    def build_app(self) -> App:
        """Create the Slack Bolt app and register the DM listener."""
        if _SLACK_IMPORT_ERROR is not None:
            raise RuntimeError(
                "slack-bolt is not installed. Add project dependencies before running the Slack app."
            ) from _SLACK_IMPORT_ERROR

        self.settings.require_slack_socket_mode()
        app = App(token=self.bot_token)

        @app.event("message")
        def handle_message_events(
            event: dict[str, Any],
            say: Callable[..., Any],
            client: Any | None = None,
        ) -> None:
            self._handle_message_event(event, say, client=client)

        return app

    def _handle_message_event(
        self,
        event: Mapping[str, Any],
        say: Callable[..., Any],
        *,
        client: Any | None = None,
    ) -> None:
        """Ignore non-DM traffic and delegate supported messages to the backend bridge."""
        if not is_direct_message_event(event):
            return
        if self._is_duplicate_event(event):
            self.logger.info("Skipping duplicate Slack event %s", self._event_id(event))
            return

        text = str(event.get("text") or "").strip()
        interaction_context = InteractionContext(
            source="slack",
            channel_id=str(event.get("channel") or "") or None,
            user_id=str(event.get("user") or "") or None,
        )
        should_send_progress = self.bridge.should_send_progress(text)
        self.logger.info("PROGRESS_ACK_SENT=%s", should_send_progress)
        deliver = lambda message: self._deliver_message(
            message,
            channel_id=interaction_context.channel_id,
            say=say,
            client=client,
        )
        if should_send_progress:
            deliver("On it.")
        self.background_dispatcher(lambda: self._process_message(text, deliver, interaction_context))

    def _process_message(
        self,
        text: str,
        deliver: Callable[[str], Any],
        interaction_context: InteractionContext,
    ) -> None:
        try:
            with bind_interaction_context(interaction_context):
                response_text = self.bridge.handle_user_message(
                    text,
                    channel_id=interaction_context.channel_id,
                    user_id=interaction_context.user_id,
                )
            deliver(response_text)
        except Exception:
            self.logger.exception("Slack background delivery failed")
            try:
                deliver(
                    "I hit an internal error while processing that request. "
                    "Please try again in a moment."
                )
            except Exception:
                self.logger.exception("Slack failure notification could not be delivered")

    def _deliver_message(
        self,
        text: str,
        *,
        channel_id: str | None,
        say: Callable[..., Any],
        client: Any | None = None,
    ) -> Any:
        """Send a Slack reply with provider evidence when the WebClient is available."""
        message = _clean_slack_text(text)
        if not message:
            message = (
                "I finished processing that, but the reply came back empty. "
                "Please retry so I can give you a useful result."
            )

        if client is not None and channel_id:
            response = client.chat_postMessage(channel=channel_id, text=message)
            ok = bool(getattr(response, "get", lambda *_args, **_kwargs: True)("ok", True))
            if not ok:
                error = getattr(response, "get", lambda *_args, **_kwargs: None)("error")
                raise RuntimeError(f"Slack chat_postMessage failed: {error or 'unknown_error'}")
            self.logger.info(
                "SLACK_REPLY_DELIVERED method=chat_postMessage channel=%s ts=%s",
                channel_id,
                getattr(response, "get", lambda *_args, **_kwargs: None)("ts"),
            )
            return response

        result = say(message)
        self.logger.info("SLACK_REPLY_DELIVERED method=say channel=%s", channel_id)
        return result

    def _event_id(self, event: Mapping[str, Any]) -> str | None:
        for key in ("client_msg_id", "event_ts", "ts"):
            value = event.get(key)
            if value:
                return str(value)
        return None

    def _is_duplicate_event(self, event: Mapping[str, Any]) -> bool:
        event_id = self._event_id(event)
        if not event_id:
            return False

        now = time.monotonic()
        with self._dedupe_lock:
            while self._recent_event_ids:
                oldest_event_id, seen_at = next(iter(self._recent_event_ids.items()))
                if now - seen_at <= self.dedupe_window_seconds:
                    break
                self._recent_event_ids.pop(oldest_event_id)

            if event_id in self._recent_event_ids:
                return True

            self._recent_event_ids[event_id] = now
            return False

    def start(self) -> None:
        """Run the Slack app in Socket Mode."""
        self.settings.require_slack_socket_mode()
        self.logger.info("Starting Slack Socket Mode app for %s", self.settings.app_name)
        handler = SocketModeHandler(self.build_app(), self.app_token)
        handler.start()
