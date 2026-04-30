"""Focused coverage for the operator console read-only API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence
from core.state import task_state_store
from memory.types import MemoryFact, MemorySnapshot


class OperatorConsoleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        task_state_store._tasks.clear()
        self.client = TestClient(app)

    def test_agent_and_browser_status_expose_safe_evidence_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            screenshot = Path(temp_dir) / ".sovereign" / "browser_artifacts" / "example-home.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"fake")
            task = Task(
                goal="Open https://example.com and summarize it",
                title="Browser check",
                description="Browser check",
                subtasks=[
                    SubTask(
                        title="Open page",
                        description="Open page",
                        objective="Open https://example.com",
                        assigned_agent="browser_agent",
                    )
                ],
            )
            task.results.append(
                AgentResult(
                    subtask_id=task.subtasks[0].id,
                    agent="browser_agent",
                    status=AgentExecutionStatus.COMPLETED,
                    summary="Opened Example Domain and captured page evidence.",
                    tool_name="browser_tool",
                    artifacts=[f"browser:screenshot:{screenshot}"],
                    evidence=[
                        ToolEvidence(
                            tool_name="browser_tool",
                            summary="Opened Example Domain.",
                            payload={
                                "requested_url": "https://example.com",
                                "final_url": "https://example.com",
                                "title": "Example Domain",
                                "headings": ["Example Domain"],
                                "summary_text": "Example Domain is visible.",
                                "screenshot_path": str(screenshot),
                                "backend": "playwright",
                                "headless": True,
                                "local_visible": False,
                            },
                        )
                    ],
                )
            )
            task_state_store.add_task(task)

            with patch.object(settings, "workspace_root", temp_dir):
                browser_response = self.client.get("/browser/status")
                agents_response = self.client.get("/agents/status")

        self.assertEqual(browser_response.status_code, 200)
        payload = browser_response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["evidence"]["title"], "Example Domain")
        self.assertEqual(payload["evidence"]["screenshot"]["name"], "example-home.png")
        self.assertFalse(payload["live_stream"]["available"])

        self.assertEqual(agents_response.status_code, 200)
        browser_agent = next(agent for agent in agents_response.json()["agents"] if agent["id"] == "browser_agent")
        self.assertEqual(browser_agent["status"], "completed")
        self.assertGreaterEqual(browser_agent["evidence_count"], 1)

    def test_memory_summary_filters_secrets_and_contact_like_values(self) -> None:
        class FakeMemoryStore:
            provider_name = "fake_memory"

            def snapshot(self) -> MemorySnapshot:
                return MemorySnapshot(
                    project_facts=[
                        MemoryFact(
                            layer="project",
                            category="identity",
                            key="product_identity",
                            value="Sovereign is one CEO-style operator.",
                        ),
                        MemoryFact(
                            layer="project",
                            category="secrets",
                            key="openrouter_api_key",
                            value="sk-test-secret-token-that-should-never-leak",
                        ),
                        MemoryFact(
                            layer="project",
                            category="contact",
                            key="owner_email",
                            value="user@example.com",
                        ),
                    ]
                )

        with patch("api.routes.operator_console.memory_store", FakeMemoryStore()):
            response = self.client.get("/memory/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = str(payload)
        self.assertIn("CEO-style operator", serialized)
        self.assertNotIn("sk-test-secret", serialized)
        self.assertNotIn("user@example.com", serialized)
        self.assertFalse(payload["secrets_exposed"])

    def test_life_and_integration_endpoints_return_summary_shapes(self) -> None:
        for path in (
            "/integrations/status",
            "/life/reminders",
            "/life/calendar",
            "/life/tasks",
            "/browser/artifacts",
            "/runs/status",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("mock", response.json(), path)


if __name__ == "__main__":
    unittest.main()
