"""Focused coverage for the thin Slack interface layer."""

from __future__ import annotations

import unittest
from collections.abc import Callable

from core.models import (
    AgentExecutionStatus,
    AgentResult,
    ChatResponse,
    FileEvidence,
    RequestMode,
    TaskOutcome,
    TaskStatus,
    ToolEvidence,
)
from integrations.slack_client import (
    SlackClient,
    SlackOperatorBridge,
    format_chat_response_for_slack,
    is_direct_message_event,
)


class SlackFormattingTests(unittest.TestCase):
    """Keep Slack response formatting compact and useful."""

    def test_formats_chat_response_as_assistant_reply(self) -> None:
        response = ChatResponse(
            task_id="task-123",
            status=TaskStatus.COMPLETED,
            planner_mode="deterministic",
            request_mode=RequestMode.ACT,
            response="Done. I created `hello.txt` and verified it.",
            outcome=TaskOutcome(completed=3, total_subtasks=3),
            subtasks=[],
            results=[
                AgentResult(
                    subtask_id="subtask-1",
                    agent="coding_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Created workspace file at hello.txt.",
                    tool_name="file_tool",
                    evidence=[
                        FileEvidence(
                            operation="write",
                            file_path="C:\\workspace\\hello.txt",
                            content_preview="Hello from Project Sovereign!",
                        )
                    ],
                ),
                AgentResult(
                    subtask_id="subtask-2",
                    agent="reviewer_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Reviewed workspace file result successfully.",
                    tool_name="file_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="runtime_tool",
                            summary="Verified the output preview was captured.",
                            payload={"command": "python --version"},
                        )
                    ],
                ),
            ],
        )

        formatted = format_chat_response_for_slack(response)

        self.assertEqual(formatted, "Done. I created `hello.txt` and verified it.")

    def test_collapses_extra_whitespace_for_slack(self) -> None:
        response = ChatResponse(
            task_id="task-456",
            status=TaskStatus.COMPLETED,
            planner_mode="conversation",
            request_mode=RequestMode.ANSWER,
            response="Hi there.  \n\n\nI can help with that.   ",
            outcome=TaskOutcome(total_subtasks=0),
            subtasks=[],
            results=[],
        )

        formatted = format_chat_response_for_slack(response)

        self.assertEqual(formatted, "Hi there.\n\nI can help with that.")


class SlackBridgeTests(unittest.TestCase):
    """Verify transport behavior stays thin and safe."""

    def test_bridge_returns_safe_failure_message(self) -> None:
        bridge = SlackOperatorBridge(backend_handler=self._raise_backend_error)

        message = bridge.handle_user_message("Please run this request")

        self.assertIn("internal error", message)
        self.assertNotIn("Traceback", message)

    def test_bridge_normalizes_raw_slack_link_markup_before_backend(self) -> None:
        captured: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=lambda message: self._capture_and_build_response(message, captured)
        )

        bridge.handle_user_message("open <https://example.com> and summarize it")

        self.assertEqual(captured, ["open https://example.com and summarize it"])

    def test_bridge_normalizes_labeled_slack_link_markup_before_backend(self) -> None:
        captured: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=lambda message: self._capture_and_build_response(message, captured)
        )

        bridge.handle_user_message("summarize <https://example.com|example.com>")

        self.assertEqual(captured, ["summarize https://example.com"])

    def test_bridge_logs_raw_and_normalized_text(self) -> None:
        bridge = SlackOperatorBridge(backend_handler=self._build_response)

        with self.assertLogs("integrations.slack_client", level="INFO") as logs:
            bridge.handle_user_message("open <https://example.com> and summarize it")

        combined = "\n".join(logs.output)
        self.assertIn("SLACK_MESSAGE_NORMALIZED", combined)
        self.assertIn("raw_text='open <https://example.com> and summarize it'", combined)
        self.assertIn("normalized_text='open https://example.com and summarize it'", combined)

    def test_client_handles_direct_messages_only(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(backend_handler=self._build_response)
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "text": "Create hello.txt"},
            lambda text: sent_messages.append(text),
        )
        client._handle_message_event(
            {"type": "message", "channel_type": "channel", "text": "Ignore this"},
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(sent_messages, ["Completed the requested task."])

    def test_client_skips_progress_message_for_answer_mode(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=self._build_answer_response,
            mode_decider=lambda _: RequestMode.ANSWER,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "text": "hi"},
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(sent_messages, ["Hi there."])

    def test_client_defaults_to_no_progress_when_mode_decider_fails(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=self._build_answer_response,
            mode_decider=lambda _: self._raise_mode_error(),
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "text": "hi"},
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(sent_messages, ["Hi there."])

    def test_client_sends_ack_before_background_response(self) -> None:
        sent_messages: list[str] = []
        scheduled_tasks: list[Callable[[], None]] = []
        bridge = SlackOperatorBridge(backend_handler=self._build_response)
        client = SlackClient(bridge=bridge, background_dispatcher=scheduled_tasks.append)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "text": "Create hello.txt"},
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(sent_messages, [])
        self.assertEqual(len(scheduled_tasks), 1)

        scheduled_tasks[0]()

        self.assertEqual(sent_messages, ["Completed the requested task."])

    def test_client_skips_progress_message_for_fast_action(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=self._build_response,
            progress_decider=lambda _: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {
                "type": "message",
                "channel_type": "im",
                "text": "Remind me in 2 minutes to drink water",
            },
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(sent_messages, ["Completed the requested task."])

    def test_client_sends_progress_message_for_execution_work(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=self._build_response,
            progress_decider=lambda _: True,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        with self.assertLogs("integrations.slack_client", level="INFO") as logs:
            client._handle_message_event(
                {"type": "message", "channel_type": "im", "text": "Build the reminder system"},
                lambda text: sent_messages.append(text),
            )

        self.assertEqual(sent_messages, ["On it.", "Completed the requested task."])
        self.assertIn("PROGRESS_ACK_SENT=True", "\n".join(logs.output))

    def test_client_sends_safe_failure_message_from_background_path(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(backend_handler=self._raise_backend_error)
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "text": "Create hello.txt"},
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(len(sent_messages), 1)
        self.assertIn("internal error", sent_messages[0])
        self.assertNotIn("Traceback", sent_messages[0])

    def test_client_posts_simple_answer_with_slack_web_client(self) -> None:
        sent_messages: list[str] = []
        web_client = FakeSlackWebClient()
        bridge = SlackOperatorBridge(
            backend_handler=self._build_answer_response,
            progress_decider=lambda _: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "channel": "D123", "text": "hi"},
            lambda text: sent_messages.append(text),
            client=web_client,
        )

        self.assertEqual(sent_messages, [])
        self.assertEqual(web_client.posts, [{"channel": "D123", "text": "Hi there."}])

    def test_client_posts_fast_action_response_with_slack_web_client(self) -> None:
        web_client = FakeSlackWebClient()
        bridge = SlackOperatorBridge(
            backend_handler=self._build_response,
            progress_decider=lambda _: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "channel": "D123", "text": "remind me to study at 7"},
            lambda text: self.fail(f"say fallback should not be used: {text}"),
            client=web_client,
        )

        self.assertEqual(web_client.posts, [{"channel": "D123", "text": "Completed the requested task."}])

    def test_client_posts_blocked_action_response_with_slack_web_client(self) -> None:
        web_client = FakeSlackWebClient()
        bridge = SlackOperatorBridge(
            backend_handler=self._build_blocked_response,
            progress_decider=lambda _: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "channel": "D123", "text": "remind me sometime"},
            lambda text: self.fail(f"say fallback should not be used: {text}"),
            client=web_client,
        )

        self.assertEqual(web_client.posts, [{"channel": "D123", "text": "I need a clearer time before I can schedule that."}])

    def test_client_posts_execution_progress_and_final_with_slack_web_client(self) -> None:
        web_client = FakeSlackWebClient()
        bridge = SlackOperatorBridge(
            backend_handler=self._build_execution_response,
            progress_decider=lambda _: True,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "channel": "D123", "text": "build the app"},
            lambda text: self.fail(f"say fallback should not be used: {text}"),
            client=web_client,
        )

        self.assertEqual(
            web_client.posts,
            [
                {"channel": "D123", "text": "On it."},
                {"channel": "D123", "text": "Built the requested artifact and verified it."},
            ],
        )

    def test_client_falls_back_to_say_when_web_client_is_unavailable(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=self._build_answer_response,
            progress_decider=lambda _: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        client._handle_message_event(
            {"type": "message", "channel_type": "im", "channel": "D123", "text": "hi"},
            lambda text: sent_messages.append(text),
        )

        self.assertEqual(sent_messages, ["Hi there."])

    def test_client_ignores_duplicate_direct_message_events(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(backend_handler=self._build_response)
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)
        event = {
            "type": "message",
            "channel_type": "im",
            "text": "Create hello.txt",
            "client_msg_id": "event-123",
        }

        client._handle_message_event(event, lambda text: sent_messages.append(text))
        client._handle_message_event(event, lambda text: sent_messages.append(text))

        self.assertEqual(sent_messages, ["Completed the requested task."])

    def test_client_logs_progress_ack_state(self) -> None:
        sent_messages: list[str] = []
        bridge = SlackOperatorBridge(
            backend_handler=self._build_answer_response,
            progress_decider=lambda _: False,
        )
        client = SlackClient(bridge=bridge, background_dispatcher=self._inline_dispatcher)

        with self.assertLogs("integrations.slack_client", level="INFO") as logs:
            client._handle_message_event(
                {"type": "message", "channel_type": "im", "text": "hi"},
                lambda text: sent_messages.append(text),
            )

        self.assertEqual(sent_messages, ["Hi there."])
        self.assertIn("PROGRESS_ACK_SENT=False", "\n".join(logs.output))

    def test_direct_message_filter_rejects_bot_and_empty_messages(self) -> None:
        self.assertTrue(
            is_direct_message_event({"type": "message", "channel_type": "im", "text": "Hello"})
        )
        self.assertFalse(
            is_direct_message_event(
                {"type": "message", "channel_type": "im", "text": "Hello", "bot_id": "B123"}
            )
        )
        self.assertFalse(
            is_direct_message_event({"type": "message", "channel_type": "im", "text": "   "})
        )

    @staticmethod
    def _raise_backend_error(_: str) -> ChatResponse:
        raise RuntimeError("boom")

    @staticmethod
    def _build_response(_: str) -> ChatResponse:
        return ChatResponse(
            task_id="task-1",
            status=TaskStatus.COMPLETED,
            planner_mode="deterministic",
            request_mode=RequestMode.ACT,
            response="Completed the requested task.",
            outcome=TaskOutcome(completed=1, total_subtasks=1),
            subtasks=[],
            results=[
                AgentResult(
                    subtask_id="subtask-1",
                    agent="coding_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Executed runtime command successfully.",
                    tool_name="runtime_tool",
                    evidence=[
                        ToolEvidence(
                            tool_name="runtime_tool",
                            summary="Executed runtime command successfully.",
                            payload={"command": "python --version"},
                        )
                    ],
                )
            ],
        )

    @staticmethod
    def _build_answer_response(_: str) -> ChatResponse:
        return ChatResponse(
            task_id="answer-1",
            status=TaskStatus.COMPLETED,
            planner_mode="conversation",
            request_mode=RequestMode.ANSWER,
            response="Hi there.",
            outcome=TaskOutcome(total_subtasks=0),
            subtasks=[],
            results=[],
        )

    @staticmethod
    def _build_blocked_response(_: str) -> ChatResponse:
        return ChatResponse(
            task_id="blocked-1",
            status=TaskStatus.BLOCKED,
            planner_mode="fast_action",
            request_mode=RequestMode.ACT,
            response="I need a clearer time before I can schedule that.",
            outcome=TaskOutcome(total_subtasks=1),
            subtasks=[],
            results=[
                AgentResult(
                    subtask_id="subtask-blocked",
                    agent="scheduling_agent",
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Reminder needs a clearer time.",
                    tool_name="reminder_scheduler",
                    blockers=["Missing reminder time."],
                )
            ],
        )

    @staticmethod
    def _build_execution_response(_: str) -> ChatResponse:
        return ChatResponse(
            task_id="execute-1",
            status=TaskStatus.COMPLETED,
            planner_mode="deterministic",
            request_mode=RequestMode.EXECUTE,
            response="Built the requested artifact and verified it.",
            outcome=TaskOutcome(completed=2, total_subtasks=2),
            subtasks=[],
            results=[
                AgentResult(
                    subtask_id="subtask-execute",
                    agent="coding_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Created and verified the artifact.",
                    tool_name="runtime_tool",
                )
            ],
        )

    @staticmethod
    def _capture_and_build_response(message: str, captured: list[str]) -> ChatResponse:
        captured.append(message)
        return SlackBridgeTests._build_response(message)

    @staticmethod
    def _inline_dispatcher(task: Callable[[], None]) -> None:
        task()

    @staticmethod
    def _raise_mode_error() -> RequestMode:
        raise RuntimeError("mode error")


class FakeSlackWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []

    def chat_postMessage(self, *, channel: str, text: str) -> dict[str, str | bool]:
        self.posts.append({"channel": channel, "text": text})
        return {"ok": True, "channel": channel, "ts": f"{len(self.posts)}.000100"}


if __name__ == "__main__":
    unittest.main()
