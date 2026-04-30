"""Bounded Slack outbound messaging tool."""

from __future__ import annotations

from app.config import Settings, settings
from core.logging import get_logger
from core.models import ToolInvocation
from integrations.slack_outbound import SlackOutboundAdapter
from tools.base_tool import BaseTool


class SlackMessagingTool(BaseTool):
    """Wrap outbound Slack posting behind the shared tool contract."""

    name = "slack_messaging_tool"

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        outbound_adapter: SlackOutboundAdapter | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.logger = get_logger(__name__)
        self.outbound_adapter = outbound_adapter or SlackOutboundAdapter(
            runtime_settings=self.settings
        )

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name and invocation.action in {
            "send_channel_message",
            "send_dm",
        }

    def execute(self, invocation: ToolInvocation) -> dict:
        message_text = " ".join((invocation.parameters.get("message_text") or "").split())
        action = invocation.action
        self.logger.info(
            "SLACK_MESSAGE_SEND_START action=%s target=%r user_id=%r channel_id=%r",
            action,
            invocation.parameters.get("target"),
            invocation.parameters.get("user_id"),
            invocation.parameters.get("channel_id"),
        )

        if not message_text:
            return self._blocked_response(
                summary="Slack outbound delivery could not start.",
                error="Slack message text is required.",
                payload={"status": "blocked", "action": action},
            )

        if action == "send_channel_message":
            channel = self._resolve_channel_target(invocation)
            if not channel:
                return self._blocked_response(
                    summary="Slack channel delivery could not determine where to send the message.",
                    error="Missing Slack channel target. Provide a channel id or channel name like #general.",
                    payload={"status": "blocked", "action": action, "message_text": message_text},
                )
            result = self.outbound_adapter.send_channel_message(
                channel=channel,
                message=message_text,
            )
            response = self._result_to_output(
                result,
                action=action,
                target=channel,
                message_text=message_text,
            )
            self.logger.info(
                "SLACK_MESSAGE_SEND_END action=%s success=%s target=%s delivery_id=%r",
                action,
                result.success,
                channel,
                result.delivery_id,
            )
            return response

        channel_id = self._normalize_optional_text(invocation.parameters.get("channel_id"))
        user_id = self._resolve_user_target(invocation)
        if not channel_id and not user_id:
            return self._blocked_response(
                summary="Slack DM delivery could not determine who to message.",
                error="Missing Slack DM target. Provide a DM channel id or Slack user id.",
                payload={"status": "blocked", "action": action, "message_text": message_text},
            )
        result = self.outbound_adapter.send_dm(
            message=message_text,
            channel_id=channel_id,
            user_id=user_id,
        )
        resolved_target = channel_id or user_id or ""
        response = self._result_to_output(
            result,
            action=action,
            target=resolved_target,
            message_text=message_text,
        )
        self.logger.info(
            "SLACK_MESSAGE_SEND_END action=%s success=%s target=%s delivery_id=%r",
            action,
            result.success,
            resolved_target,
            result.delivery_id,
        )
        return response

    def _resolve_channel_target(self, invocation: ToolInvocation) -> str | None:
        for key in ("channel_id", "channel", "target"):
            value = self._normalize_optional_text(invocation.parameters.get(key))
            if value:
                return value
        return None

    def _resolve_user_target(self, invocation: ToolInvocation) -> str | None:
        user_id = self._normalize_optional_text(invocation.parameters.get("user_id"))
        if user_id:
            return user_id
        target = self._normalize_optional_text(invocation.parameters.get("target"))
        if target and target.upper().startswith("U"):
            return target
        return None

    def _result_to_output(
        self,
        result,
        *,
        action: str,
        target: str,
        message_text: str,
    ) -> dict:
        payload = {
            "status": "completed" if result.success else "blocked",
            "action": action,
            "target": target,
            "message_text": message_text,
            "timestamp": result.metadata.get("timestamp", ""),
            "response_id": result.delivery_id or "",
        }
        if "target_channel" in result.metadata:
            payload["channel"] = result.metadata["target_channel"]
        if "target_user" in result.metadata:
            payload["user_id"] = result.metadata["target_user"]
        return {
            "success": result.success,
            "summary": result.summary,
            "error": result.blockers[0] if result.blockers else None,
            "payload": payload,
        }

    def _blocked_response(self, *, summary: str, error: str, payload: dict[str, str]) -> dict:
        self.logger.info(
            "SLACK_MESSAGE_SEND_END action=%s success=%s target=%s delivery_id=%r",
            payload.get("action"),
            False,
            payload.get("target", ""),
            None,
        )
        return {
            "success": False,
            "summary": summary,
            "error": error,
            "payload": payload,
        }

    def _normalize_optional_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text or None
