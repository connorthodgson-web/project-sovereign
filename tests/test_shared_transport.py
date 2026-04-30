"""Coverage for the shared dashboard/Slack operator transport."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from core.interaction_context import get_interaction_context
from core.models import ChatResponse, RequestMode, TaskOutcome, TaskStatus
from core.transport import OperatorMessage, handle_operator_message


class SharedTransportTests(unittest.TestCase):
    def test_transport_normalizes_message_and_binds_context(self) -> None:
        captured: dict[str, object] = {}

        def backend_handler(message: str) -> ChatResponse:
            captured["message"] = message
            captured["context"] = get_interaction_context()
            return self._response("Handled shared transport.")

        response = handle_operator_message(
            OperatorMessage(
                message="summarize <https://example.com|Example>",
                transport="dashboard",
                channel_id="browser-session-1",
                user_id="user-1",
            ),
            backend_handler=backend_handler,
        )

        self.assertEqual(response.response, "Handled shared transport.")
        self.assertEqual(captured["message"], "summarize https://example.com")
        context = captured["context"]
        self.assertIsNotNone(context)
        self.assertEqual(context.source, "dashboard")
        self.assertEqual(context.channel_id, "browser-session-1")
        self.assertEqual(context.user_id, "user-1")

    def test_chat_endpoint_uses_shared_transport_request_shape(self) -> None:
        captured: dict[str, OperatorMessage] = {}

        def fake_handle(message: OperatorMessage) -> ChatResponse:
            captured["message"] = message
            return self._response("Dashboard chat reached the shared operator transport.")

        client = TestClient(app)
        with patch("api.routes.chat.handle_operator_message", side_effect=fake_handle):
            response = client.post(
                "/chat",
                json={
                    "message": "hi",
                    "transport": "dashboard",
                    "channel_id": "dash-session",
                    "user_id": "dashboard-user",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "Dashboard chat reached the shared operator transport.")
        self.assertEqual(captured["message"].transport, "dashboard")
        self.assertEqual(captured["message"].channel_id, "dash-session")
        self.assertEqual(captured["message"].user_id, "dashboard-user")

    def test_chat_endpoint_accepts_future_ios_transport(self) -> None:
        captured: dict[str, OperatorMessage] = {}

        def fake_handle(message: OperatorMessage) -> ChatResponse:
            captured["message"] = message
            return self._response("iOS chat reached the same operator transport.")

        client = TestClient(app)
        with patch("api.routes.chat.handle_operator_message", side_effect=fake_handle):
            response = client.post(
                "/chat",
                json={
                    "message": "mobile hello",
                    "transport": "ios",
                    "channel_id": "ios-device-session",
                    "user_id": "mobile-user",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "iOS chat reached the same operator transport.")
        self.assertEqual(captured["message"].transport, "ios")
        self.assertEqual(captured["message"].channel_id, "ios-device-session")
        self.assertEqual(captured["message"].user_id, "mobile-user")

    @staticmethod
    def _response(text: str) -> ChatResponse:
        return ChatResponse(
            task_id="transport-test",
            status=TaskStatus.COMPLETED,
            planner_mode="transport",
            request_mode=RequestMode.ANSWER,
            response=text,
            outcome=TaskOutcome(total_subtasks=0),
            subtasks=[],
            results=[],
        )


if __name__ == "__main__":
    unittest.main()
