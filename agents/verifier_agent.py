"""Final verification agent implementation."""

from __future__ import annotations

from core.models import FileEvidence
from agents.base_agent import BaseAgent
from core.evaluator import GoalEvaluator
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence


class VerifierAgent(BaseAgent):
    """Performs the final anti-fake-completion quality gate."""

    name = "verifier_agent"

    def __init__(self, *, evaluator: GoalEvaluator | None = None) -> None:
        self.evaluator = evaluator or GoalEvaluator()

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        file_mismatch = self._find_reviewed_file_mismatch(task)
        if file_mismatch is not None:
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="Verified that the file result does not satisfy the requested path.",
                tool_name="verification_agent",
                details=[
                    f"Goal context: {task.goal}",
                    f"Verification objective: {subtask.objective}",
                    file_mismatch,
                ],
                artifacts=[f"verification:{task.id}"],
                evidence=[
                    ToolEvidence(
                        tool_name="verification_agent",
                        summary="Final verification found a reviewed file path mismatch.",
                        payload={
                            "done": False,
                            "blocked": True,
                            "needs_review": False,
                            "should_continue": False,
                            "completion_confidence": 0.0,
                            "reasoning": file_mismatch,
                            "missing": ["Retry the file operation at the correct normalized path"],
                            "next_action": "Retry the file operation at the correct normalized path",
                            "evaluation_mode": "deterministic",
                        },
                    )
                ],
                blockers=["Retry the file operation at the correct normalized path"],
                next_actions=["Retry the file operation at the correct normalized path"],
            )
        evaluation, evaluation_mode = self.evaluator.evaluate(task)
        if evaluation.satisfied:
            status = AgentExecutionStatus.COMPLETED
            summary = "Verified that the final output satisfies the original goal."
        elif evaluation.blocked:
            status = AgentExecutionStatus.BLOCKED
            summary = "Verified that the task is blocked and cannot honestly be marked complete."
        else:
            status = AgentExecutionStatus.SIMULATED
            summary = "Verified that the task is not yet complete and should not be marked done."
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary,
            tool_name="verification_agent",
            details=[
                f"Goal context: {task.goal}",
                f"Verification objective: {subtask.objective}",
                f"Evaluation mode: {evaluation_mode}",
                f"Verifier reasoning: {evaluation.reasoning}",
            ],
            artifacts=[f"verification:{task.id}"],
            evidence=[
                ToolEvidence(
                    tool_name="verification_agent",
                    summary="Final verification for the current goal.",
                    payload={
                        "done": evaluation.satisfied,
                        "blocked": evaluation.blocked,
                        "needs_review": evaluation.needs_review,
                        "should_continue": evaluation.should_continue,
                        "completion_confidence": evaluation.completion_confidence,
                        "reasoning": evaluation.reasoning,
                        "missing": evaluation.missing,
                        "next_action": evaluation.next_action,
                        "evaluation_mode": evaluation_mode,
                    },
                )
            ],
            blockers=evaluation.missing if (evaluation.blocked or not evaluation.satisfied) else [],
            next_actions=[evaluation.next_action] if evaluation.next_action else [],
        )

    def _find_reviewed_file_mismatch(self, task: Task) -> str | None:
        for result in reversed(task.results):
            for evidence in result.evidence:
                if not isinstance(evidence, FileEvidence):
                    continue
                if evidence.normalized_path and evidence.actual_path and evidence.workspace_root:
                    expected = self.evaluator._normalize_file_path(
                        evidence.normalized_path,
                        workspace_root=evidence.workspace_root,
                    )
                    actual = self.evaluator._normalize_file_path(
                        evidence.actual_path,
                        workspace_root=evidence.workspace_root,
                    )
                    if expected and actual and expected != actual:
                        return f"Expected normalized path {expected}, but actual normalized path was {actual}."
        return None
