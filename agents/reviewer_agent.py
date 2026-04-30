"""Review-focused agent implementation."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

from agents.base_agent import BaseAgent
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    FileEvidence,
    SubTask,
    Task,
    ToolEvidence,
)
from tools.file_tool import FileTool, FileToolResult
from tools.registry import ToolRegistry, build_default_tool_registry
from core.logging import get_logger


class ReviewerAgent(BaseAgent):
    """Handles review, critique, verification, and quality-control tasks."""

    name = "reviewer_agent"

    def __init__(
        self,
        file_tool: FileTool | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.logger = get_logger(__name__)
        self.tool_registry = tool_registry or build_default_tool_registry(file_tool=file_tool)

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        coding_artifact_review = self._review_local_coding_artifact_result(task, subtask)
        if coding_artifact_review is not None:
            return coding_artifact_review

        review_target = self._latest_reviewable_result(task)
        if review_target is not None:
            prior_result, strategy = review_target
            return strategy(task, subtask, prior_result)

        checks = self._build_review_checks(task)
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.SIMULATED,
            summary="Review checkpoints were outlined against the current task outputs, but no live artifact was available for verification.",
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Current task has {len(task.subtasks)} subtasks and {len(task.results)} prior results.",
                f"Review checks: {'; '.join(checks)}",
            ],
            artifacts=[f"review-checklist:{task.id}"],
            next_actions=[
                "Attach concrete artifacts or outputs for actual review execution.",
                "Promote this agent to completed only after live verification is performed.",
            ],
        )

    def _latest_reviewable_result(self, task: Task):
        subtasks_by_id = {subtask.id: subtask for subtask in task.subtasks}
        for result in reversed(task.results):
            originating_subtask = subtasks_by_id.get(result.subtask_id)
            strategy = self._select_review_strategy(result, originating_subtask)
            if strategy is not None:
                return result, strategy
        return None

    def _review_file_result(self, task: Task, subtask: SubTask, prior_result: AgentResult) -> AgentResult:
        evidence = next(
            item for item in prior_result.evidence if isinstance(item, FileEvidence)
        )
        verification_notes: list[str] = []
        passed = False
        expected_path = self._expected_file_path(task, prior_result)
        actual_path = self._normalize_for_compare(
            evidence.actual_path or evidence.file_path,
            workspace_root=evidence.workspace_root,
        )
        path_match = expected_path is None or actual_path == expected_path
        self.logger.info(
            "REVIEWER_PATH_CHECK expected=%r actual=%r match=%s",
            expected_path,
            actual_path,
            path_match,
        )
        if expected_path is not None:
            verification_notes.append(
                f"Expected normalized path: {expected_path}; actual normalized path: {actual_path or 'unknown'}."
            )

        if evidence.operation == "write" and evidence.file_path:
            verification = self._execute_file_tool("read", path=evidence.file_path)
            passed = verification.success and path_match
            verification_notes.append(
                "Verified created file exists and can be read." if verification.success else f"File verification failed: {verification.error}"
            )
            if verification.success and not path_match:
                verification_notes.append("The file was created, but not at the requested normalized path.")
            content_preview = verification.content_preview
            listed_entries: list[str] = []
        elif evidence.operation == "read":
            passed = bool(evidence.content_preview) and path_match
            verification_notes.append(
                "Verified read operation returned content." if passed else "Read operation did not return content."
            )
            content_preview = evidence.content_preview
            listed_entries = []
        elif evidence.operation == "list" and evidence.file_path:
            verification = self._execute_file_tool("list", path=evidence.file_path)
            passed = verification.success and bool(verification.listed_entries or verification.file_path) and path_match
            verification_notes.append(
                "Verified directory listing returned entries."
                if verification.success
                else f"Directory verification failed: {verification.error}"
            )
            if verification.success and not path_match:
                verification_notes.append("The directory listing resolved to a different normalized path than requested.")
            content_preview = None
            listed_entries = verification.listed_entries if verification.success else []
        else:
            verification_notes.append("No reviewable file evidence was available.")
            content_preview = evidence.content_preview
            listed_entries = evidence.listed_entries

        status = AgentExecutionStatus.COMPLETED if passed else AgentExecutionStatus.BLOCKED
        summary = (
            f"Reviewed workspace {evidence.operation} result successfully."
            if passed
            else f"Workspace {evidence.operation} result could not be verified."
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name=prior_result.tool_name or evidence.tool_name,
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Reviewed operation: {evidence.operation}",
            ],
            artifacts=[f"review:{evidence.operation}"],
            evidence=[
                FileEvidence(
                    tool_name=evidence.tool_name,
                    requested_path=evidence.requested_path,
                    normalized_path=evidence.normalized_path,
                    workspace_root=evidence.workspace_root,
                    actual_path=evidence.actual_path or evidence.file_path,
                    file_path=evidence.file_path,
                    operation=evidence.operation,
                    content_preview=content_preview,
                    listed_entries=listed_entries,
                    verification_notes=verification_notes,
                )
            ],
            blockers=[] if passed else verification_notes,
            next_actions=[] if passed else ["Inspect the preceding execution result and retry the workspace file task."],
        )

    def _review_generic_tool_result(
        self,
        task: Task,
        subtask: SubTask,
        prior_result: AgentResult,
    ) -> AgentResult:
        tool_name = prior_result.tool_name or "unknown_tool"
        verified_evidence = [
            ToolEvidence(
                tool_name=evidence.tool_name,
                summary=evidence.summary,
                payload=evidence.payload,
                verification_notes=[
                    f"Verified execution completed and emitted evidence for {evidence.tool_name}."
                ],
            )
            if isinstance(evidence, ToolEvidence)
            else evidence.model_copy(
                update={
                    "verification_notes": [
                        f"Verified execution completed and emitted evidence for {tool_name}."
                    ]
                }
            )
            for evidence in prior_result.evidence
        ]
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Confirmed {tool_name} execution completed and produced concrete evidence.",
            tool_name=tool_name,
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Confirmed prior result status: {prior_result.status.value}",
                f"Evidence count: {len(prior_result.evidence)}",
                "This generic review confirms completion and evidence presence, not deep semantic correctness.",
            ],
            artifacts=[f"review:{tool_name}"],
            evidence=verified_evidence,
            blockers=[],
            next_actions=[],
        )

    def _review_runtime_result(
        self,
        task: Task,
        subtask: SubTask,
        prior_result: AgentResult,
    ) -> AgentResult:
        runtime_evidence = next(
            item for item in prior_result.evidence if isinstance(item, ToolEvidence)
        )
        payload = runtime_evidence.payload
        command = str(payload.get("command", "")).strip()
        exit_code = payload.get("exit_code")
        stdout_preview = payload.get("stdout_preview")
        stderr_preview = payload.get("stderr_preview")
        timed_out = bool(payload.get("timed_out", False))

        verification_notes: list[str] = []
        passed = True

        if prior_result.status != AgentExecutionStatus.COMPLETED:
            passed = False
            verification_notes.append("Runtime execution did not complete successfully.")
        else:
            verification_notes.append("Verified runtime execution completed.")

        if command:
            verification_notes.append(f"Verified runtime command was captured: {command}")
        else:
            passed = False
            verification_notes.append("Runtime command was not captured in the evidence.")

        if isinstance(exit_code, int):
            verification_notes.append(f"Verified runtime exit code was captured: {exit_code}")
        else:
            passed = False
            verification_notes.append("Runtime exit code was not captured in the evidence.")

        if stdout_preview or stderr_preview:
            verification_notes.append("Verified runtime output preview was captured.")
        else:
            passed = False
            verification_notes.append("Runtime output preview was not captured in the evidence.")

        if timed_out:
            passed = False
            verification_notes.append("Runtime evidence indicates the command timed out.")

        status = AgentExecutionStatus.COMPLETED if passed else AgentExecutionStatus.BLOCKED
        summary = (
            "Reviewed runtime command result successfully."
            if passed
            else "Runtime command result could not be verified."
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="runtime_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Runtime command: {command or 'unknown'}",
                "This review confirms completion, exit code capture, and output presence only.",
            ],
            artifacts=["review:runtime_tool"],
            evidence=[
                ToolEvidence(
                    tool_name="runtime_tool",
                    summary=runtime_evidence.summary,
                    payload=runtime_evidence.payload,
                    verification_notes=verification_notes,
                )
            ],
            blockers=[] if passed else verification_notes,
            next_actions=[] if passed else ["Inspect the runtime command result and retry with a simpler workspace-scoped command."],
        )

    def _review_browser_result(
        self,
        task: Task,
        subtask: SubTask,
        prior_result: AgentResult,
    ) -> AgentResult:
        browser_evidence = next(
            item for item in prior_result.evidence if isinstance(item, ToolEvidence)
        )
        payload = browser_evidence.payload
        browser_task = payload.get("browser_task", {})
        requested_url = str(payload.get("requested_url", "")).strip()
        final_url = str(payload.get("final_url", "")).strip()
        title = str(payload.get("title", "")).strip()
        headings = [str(item).strip() for item in payload.get("headings", []) if str(item).strip()]
        text_preview = str(payload.get("text_preview", "")).strip()
        summary_text = str(payload.get("summary_text", "")).strip()
        screenshot_path = str(payload.get("screenshot_path", "")).strip()
        status_code = payload.get("status_code")
        blocker = str(payload.get("blocker") or payload.get("error") or "").strip()
        synthesis_result = str(browser_task.get("synthesis_result", "")).strip()

        verification_notes: list[str] = []
        passed = True

        if prior_result.status != AgentExecutionStatus.COMPLETED:
            passed = False
            verification_notes.append("Browser execution did not complete successfully.")
        else:
            verification_notes.append("Verified browser execution completed.")

        if final_url:
            verification_notes.append(f"Verified final URL was captured: {final_url}")
        else:
            passed = False
            verification_notes.append("Final URL was not captured in browser evidence.")

        if requested_url:
            verification_notes.append(f"Verified requested URL was captured: {requested_url}")
        else:
            verification_notes.append("Requested URL was not captured; continuing because final URL evidence exists.")

        if isinstance(status_code, int):
            if status_code in {401, 403, 407}:
                passed = False
                verification_notes.append(f"Browser evidence shows blocked HTTP status {status_code}.")
            else:
                verification_notes.append(f"Verified HTTP status was captured: {status_code}.")

        if screenshot_path:
            verification_notes.append("Verified browser evidence includes a screenshot path.")
        else:
            verification_notes.append("No screenshot path was captured; structured page evidence is acceptable for this browser task.")

        if blocker:
            passed = False
            verification_notes.append(f"Browser result reported a blocker: {blocker}")

        has_clear_title = bool(title and title.lower() not in {"browser use result", "untitled", "unknown"})
        has_visible_content = bool(headings or text_preview or summary_text)
        if has_visible_content or has_clear_title:
            verification_notes.append("Verified browser evidence includes visible page content.")
        else:
            passed = False
            verification_notes.append("Browser evidence did not include readable visible page content or a clear page title.")

        if synthesis_result and final_url and (has_visible_content or has_clear_title):
            verification_notes.append("Verified browser synthesis answered from captured evidence.")
        else:
            passed = False
            verification_notes.append("Browser synthesis result was missing or not tied to concrete page evidence.")

        status = AgentExecutionStatus.COMPLETED if passed else AgentExecutionStatus.BLOCKED
        summary = (
            "Reviewed browser result successfully."
            if passed
            else "Browser result could not be fully verified."
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="browser_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Browser title: {title or 'unknown'}",
                "This review checks evidence presence and grounded synthesis, not semantic perfection.",
            ],
            artifacts=["review:browser_tool"],
            evidence=[
                ToolEvidence(
                    tool_name="browser_tool",
                    summary=browser_evidence.summary,
                    payload=browser_evidence.payload,
                    verification_notes=verification_notes,
                )
            ],
            blockers=[] if passed else verification_notes,
            next_actions=[] if passed else ["Retry the browser task and capture stronger browser evidence before calling it complete."],
        )

    def _review_search_result(
        self,
        task: Task,
        subtask: SubTask,
        prior_result: AgentResult,
    ) -> AgentResult:
        search_evidence = next(
            item
            for item in prior_result.evidence
            if isinstance(item, ToolEvidence) and item.tool_name == "web_search_tool"
        )
        payload = search_evidence.payload
        query = str(payload.get("query", "")).strip()
        provider = str(payload.get("provider", "")).strip()
        answer = str(payload.get("answer", "")).strip()
        timestamp = str(payload.get("timestamp", "")).strip()
        sources_payload = payload.get("sources", [])
        sources = sources_payload if isinstance(sources_payload, list) else []

        verification_notes: list[str] = []
        passed = True

        if prior_result.status != AgentExecutionStatus.COMPLETED:
            passed = False
            verification_notes.append("Research execution did not complete successfully.")
        else:
            verification_notes.append("Verified source-backed research completed.")

        if query:
            verification_notes.append(f"Verified search query was captured: {query}")
        else:
            passed = False
            verification_notes.append("Search query was not captured in research evidence.")

        if provider:
            verification_notes.append(f"Verified search provider was captured: {provider}")
        else:
            passed = False
            verification_notes.append("Search provider was not captured in research evidence.")

        if answer:
            verification_notes.append("Verified research answer/summary was captured.")
        else:
            passed = False
            verification_notes.append("Research answer/summary was not captured.")

        if timestamp:
            verification_notes.append(f"Verified research timestamp was captured: {timestamp}")
        else:
            passed = False
            verification_notes.append("Research timestamp was not captured.")

        usable_sources = [
            item
            for item in sources
            if isinstance(item, dict)
            and str(item.get("title", "")).strip()
            and str(item.get("url", "")).strip()
        ]
        if usable_sources:
            verification_notes.append(f"Verified {len(usable_sources)} source title/URL pair(s).")
        else:
            passed = False
            verification_notes.append("Research evidence did not include source titles and URLs.")

        status = AgentExecutionStatus.COMPLETED if passed else AgentExecutionStatus.BLOCKED
        summary = (
            "Reviewed source-backed research evidence successfully."
            if passed
            else "Research evidence could not be fully verified."
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="web_search_tool",
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Search provider: {provider or 'unknown'}",
                "This review checks source evidence presence, not full citation accuracy.",
            ],
            artifacts=["review:web_search_tool"],
            evidence=[
                ToolEvidence(
                    tool_name="web_search_tool",
                    summary=search_evidence.summary,
                    payload=search_evidence.payload,
                    verification_notes=verification_notes,
                )
            ],
            blockers=[] if passed else verification_notes,
            next_actions=[] if passed else ["Retry research with a provider result that includes source titles and URLs."],
        )

    def _review_codex_result(
        self,
        task: Task,
        subtask: SubTask,
        prior_result: AgentResult,
    ) -> AgentResult:
        codex_evidence = next(
            item for item in prior_result.evidence if isinstance(item, ToolEvidence)
        )
        payload = codex_evidence.payload
        exit_code = payload.get("exit_code")
        timed_out = bool(payload.get("timed_out", False))
        stdout_preview = str(payload.get("stdout_preview", "")).strip()
        stderr_preview = str(payload.get("stderr_preview", "")).strip()
        changed_files = [str(item).strip() for item in payload.get("changed_files", []) if str(item).strip()]
        diff_summary = str(payload.get("diff_summary", "")).strip()
        tests_run = [str(item).strip() for item in payload.get("tests_run", []) if str(item).strip()]
        completion_blockers = [
            str(item).strip()
            for item in payload.get("completion_evidence_blockers", [])
            if str(item).strip()
        ]
        report = payload.get("codex_report", {})
        report_outcome = str(report.get("outcome", "")).strip().lower()

        verification_notes: list[str] = []
        passed = True

        if prior_result.status != AgentExecutionStatus.COMPLETED:
            passed = False
            verification_notes.append("Codex execution did not complete successfully.")
        else:
            verification_notes.append("Verified Codex execution completed.")

        if isinstance(exit_code, int):
            verification_notes.append(f"Verified Codex exit code was captured: {exit_code}")
        else:
            passed = False
            verification_notes.append("Codex exit code was not captured in the evidence.")

        if stdout_preview or stderr_preview:
            verification_notes.append("Verified Codex stdout/stderr preview was captured.")
        else:
            passed = False
            verification_notes.append("Codex stdout/stderr preview was not captured in the evidence.")

        if diff_summary or changed_files:
            verification_notes.append(
                f"Verified git diff evidence was captured with {len(changed_files)} changed file(s)."
            )
        else:
            passed = False
            verification_notes.append("Git diff evidence was not captured for the Codex run.")

        if "tests_run" in payload:
            verification_notes.append(
                f"Verified tests run were captured: {', '.join(tests_run) if tests_run else 'none reported'}."
            )
        else:
            passed = False
            verification_notes.append("Codex evidence did not report whether tests were run.")

        if timed_out:
            passed = False
            verification_notes.append("Codex evidence indicates the delegated run timed out.")

        if completion_blockers:
            passed = False
            verification_notes.extend(completion_blockers)

        if report_outcome == "needs_user_review":
            passed = False
            verification_notes.append("Codex reported that the result still needs user review.")
        elif report_outcome == "incomplete":
            passed = False
            verification_notes.append("Codex reported that the task is still incomplete.")
        elif report_outcome == "blocked":
            passed = False
            verification_notes.append("Codex reported that the task is blocked.")
        elif report_outcome == "completed":
            verification_notes.append("Codex reported a completed outcome.")
        else:
            passed = False
            verification_notes.append("Codex did not provide a completed final report outcome.")

        status = AgentExecutionStatus.COMPLETED if passed else AgentExecutionStatus.BLOCKED
        summary = (
            "Reviewed Codex CLI execution evidence successfully."
            if passed
            else "Codex CLI execution evidence could not be fully verified."
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="codex_cli",
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Codex reported outcome: {report_outcome or 'unspecified'}",
                f"Changed files: {len(changed_files)}",
                f"Tests run: {', '.join(tests_run) if tests_run else 'none reported'}",
            ],
            artifacts=["review:codex_cli"],
            evidence=[
                ToolEvidence(
                    tool_name="codex_cli",
                    summary=codex_evidence.summary,
                    payload=codex_evidence.payload,
                    verification_notes=verification_notes,
                )
            ],
            blockers=[] if passed else verification_notes,
            next_actions=[] if passed else ["Inspect the Codex evidence, adjust the task, and retry the managed coding lane."],
        )

    def _review_local_coding_artifact_result(
        self,
        task: Task,
        subtask: SubTask,
    ) -> AgentResult | None:
        if "coding artifact" not in subtask.objective.lower():
            return None
        file_result, file_evidence = self._latest_completed_file_write(task)
        runtime_result, runtime_evidence = self._latest_runtime_result(task)
        if file_result is None or file_evidence is None:
            return None

        verification_notes: list[str] = []
        passed = True
        verification = self._execute_file_tool("read", path=file_evidence.file_path or file_evidence.actual_path or "")
        if verification.success:
            verification_notes.append("Verified created file exists and can be read.")
        else:
            passed = False
            verification_notes.append(f"File verification failed: {verification.error}")

        run_payload: dict[str, object] | None = None
        if runtime_evidence is not None:
            run_payload = runtime_evidence.payload
            exit_code = run_payload.get("exit_code")
            timed_out = bool(run_payload.get("timed_out", False))
            has_output = bool(run_payload.get("stdout_preview") or run_payload.get("stderr_preview"))
            if runtime_result is None or runtime_result.status != AgentExecutionStatus.COMPLETED:
                passed = False
                verification_notes.append("Runtime execution did not complete successfully.")
            elif exit_code == 0 and not timed_out:
                verification_notes.append("Verified generated script exited with code 0.")
            else:
                passed = False
                verification_notes.append(f"Generated script did not exit cleanly; exit code was {exit_code}.")
            if has_output:
                verification_notes.append("Verified generated script produced captured output.")
            else:
                passed = False
                verification_notes.append("Generated script did not produce captured stdout or stderr.")

        status = AgentExecutionStatus.COMPLETED if passed else AgentExecutionStatus.BLOCKED
        payload = {
            "created_files": [
                {
                    "requested_path": file_evidence.requested_path,
                    "normalized_path": file_evidence.normalized_path,
                    "workspace_root": file_evidence.workspace_root,
                    "actual_path": file_evidence.actual_path or file_evidence.file_path,
                    "file_path": file_evidence.file_path,
                    "content_preview": verification.content_preview or file_evidence.content_preview,
                }
            ],
            "run": run_payload,
            "tests_run": [str(run_payload.get("command"))] if run_payload and run_payload.get("command") else [],
        }
        summary = (
            "Reviewed local coding artifact evidence successfully."
            if passed
            else "Local coding artifact evidence could not be fully verified."
        )
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="coding_artifact",
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Created files: {len(payload['created_files'])}",
                f"Runtime command: {run_payload.get('command') if run_payload else 'none'}",
            ],
            artifacts=["review:coding_artifact"],
            evidence=[
                ToolEvidence(
                    tool_name="coding_artifact",
                    summary=summary,
                    payload=payload,
                    verification_notes=verification_notes,
                )
            ],
            blockers=[] if passed else verification_notes,
            next_actions=[] if passed else ["Inspect the generated file and runtime output, then retry the coding task."],
        )

    def _latest_completed_file_write(self, task: Task) -> tuple[AgentResult | None, FileEvidence | None]:
        for result in reversed(task.results):
            if result.status != AgentExecutionStatus.COMPLETED or result.tool_name != "file_tool":
                continue
            for evidence in result.evidence:
                if isinstance(evidence, FileEvidence) and evidence.operation == "write" and evidence.file_path:
                    return result, evidence
        return None, None

    def _latest_runtime_result(self, task: Task) -> tuple[AgentResult | None, ToolEvidence | None]:
        for result in reversed(task.results):
            if result.tool_name != "runtime_tool":
                continue
            for evidence in result.evidence:
                if isinstance(evidence, ToolEvidence) and evidence.tool_name == "runtime_tool":
                    return result, evidence
        return None, None

    def _review_blocked_result(
        self,
        task: Task,
        subtask: SubTask,
        prior_result: AgentResult,
    ) -> AgentResult:
        tool_name = prior_result.tool_name or "unknown_tool"
        blockers = prior_result.blockers or [prior_result.summary]
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=f"Reviewed blocked execution from {tool_name} and confirmed the task is not complete.",
            tool_name=tool_name,
            details=[
                f"Goal context: {task.goal}",
                f"Review objective: {subtask.objective}",
                f"Blocked result summary: {prior_result.summary}",
            ],
            artifacts=[f"review:blocker:{tool_name}"],
            evidence=[
                ToolEvidence(
                    tool_name=tool_name,
                    summary=prior_result.summary,
                    payload={
                        "blocked": True,
                        "source_agent": prior_result.agent,
                        "blockers": blockers,
                        "next_actions": prior_result.next_actions,
                    },
                    verification_notes=[
                        f"Reviewed blocked execution from {prior_result.agent}.",
                        *blockers,
                    ],
                )
            ],
            blockers=blockers,
            next_actions=prior_result.next_actions or ["Replan or retry after the blocker is resolved."],
        )

    def _select_review_strategy(
        self,
        result: AgentResult,
        subtask: SubTask | None,
    ) -> Callable[[Task, SubTask, AgentResult], AgentResult] | None:
        if result.status == AgentExecutionStatus.BLOCKED:
            return self._review_blocked_result
        tool_name = result.tool_name or (subtask.tool_invocation.tool_name if subtask and subtask.tool_invocation else None)
        if tool_name == "file_tool":
            if self._is_reviewable_file_result(result, subtask):
                return self._review_file_result
            return None
        if tool_name == "runtime_tool":
            if self._is_reviewable_runtime_result(result):
                return self._review_runtime_result
            return None
        if tool_name == "browser_tool":
            if self._is_reviewable_browser_result(result):
                return self._review_browser_result
            return None
        if tool_name == "web_search_tool":
            if self._is_reviewable_search_result(result):
                return self._review_search_result
            return None
        if tool_name == "codex_cli":
            if self._is_reviewable_codex_result(result):
                return self._review_codex_result
            return None
        if self._is_reviewable_generic_result(result):
            return self._review_generic_tool_result
        return None

    def _is_reviewable_file_result(self, result: AgentResult, subtask: SubTask | None) -> bool:
        supported_operations = {"write", "read", "list"}
        if not result.evidence:
            return False

        if any(evidence.verification_notes for evidence in result.evidence):
            return False

        if subtask and subtask.tool_invocation is not None:
            invocation = subtask.tool_invocation
            return (
                invocation.tool_name == "file_tool"
                and invocation.action in supported_operations
                and any(
                    isinstance(evidence, FileEvidence) and evidence.operation == invocation.action
                    for evidence in result.evidence
                )
            )

        return any(
            isinstance(evidence, FileEvidence) and evidence.operation in supported_operations
            for evidence in result.evidence
        )

    def _is_reviewable_generic_result(self, result: AgentResult) -> bool:
        if result.status != AgentExecutionStatus.COMPLETED or not result.evidence:
            return False
        return not any(evidence.verification_notes for evidence in result.evidence)

    def _is_reviewable_runtime_result(self, result: AgentResult) -> bool:
        if result.status != AgentExecutionStatus.COMPLETED or not result.evidence:
            return False
        if any(evidence.verification_notes for evidence in result.evidence):
            return False
        return any(
            isinstance(evidence, ToolEvidence) and evidence.tool_name == "runtime_tool"
            for evidence in result.evidence
        )

    def _is_reviewable_browser_result(self, result: AgentResult) -> bool:
        if result.status != AgentExecutionStatus.COMPLETED or not result.evidence:
            return False
        if any(evidence.verification_notes for evidence in result.evidence):
            return False
        return any(
            isinstance(evidence, ToolEvidence) and evidence.tool_name == "browser_tool"
            for evidence in result.evidence
        )

    def _is_reviewable_search_result(self, result: AgentResult) -> bool:
        if result.status != AgentExecutionStatus.COMPLETED or not result.evidence:
            return False
        if any(evidence.verification_notes for evidence in result.evidence):
            return False
        return any(
            isinstance(evidence, ToolEvidence) and evidence.tool_name == "web_search_tool"
            for evidence in result.evidence
        )

    def _is_reviewable_codex_result(self, result: AgentResult) -> bool:
        if result.status != AgentExecutionStatus.COMPLETED or not result.evidence:
            return False
        if any(evidence.verification_notes for evidence in result.evidence):
            return False
        return any(
            isinstance(evidence, ToolEvidence) and evidence.tool_name == "codex_cli"
            for evidence in result.evidence
        )

    def _execute_file_tool(self, action: str, **parameters: str) -> FileToolResult:
        from core.models import ToolInvocation

        return FileToolResult.model_validate(
            self.tool_registry.execute(
                ToolInvocation(tool_name="file_tool", action=action, parameters=parameters)
            )
        )

    def _build_review_checks(self, task: Task) -> list[str]:
        return [
            "Confirm every subtask has an explicit assigned agent",
            "Confirm blocked work is called out instead of implied complete",
            f"Confirm remaining placeholder integrations are documented for task {task.id}",
        ]

    def _expected_file_path(self, task: Task, prior_result: AgentResult) -> str | None:
        originating_subtask = next((item for item in task.subtasks if item.id == prior_result.subtask_id), None)
        if originating_subtask and originating_subtask.tool_invocation is not None:
            requested_path = originating_subtask.tool_invocation.parameters.get("path")
            if requested_path:
                return self._normalize_for_compare(requested_path)
        match = re.search(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+)", task.goal)
        if match:
            return self._normalize_for_compare(match.group(1))
        return None

    def _normalize_for_compare(
        self,
        path: str | None,
        *,
        workspace_root: str | None = None,
    ) -> str | None:
        if not path:
            return None
        candidate = Path(str(path).replace("\\", "/"))
        root = Path(workspace_root) if workspace_root else None
        if root is not None:
            try:
                candidate = candidate.resolve().relative_to(root.resolve())
                return candidate.as_posix()
            except ValueError:
                pass
        normalized = candidate.as_posix().lstrip("./")
        for prefix in ("workspace/",):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        return normalized or "."
