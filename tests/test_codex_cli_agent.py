"""Focused coverage for the managed Codex CLI coding lane."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.codex_cli_agent import CodexCliAgentAdapter
from app.config import Settings, settings
from core.models import (
    AgentDescriptor,
    AgentExecutionStatus,
    AgentProvider,
    ExecutionEscalation,
    SubTask,
    Task,
)
from core.planner import Planner
from core.router import Router
from agents.reviewer_agent import ReviewerAgent
from agents.verifier_agent import VerifierAgent
from core.evaluator import GoalEvaluator
from core.supervisor import Supervisor
from integrations.openrouter_client import OpenRouterClient


class CodexCliAgentTests(unittest.TestCase):
    class NoLlmClient:
        def is_configured(self) -> bool:
            return False

    def _descriptor(self) -> AgentDescriptor:
        return AgentDescriptor(
            agent_id="codex_cli_agent",
            display_name="Codex CLI Agent",
            provider=AgentProvider.CODEX_CLI,
            capabilities=["coding", "managed_coding", "workspace_edits", "code_review"],
            cost_tier="standard",
            risk_level="medium",
            input_schema={"task": "Task", "subtask": "SubTask"},
            output_schema={"result": "AgentResult"},
            evidence_schema={"tool_name": "codex_cli"},
            supports_async=True,
            requires_credentials=False,
            enabled=False,
        )

    def _task(self, goal: str = "Refactor the failing module and add regression coverage.") -> Task:
        return Task(
            goal=goal,
            title=goal,
            description=goal,
            escalation_level=ExecutionEscalation.BOUNDED_TASK_EXECUTION,
        )

    def _subtask(self, objective: str | None = None) -> SubTask:
        objective = objective or "Refactor the failing module and add regression coverage."
        return SubTask(
            title="Execute bounded coding task",
            description="Use Codex for bounded coding work.",
            objective=objective,
            assigned_agent="codex_cli_agent",
        )

    def _settings(
        self,
        *,
        workspace_root: str,
        enabled: bool = True,
        command: str = "codex",
        auto_mode: bool = False,
    ) -> Settings:
        return Settings(
            codex_cli_enabled=enabled,
            codex_cli_command=command,
            codex_cli_workspace_root=workspace_root,
            codex_cli_timeout_seconds=30,
            codex_cli_auto_mode=auto_mode,
            openrouter_api_key=None,
        )

    def _write_fake_codex(
        self,
        directory: Path,
        *,
        body: str,
    ) -> Path:
        script_path = directory / "fake_codex.py"
        script_path.write_text(textwrap.dedent(body), encoding="utf-8")
        return script_path

    def test_codex_cli_agent_reports_blocked_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(workspace_root=temp_dir, enabled=False),
            )

            result = adapter.run(self._task(), self._subtask())

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("not fully configured", result.summary.lower())
        self.assertTrue(any("CODEX_CLI_ENABLED=true" in action for action in result.next_actions))

    def test_codex_cli_agent_reports_blocked_when_command_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=temp_dir,
                    command="definitely-not-a-real-codex-command",
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(any("not available" in blocker.lower() for blocker in result.blockers))

    def test_codex_cli_agent_runs_mocked_command_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                import pathlib
                import sys

                pathlib.Path("module.py").write_text("print('codex ok')\\n", encoding="utf-8")
                print("OUTCOME: completed")
                print("SUMMARY: Codex finished the bounded coding task.")
                print("CHANGED_FILES: module.py")
                print("TESTS_RUN: pytest -q")
                print("BLOCKERS: none")
                print("NEXT_ACTIONS: none")
                sys.stderr.write("mock stderr\\n")
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertEqual(result.tool_name, "codex_cli")
        self.assertTrue(result.evidence)
        payload = result.evidence[0].payload
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn("Codex finished", result.summary)
        self.assertIn("module.py", payload["changed_files"])
        self.assertIn("command_invoked", payload)
        self.assertEqual(payload["completion_evidence_state"], "reviewable")

    def test_codex_cli_agent_captures_stdout_stderr_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                import sys

                print("standard output from fake codex")
                sys.stderr.write("standard error from fake codex\\n")
                print("OUTCOME: blocked")
                print("SUMMARY: Codex hit a blocker.")
                print("CHANGED_FILES: none")
                print("TESTS_RUN: none")
                print("BLOCKERS: missing fixture")
                print("NEXT_ACTIONS: add fixture")
                sys.exit(2)
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        payload = result.evidence[0].payload
        self.assertEqual(payload["exit_code"], 2)
        self.assertIn("standard output", payload["stdout_preview"])
        self.assertIn("standard error", payload["stderr_preview"])

    def test_codex_cli_agent_redacts_secret_like_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                print("OUTCOME: blocked")
                print("SUMMARY: Codex hit a blocker.")
                print("CHANGED_FILES: none")
                print("TESTS_RUN: none")
                print("BLOCKERS: API_KEY=super-secret-value")
                print("NEXT_ACTIONS: none")
                print("token=very-secret-token")
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        payload = result.evidence[0].payload
        self.assertNotIn("super-secret-value", payload["stdout_preview"])
        self.assertNotIn("very-secret-token", payload["stdout_preview"])
        self.assertIn("[REDACTED]", payload["stdout_preview"])

    def test_codex_cli_agent_does_not_complete_incomplete_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                print("OUTCOME: incomplete")
                print("SUMMARY: Codex started but did not finish.")
                print("CHANGED_FILES: partial.py")
                print("TESTS_RUN: none")
                print("BLOCKERS: remaining implementation work")
                print("NEXT_ACTIONS: finish the implementation")
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertIn("remaining implementation work", result.blockers)

    def test_codex_cli_agent_does_not_complete_without_diff_or_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                print("OUTCOME: completed")
                print("SUMMARY: Codex says it finished but changed nothing.")
                print("CHANGED_FILES: none")
                print("TESTS_RUN: none")
                print("BLOCKERS: none")
                print("NEXT_ACTIONS: none")
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        payload = result.evidence[0].payload
        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(payload["completion_evidence_state"], "blocked")
        self.assertTrue(any("changed files" in blocker.lower() for blocker in result.blockers))

    def test_codex_cli_agent_failed_tests_block_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                import pathlib

                pathlib.Path("module.py").write_text("print('changed')\\n", encoding="utf-8")
                print("OUTCOME: completed")
                print("SUMMARY: Codex claims the task is done.")
                print("CHANGED_FILES: module.py")
                print("TESTS_RUN: pytest -q")
                print("BLOCKERS: none")
                print("NEXT_ACTIONS: none")
                print("1 failed, 2 passed")
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        self.assertEqual(result.status, AgentExecutionStatus.BLOCKED)
        self.assertTrue(any("test output" in blocker.lower() for blocker in result.blockers))

    def test_codex_prompt_includes_workspace_safety_and_verification_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            (workspace_root / "AGENTS.md").write_text(
                "# Local Instructions\n\nSovereign owns review and final response.\n",
                encoding="utf-8",
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(workspace_root=str(workspace_root)),
                which=lambda command: command,
            )

            prompt = adapter._build_bounded_prompt(self._task(), self._subtask(), workspace_root)

        self.assertIn(f"Allowed workspace root: {workspace_root}", prompt)
        self.assertIn("Workspace boundary: read and write only within that exact directory tree", prompt)
        self.assertIn("AGENTS.md context", prompt)
        self.assertIn("Sovereign owns review and final response", prompt)
        self.assertIn("Do not print secrets", prompt)
        self.assertIn("TESTS_RUN", prompt)

    @unittest.skipUnless(shutil.which("git"), "git is required for diff summary coverage")
    def test_codex_cli_agent_captures_git_diff_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            tracked_file = workspace_root / "tracked_module.py"
            subprocess.run(["git", "init"], cwd=workspace_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=workspace_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Project Sovereign Tests"], cwd=workspace_root, check=True, capture_output=True, text=True)
            tracked_file.write_text("print('before')\\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked_module.py"], cwd=workspace_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=workspace_root, check=True, capture_output=True, text=True)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                import pathlib

                path = pathlib.Path("tracked_module.py")
                path.write_text("print('after')\\n", encoding="utf-8")
                print("OUTCOME: completed")
                print("SUMMARY: Codex updated the tracked module.")
                print("CHANGED_FILES: tracked_module.py")
                print("TESTS_RUN: none")
                print("BLOCKERS: none")
                print("NEXT_ACTIONS: none")
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )

            result = adapter.run(self._task(), self._subtask())

        payload = result.evidence[0].payload
        self.assertEqual(result.status, AgentExecutionStatus.COMPLETED)
        self.assertIn("tracked_module.py", payload["changed_files"])
        self.assertTrue(payload["diff_summary"])

    def test_coding_request_routes_to_codex_agent_when_enabled(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "codex_cli_enabled", True),
            patch.object(settings, "codex_cli_command", "codex"),
            patch.object(settings, "codex_cli_workspace_root", temp_dir),
            patch.object(settings, "openrouter_api_key", None),
        ):
            router = Router(openrouter_client=None)
            planner = Planner(
                openrouter_client=self.NoLlmClient(),
                agent_registry=router.agent_registry,
            )

            subtasks, planner_mode = planner.create_plan(
                "Refactor the failing auth module, fix the regression, and add tests."
            )

        self.assertEqual(planner_mode, "deterministic_fallback")
        self.assertEqual(subtasks[1].assigned_agent, "codex_cli_agent")

    def test_greeting_does_not_route_to_codex(self) -> None:
        supervisor = Supervisor()
        decision = supervisor.assistant_layer.decide("hi")
        lane = supervisor._select_lane("hi", decision)
        self.assertNotEqual(lane.agent_id, "codex_cli_agent")

    def test_browser_request_does_not_route_to_codex(self) -> None:
        planner = Planner(openrouter_client=OpenRouterClient(api_key=None))
        subtasks, _planner_mode = planner.create_plan("Open https://example.com in the browser and inspect it.")
        self.assertTrue(all(subtask.assigned_agent != "codex_cli_agent" for subtask in subtasks))

    def test_reminder_request_does_not_route_to_codex(self) -> None:
        planner = Planner(openrouter_client=OpenRouterClient(api_key=None))
        subtasks, _planner_mode = planner.create_plan("Remind me tomorrow at 9am to submit the form.")
        self.assertTrue(all(subtask.assigned_agent != "codex_cli_agent" for subtask in subtasks))

    def test_failed_codex_run_is_reviewed_and_verifier_marks_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            script_path = self._write_fake_codex(
                workspace_root,
                body="""
                import sys

                print("OUTCOME: blocked")
                print("SUMMARY: Codex could not complete the task.")
                print("CHANGED_FILES: none")
                print("TESTS_RUN: none")
                print("BLOCKERS: failing build")
                print("NEXT_ACTIONS: fix the build")
                sys.exit(2)
                """,
            )
            adapter = CodexCliAgentAdapter(
                descriptor=self._descriptor(),
                runtime_settings=self._settings(
                    workspace_root=str(workspace_root),
                    command=f'"{sys.executable}" "{script_path}"',
                ),
            )
            execution_subtask = self._subtask()
            task = self._task()
            execution_result = adapter.run(task, execution_subtask)
            task.subtasks = [execution_subtask]
            task.results = [execution_result]

            reviewer = ReviewerAgent()
            review_result = reviewer.run(
                task,
                SubTask(
                    title="Review Codex output",
                    description="Review Codex output",
                    objective="Review Codex output",
                    assigned_agent="reviewer_agent",
                ),
            )
            task.results.append(review_result)
            verifier = VerifierAgent(
                evaluator=GoalEvaluator(openrouter_client=self.NoLlmClient())
            )
            verifier_result = verifier.run(
                task,
                SubTask(
                    title="Verify Codex output",
                    description="Verify Codex output",
                    objective="Verify Codex output",
                    assigned_agent="verifier_agent",
                ),
            )

        self.assertEqual(execution_result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(review_result.status, AgentExecutionStatus.BLOCKED)
        self.assertEqual(verifier_result.status, AgentExecutionStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
