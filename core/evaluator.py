"""Goal evaluation for bounded supervisor decisions."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from core.models import (
    AgentExecutionStatus,
    ExecutionEscalation,
    FileEvidence,
    GoalEvaluation,
    Task,
    ToolEvidence,
)
from core.model_routing import ModelRequestContext
from integrations.openrouter_client import OpenRouterClient


class GoalEvaluator:
    """Assess whether the current task evidence satisfies the user's goal."""

    def __init__(self, *, openrouter_client: OpenRouterClient | None = None) -> None:
        self.openrouter_client = openrouter_client or OpenRouterClient()

    def evaluate(self, task: Task) -> tuple[GoalEvaluation, str]:
        llm_evaluation = self._evaluate_with_llm(task)
        if llm_evaluation is not None:
            return llm_evaluation, "openrouter"
        return self._evaluate_deterministically(task), "deterministic"

    def _evaluate_with_llm(self, task: Task) -> GoalEvaluation | None:
        if not self.openrouter_client.is_configured():
            return None

        prompt = (
            "Evaluate whether the user's goal is satisfied based only on the provided execution evidence.\n"
            "Return strict JSON with the shape "
            '{"satisfied":true,"reasoning":"...","missing":["..."],"should_continue":false,"blocked":false,"needs_review":false,"completion_confidence":0.82,"next_action":"..."}.\n'
            "Do not claim success without explicit supporting evidence. "
            "Treat reviewer verification as important supporting evidence when present.\n"
            "For objective-like requests, prefer should_continue=true until there is clear completion or a real blocker.\n"
            f"Goal: {task.goal}\n"
            f"Results: {json.dumps(self._serialize_results(task), ensure_ascii=True)}"
        )

        try:
            response = self.openrouter_client.prompt(
                prompt,
                system_prompt=(
                    "You are a careful evaluator. Ground every decision in the provided evidence and return only valid JSON."
                ),
                label="goal_evaluate",
                context=self._model_context_for_task(task),
            )
            payload = json.loads(response)
            if "satisfied" not in payload:
                return None
            satisfied = bool(payload.get("satisfied", False))
            reasoning = str(payload.get("reasoning", "")).strip()
            missing_payload = payload.get("missing", [])
            if not reasoning or not isinstance(missing_payload, list):
                return None
            missing = [str(item).strip() for item in missing_payload if str(item).strip()]
            should_continue = bool(payload.get("should_continue", not satisfied))
            blocked = bool(payload.get("blocked", False))
            needs_review = bool(payload.get("needs_review", False))
            try:
                completion_confidence = float(payload.get("completion_confidence", 0.0))
            except (TypeError, ValueError):
                completion_confidence = 0.0
            next_action = str(payload.get("next_action", "")).strip() or None
            if satisfied and not self._has_supporting_evidence(task):
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="The LLM evaluation was overridden because no supporting evidence was available.",
                    missing=["Concrete execution evidence or reviewer verification"],
                    should_continue=True,
                    blocked=False,
                    needs_review=True,
                    completion_confidence=0.2,
                    next_action="Gather reviewed evidence before claiming completion",
                )
            return GoalEvaluation(
                satisfied=satisfied,
                reasoning=reasoning,
                missing=missing,
                should_continue=should_continue,
                blocked=blocked,
                needs_review=needs_review,
                completion_confidence=max(0.0, min(completion_confidence, 1.0)),
                next_action=next_action,
            )
        except (RuntimeError, httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def _evaluate_deterministically(self, task: Task) -> GoalEvaluation:
        if any(result.status == AgentExecutionStatus.BLOCKED for result in task.results):
            return GoalEvaluation(
                satisfied=False,
                reasoning="Execution is blocked, so the goal cannot be considered satisfied.",
                missing=["Resolve the reported blocker before continuing"],
                should_continue=False,
                blocked=True,
                needs_review=False,
                completion_confidence=0.0,
                next_action="Resolve the reported blocker",
            )

        if task.escalation_level == ExecutionEscalation.SINGLE_ACTION:
            action_result = self._latest_completed_non_review_result(task)
            if action_result is not None and action_result.evidence:
                if action_result.tool_name == "browser_tool" and not self._result_has_browser_evidence(action_result):
                    return GoalEvaluation(
                        satisfied=False,
                        reasoning="The browser action completed status was rejected because it did not include concrete page evidence.",
                        missing=["Browser evidence with final URL plus visible content or a clear page title"],
                        should_continue=True,
                        blocked=False,
                        needs_review=True,
                        completion_confidence=0.35,
                        next_action="Capture real browser page evidence before claiming completion",
                    )
                if action_result.tool_name == "web_search_tool" and not self._result_has_search_evidence(action_result):
                    return GoalEvaluation(
                        satisfied=False,
                        reasoning="The research action completed status was rejected because it did not include source-backed evidence.",
                        missing=["Search evidence with provider, answer, timestamp, and source title/URL pairs"],
                        should_continue=True,
                        blocked=False,
                        needs_review=True,
                        completion_confidence=0.35,
                        next_action="Capture real search source evidence before claiming completion",
                    )
                return GoalEvaluation(
                    satisfied=True,
                    reasoning="The requested single action completed with concrete execution evidence.",
                    missing=[],
                    should_continue=False,
                    blocked=False,
                    needs_review=False,
                    completion_confidence=0.88,
                    next_action=None,
                )

        review_result = self._latest_completed_review_result(task)
        if review_result is None:
            next_action = (
                "Run a reviewer pass before claiming the objective is complete"
                if task.escalation_level != ExecutionEscalation.SINGLE_ACTION
                else "Complete the requested action and gather concrete evidence"
            )
            return GoalEvaluation(
                satisfied=False,
                reasoning="No completed reviewer verification was found for the current task.",
                missing=["A completed reviewer result tied to concrete execution evidence"],
                should_continue=bool(task.results),
                blocked=False,
                needs_review=task.escalation_level != ExecutionEscalation.SINGLE_ACTION,
                completion_confidence=0.35 if task.results else 0.0,
                next_action=next_action,
            )

        return self._evaluate_reviewed_evidence(review_result)

    def _serialize_results(self, task: Task) -> list[dict[str, object]]:
        serialized: list[dict[str, object]] = []
        for result in task.results:
            serialized.append(
                {
                    "agent": result.agent,
                    "status": result.status.value,
                    "summary": result.summary,
                    "details": result.details,
                    "blockers": result.blockers,
                    "evidence": [item.model_dump() for item in result.evidence],
                }
            )
        return serialized

    def _has_supporting_evidence(self, task: Task) -> bool:
        return any(
            result.agent == "reviewer_agent"
            and result.status == AgentExecutionStatus.COMPLETED
            and any(evidence.verification_notes for evidence in result.evidence)
            for result in task.results
        )

    def _latest_completed_review_result(self, task: Task):
        return next(
            (
                result
                for result in reversed(task.results)
                if result.agent == "reviewer_agent"
                and result.status == AgentExecutionStatus.COMPLETED
                and result.evidence
            ),
            None,
        )

    def _latest_completed_non_review_result(self, task: Task):
        return next(
            (
                result
                for result in reversed(task.results)
                if result.agent != "reviewer_agent"
                and result.status == AgentExecutionStatus.COMPLETED
            ),
            None,
        )

    def _result_has_browser_evidence(self, result) -> bool:
        browser_evidence = next(
            (
                evidence
                for evidence in result.evidence
                if isinstance(evidence, ToolEvidence) and evidence.tool_name == "browser_tool"
            ),
            None,
        )
        if browser_evidence is None:
            return False
        return self._browser_payload_has_page_evidence(browser_evidence.payload)

    def _evaluate_reviewed_evidence(self, review_result) -> GoalEvaluation:
        reviewed_evidence = [
            evidence for evidence in review_result.evidence if evidence.verification_notes
        ]
        if not reviewed_evidence:
            return GoalEvaluation(
                satisfied=False,
                reasoning="Reviewer output exists, but it does not yet contain verified evidence to mark the goal satisfied.",
                missing=["Reviewer verification notes tied to concrete evidence"],
                should_continue=True,
                blocked=False,
                needs_review=True,
                completion_confidence=0.45,
                next_action="Collect reviewer verification tied to concrete evidence",
            )

        for evidence in reviewed_evidence:
            if isinstance(evidence, FileEvidence):
                file_evaluation = self._evaluate_reviewed_file_evidence(evidence)
                if file_evaluation.satisfied:
                    return file_evaluation
            elif isinstance(evidence, ToolEvidence):
                tool_evaluation = self._evaluate_reviewed_tool_evidence(evidence)
                if tool_evaluation.satisfied:
                    return tool_evaluation

        return GoalEvaluation(
            satisfied=False,
            reasoning="Reviewer output exists, but it does not yet contain enough verified evidence to mark the goal satisfied.",
            missing=["Reviewed evidence that confirms the requested outcome"],
            should_continue=True,
            blocked=False,
            needs_review=True,
            completion_confidence=0.5,
            next_action="Collect stronger reviewed evidence for the requested outcome",
        )

    def _evaluate_reviewed_file_evidence(self, evidence: FileEvidence) -> GoalEvaluation:
        expected_path = self._normalize_file_path(
            evidence.normalized_path or evidence.requested_path,
            workspace_root=evidence.workspace_root,
        )
        actual_path = self._normalize_file_path(
            evidence.actual_path or evidence.file_path,
            workspace_root=evidence.workspace_root,
        )
        if expected_path and actual_path and expected_path != actual_path:
            return GoalEvaluation(
                satisfied=False,
                reasoning=f"Reviewed file evidence shows a path mismatch: expected {expected_path} but actual was {actual_path}.",
                missing=["Create or access the file at the exact requested normalized path"],
                should_continue=False,
                blocked=True,
                needs_review=False,
                completion_confidence=0.0,
                next_action="Retry the file operation with the correct normalized workspace path",
            )
        if evidence.operation == "write" and evidence.file_path:
            return GoalEvaluation(
                satisfied=True,
                reasoning=f"Reviewer verified that the created file exists at {evidence.file_path}.",
                missing=[],
                should_continue=False,
                blocked=False,
                needs_review=False,
                completion_confidence=0.95,
                next_action=None,
            )
        if evidence.operation == "read" and evidence.content_preview:
            return GoalEvaluation(
                satisfied=True,
                reasoning="Reviewer verified that the requested file content was returned.",
                missing=[],
                should_continue=False,
                blocked=False,
                needs_review=False,
                completion_confidence=0.95,
                next_action=None,
            )
        if evidence.operation == "list" and evidence.listed_entries:
            return GoalEvaluation(
                satisfied=True,
                reasoning="Reviewer verified that the workspace directory listing returned entries.",
                missing=[],
                should_continue=False,
                blocked=False,
                needs_review=False,
                completion_confidence=0.9,
                next_action=None,
            )
        return GoalEvaluation(
            satisfied=False,
            reasoning="Reviewed file evidence exists, but it does not yet confirm the requested file outcome.",
            missing=["File evidence that confirms the requested operation output"],
            should_continue=True,
            blocked=False,
            needs_review=True,
            completion_confidence=0.55,
            next_action="Gather file evidence that directly confirms the requested outcome",
        )

    def _evaluate_reviewed_tool_evidence(self, evidence: ToolEvidence) -> GoalEvaluation:
        if evidence.tool_name == "browser_tool":
            payload = evidence.payload
            browser_task = payload.get("browser_task", {})
            synthesis_result = str(browser_task.get("synthesis_result", "")).strip()
            if synthesis_result and self._browser_payload_has_page_evidence(payload):
                return GoalEvaluation(
                    satisfied=True,
                    reasoning="Reviewer verified that the browser task captured concrete page evidence and returned a grounded synthesis.",
                    missing=[],
                    should_continue=False,
                    blocked=False,
                    needs_review=False,
                    completion_confidence=0.92,
                    next_action=None,
                )
            return GoalEvaluation(
                satisfied=False,
                reasoning="Reviewed browser evidence exists, but it does not yet confirm both captured page evidence and a grounded synthesis.",
                missing=["Reviewed browser evidence with final URL, visible content, and grounded synthesis"],
                should_continue=True,
                blocked=False,
                needs_review=True,
                completion_confidence=0.52,
                next_action="Capture reviewed browser evidence with grounded synthesis",
            )
        if evidence.tool_name == "web_search_tool":
            payload = evidence.payload
            if self._search_payload_has_source_evidence(payload):
                return GoalEvaluation(
                    satisfied=True,
                    reasoning="Reviewer verified that research completed with source-backed evidence.",
                    missing=[],
                    should_continue=False,
                    blocked=False,
                    needs_review=False,
                    completion_confidence=0.9,
                    next_action=None,
                )
            return GoalEvaluation(
                satisfied=False,
                reasoning="Reviewed research evidence exists, but it does not include enough source-backed evidence.",
                missing=["Reviewed research evidence with query, provider, answer, timestamp, and source title/URL pairs"],
                should_continue=True,
                blocked=False,
                needs_review=True,
                completion_confidence=0.5,
                next_action="Capture reviewed source-backed research evidence",
            )
        if evidence.tool_name == "runtime_tool":
            payload = evidence.payload
            exit_code = payload.get("exit_code")
            has_output = bool(payload.get("stdout_preview") or payload.get("stderr_preview"))
            if payload.get("command") and isinstance(exit_code, int) and has_output:
                return GoalEvaluation(
                    satisfied=True,
                    reasoning="Reviewer verified that the runtime command completed with a captured exit code and output preview.",
                    missing=[],
                    should_continue=False,
                    blocked=False,
                    needs_review=False,
                    completion_confidence=0.9,
                    next_action=None,
                )
            return GoalEvaluation(
                satisfied=False,
                reasoning="Reviewed runtime evidence exists, but it does not yet confirm a captured command result.",
                missing=["Reviewed runtime evidence with command, exit code, and output preview"],
                should_continue=True,
                blocked=False,
                needs_review=True,
                completion_confidence=0.5,
                next_action="Capture reviewed runtime evidence with exit code and output preview",
            )
        if evidence.tool_name == "codex_cli":
            payload = evidence.payload
            report = payload.get("codex_report", {})
            report_outcome = str(report.get("outcome", "")).strip().lower()
            exit_code = payload.get("exit_code")
            timed_out = bool(payload.get("timed_out", False))
            changed_files = [str(item).strip() for item in payload.get("changed_files", []) if str(item).strip()]
            completion_blockers = [
                str(item).strip()
                for item in payload.get("completion_evidence_blockers", [])
                if str(item).strip()
            ]
            has_output = bool(payload.get("stdout_preview") or payload.get("stderr_preview"))
            has_git_evidence = bool(payload.get("diff_summary") or changed_files)
            if timed_out:
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed Codex evidence shows the delegated run timed out before completion.",
                    missing=["A completed Codex run with captured output and diff evidence"],
                    should_continue=False,
                    blocked=True,
                    needs_review=False,
                    completion_confidence=0.0,
                    next_action="Retry the Codex task with a narrower scope or longer timeout",
                )
            if report_outcome == "needs_user_review":
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed Codex evidence indicates the delegated coding task still needs user review.",
                    missing=["User review of the Codex-generated changes"],
                    should_continue=False,
                    blocked=False,
                    needs_review=True,
                    completion_confidence=0.72,
                    next_action="Review the Codex-generated changes before marking the task complete",
                )
            if report_outcome == "incomplete":
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed Codex evidence indicates the delegated coding task is still incomplete.",
                    missing=["A follow-up Codex pass or manual completion for the remaining work"],
                    should_continue=True,
                    blocked=False,
                    needs_review=True,
                    completion_confidence=0.58,
                    next_action="Continue the coding task with a narrower follow-up objective",
                )
            if report_outcome == "blocked":
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed Codex evidence indicates the delegated coding task is blocked.",
                    missing=["A follow-up Codex pass after resolving the reported blocker"],
                    should_continue=False,
                    blocked=True,
                    needs_review=False,
                    completion_confidence=0.0,
                    next_action="Resolve the Codex-reported blocker before retrying",
                )
            if completion_blockers:
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed Codex evidence exists, but the completion evidence gate failed.",
                    missing=completion_blockers,
                    should_continue=True,
                    blocked=False,
                    needs_review=True,
                    completion_confidence=0.45,
                    next_action="Retry Codex with concrete changed-file, diff, and verification evidence",
                )
            if report_outcome == "completed" and isinstance(exit_code, int) and exit_code == 0 and has_output and has_git_evidence:
                return GoalEvaluation(
                    satisfied=True,
                    reasoning="Reviewer verified that Codex completed the delegated coding run with output and git diff evidence.",
                    missing=[],
                    should_continue=False,
                    blocked=False,
                    needs_review=False,
                    completion_confidence=0.91,
                    next_action=None,
                )
            return GoalEvaluation(
                satisfied=False,
                reasoning="Reviewed Codex evidence exists, but it does not yet confirm a complete bounded coding result.",
                missing=["Reviewed Codex evidence with exit code, output preview, and git diff evidence"],
                should_continue=True,
                blocked=False,
                needs_review=True,
                completion_confidence=0.55,
                next_action="Capture stronger reviewed Codex evidence before claiming completion",
            )
        if evidence.tool_name == "coding_artifact":
            payload = evidence.payload
            created_files = payload.get("created_files", [])
            run_payload = payload.get("run")
            has_created_file = isinstance(created_files, list) and any(
                isinstance(item, dict) and (item.get("file_path") or item.get("actual_path"))
                for item in created_files
            )
            if not has_created_file:
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed coding evidence exists, but it does not include a created file.",
                    missing=["Reviewed coding evidence with at least one created file path"],
                    should_continue=True,
                    blocked=False,
                    needs_review=True,
                    completion_confidence=0.48,
                    next_action="Capture created file evidence for the coding task",
                )
            if isinstance(run_payload, dict):
                exit_code = run_payload.get("exit_code")
                timed_out = bool(run_payload.get("timed_out", False))
                has_output = bool(run_payload.get("stdout_preview") or run_payload.get("stderr_preview"))
                if exit_code == 0 and not timed_out and has_output:
                    return GoalEvaluation(
                        satisfied=True,
                        reasoning="Reviewer verified that the coding task created a file and the generated script ran with captured output.",
                        missing=[],
                        should_continue=False,
                        blocked=False,
                        needs_review=False,
                        completion_confidence=0.93,
                        next_action=None,
                    )
                return GoalEvaluation(
                    satisfied=False,
                    reasoning="Reviewed coding evidence shows the generated script did not run successfully.",
                    missing=["A generated script run with exit code 0 and captured output"],
                    should_continue=False,
                    blocked=True,
                    needs_review=False,
                    completion_confidence=0.0,
                    next_action="Fix the generated script and rerun it",
                )
            return GoalEvaluation(
                satisfied=True,
                reasoning="Reviewer verified that the requested coding artifact file was created.",
                missing=[],
                should_continue=False,
                blocked=False,
                needs_review=False,
                completion_confidence=0.9,
                next_action=None,
            )
        if evidence.summary or evidence.payload:
            return GoalEvaluation(
                satisfied=True,
                reasoning=f"Reviewer verified that {evidence.tool_name} completed and returned concrete evidence.",
                missing=[],
                should_continue=False,
                blocked=False,
                needs_review=False,
                completion_confidence=0.88,
                next_action=None,
            )
        return GoalEvaluation(
            satisfied=False,
            reasoning="Reviewed non-file evidence exists, but it does not yet contain concrete tool output.",
            missing=["Concrete reviewed output from the executed tool"],
            should_continue=True,
            blocked=False,
            needs_review=True,
            completion_confidence=0.45,
            next_action="Capture concrete reviewed output from the executed tool",
        )

    def _browser_payload_has_page_evidence(self, payload: dict[str, object]) -> bool:
        if payload.get("blocker") or payload.get("error"):
            return False
        status_code = payload.get("status_code")
        if status_code in {401, 403, 407}:
            return False
        final_url = str(payload.get("final_url", "")).strip()
        if not final_url:
            return False
        title = str(payload.get("title", "")).strip()
        clear_title = bool(title and title.lower() not in {"browser use result", "untitled", "unknown"})
        headings = payload.get("headings")
        has_headings = isinstance(headings, list) and any(str(item).strip() for item in headings)
        return bool(
            clear_title
            or has_headings
            or str(payload.get("text_preview", "")).strip()
            or str(payload.get("summary_text", "")).strip()
        )

    def _result_has_search_evidence(self, result) -> bool:
        search_evidence = next(
            (
                evidence
                for evidence in result.evidence
                if isinstance(evidence, ToolEvidence) and evidence.tool_name == "web_search_tool"
            ),
            None,
        )
        if search_evidence is None:
            return False
        return self._search_payload_has_source_evidence(search_evidence.payload)

    def _search_payload_has_source_evidence(self, payload: dict[str, object]) -> bool:
        if not str(payload.get("query", "")).strip():
            return False
        if not str(payload.get("provider", "")).strip():
            return False
        if not str(payload.get("answer", "")).strip():
            return False
        if not str(payload.get("timestamp", "")).strip():
            return False
        sources = payload.get("sources")
        if not isinstance(sources, list):
            return False
        return any(
            isinstance(source, dict)
            and str(source.get("title", "")).strip()
            and str(source.get("url", "")).strip()
            for source in sources
        )

    def _model_context_for_task(self, task: Task) -> ModelRequestContext:
        reviewer_rejected = any(
            result.agent == "reviewer_agent" and result.status == AgentExecutionStatus.BLOCKED
            for result in task.results
        )
        verifier_failed = any(
            result.agent == "verifier_agent" and result.status != AgentExecutionStatus.COMPLETED
            for result in task.results
        )
        evidence_quality = "low"
        if any(result.evidence for result in task.results):
            evidence_quality = "medium"
        if self._has_supporting_evidence(task):
            evidence_quality = "high"
        return ModelRequestContext(
            intent_label="verification",
            request_mode=task.request_mode.value,
            selected_lane="verification",
            selected_agent="verifier_agent",
            task_complexity=(
                "high"
                if task.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION
                else "medium"
            ),
            risk_level=(
                "high"
                if task.escalation_level == ExecutionEscalation.OBJECTIVE_COMPLETION
                else "medium"
            ),
            requires_tool_use=bool(task.results),
            requires_review=True,
            verifier_failed=verifier_failed,
            reviewer_rejected=reviewer_rejected,
            replan_count=0,
            evidence_quality=evidence_quality,
            user_visible_latency_sensitivity="medium",
            cost_sensitivity="medium",
        )

    def _normalize_file_path(
        self,
        path: str | None,
        *,
        workspace_root: str | None = None,
    ) -> str | None:
        if not path:
            return None
        candidate = Path(str(path).replace("\\", "/"))
        if workspace_root:
            try:
                return candidate.resolve().relative_to(Path(workspace_root).resolve()).as_posix()
            except ValueError:
                pass
        normalized = candidate.as_posix().lstrip("./")
        if normalized.lower().startswith("workspace/"):
            normalized = normalized[len("workspace/") :]
        return normalized or "."
