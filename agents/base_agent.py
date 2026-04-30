"""Base abstraction for all specialized worker agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterable, Mapping

from core.models import (
    AgentExecutionStatus,
    AgentResult,
    EvidenceItem,
    NormalizedToolOutput,
    SubTask,
    Task,
    ToolInvocation,
)
from tools.registry import ToolRegistry


class BaseAgent(ABC):
    """Common contract for worker agents managed by the supervisor."""

    name: str = "base_agent"
    supported_tool_names: frozenset[str] = frozenset()

    def supports_tool_invocation(self, invocation: ToolInvocation | None) -> bool:
        """Return whether this agent can execute the provided invocation directly."""

        if invocation is None:
            return True
        return invocation.tool_name in self.supported_tool_names

    def execute_tool_invocation(
        self,
        task: Task,
        subtask: SubTask,
        *,
        tool_registry: ToolRegistry,
        validate_invocation: Callable[[ToolInvocation], str | None],
        build_evidence: Callable[[ToolInvocation, NormalizedToolOutput], Iterable[EvidenceItem]],
        build_summary: Callable[[ToolInvocation, NormalizedToolOutput], str] | None = None,
        build_details: Callable[[Task, SubTask, ToolInvocation, NormalizedToolOutput], list[str]]
        | None = None,
        build_artifacts: Callable[[ToolInvocation, NormalizedToolOutput], list[str]] | None = None,
        build_next_actions: Callable[[ToolInvocation, NormalizedToolOutput], list[str]] | None = None,
    ) -> AgentResult:
        """Run the common executor flow for a structured tool invocation."""

        invocation = subtask.tool_invocation
        if invocation is None:
            raise ValueError("execute_tool_invocation requires a subtask with a tool invocation.")

        if not self.supports_tool_invocation(invocation):
            return self._blocked_tool_result(
                task,
                subtask,
                invocation,
                summary="The assigned tool invocation is not compatible with this agent.",
                blocker=f"{self.name} does not support tool '{invocation.tool_name}'.",
                next_actions=["Route this subtask to an agent that supports the requested tool."],
            )

        validation_error = validate_invocation(invocation)
        if validation_error is not None:
            return self._blocked_tool_result(
                task,
                subtask,
                invocation,
                summary="The planned tool invocation could not be executed safely.",
                blocker=validation_error,
                next_actions=["Repair the structured tool invocation and retry execution."],
            )

        try:
            normalized_output = self.normalize_tool_output(
                invocation,
                tool_registry.execute(invocation),
            )
        except (TypeError, ValueError) as exc:
            return self._blocked_tool_result(
                task,
                subtask,
                invocation,
                summary="The tool invocation could not be executed.",
                blocker=str(exc),
                next_actions=["Inspect the tool registration and invocation payload before retrying."],
            )

        status = (
            AgentExecutionStatus.COMPLETED
            if normalized_output.success
            else AgentExecutionStatus.BLOCKED
        )
        summary_builder = build_summary or self._default_execution_summary
        details_builder = build_details or self._default_execution_details
        artifacts_builder = build_artifacts or self._default_execution_artifacts
        next_actions_builder = build_next_actions or self._default_execution_next_actions
        blockers = [normalized_output.error] if normalized_output.error else []
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=status,
            summary=summary_builder(invocation, normalized_output),
            tool_name=invocation.tool_name,
            details=details_builder(task, subtask, invocation, normalized_output),
            artifacts=artifacts_builder(invocation, normalized_output),
            evidence=list(build_evidence(invocation, normalized_output)),
            blockers=blockers,
            next_actions=next_actions_builder(invocation, normalized_output),
        )

    def normalize_tool_output(
        self,
        invocation: ToolInvocation,
        raw_output: Mapping[str, object] | object,
    ) -> NormalizedToolOutput:
        """Normalize raw tool output into the minimal executor contract."""

        payload = self._coerce_tool_output_mapping(raw_output)
        tool_payload = self._extract_tool_payload(payload)
        error = self._normalize_optional_text(payload.get("error"))
        summary = self._normalize_optional_text(payload.get("summary"))
        success = bool(payload.get("success", False))
        if summary is None:
            summary = self._build_default_tool_summary(invocation, success=success, error=error)
        return NormalizedToolOutput(
            success=success,
            error=error,
            summary=summary,
            payload=tool_payload,
        )

    def _blocked_tool_result(
        self,
        task: Task,
        subtask: SubTask,
        invocation: ToolInvocation,
        *,
        summary: str,
        blocker: str,
        next_actions: list[str],
    ) -> AgentResult:
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary=summary,
            tool_name=invocation.tool_name,
            details=[
                f"Goal context: {task.goal}",
                f"Objective: {subtask.objective}",
                f"Invocation: {invocation.model_dump()}",
            ],
            blockers=[blocker],
            next_actions=next_actions,
        )

    def _coerce_tool_output_mapping(self, raw_output: Mapping[str, object] | object) -> dict[str, object]:
        if hasattr(raw_output, "model_dump"):
            raw_output = raw_output.model_dump()
        if not isinstance(raw_output, Mapping):
            raise TypeError("Tool execution must return a mapping-like payload.")
        return dict(raw_output)

    def _extract_tool_payload(self, payload: dict[str, object]) -> dict[str, object]:
        normalized_payload: dict[str, object] = {}
        explicit_payload = payload.get("payload")
        if isinstance(explicit_payload, Mapping):
            normalized_payload.update(dict(explicit_payload))
        for key, value in payload.items():
            if key not in {"success", "error", "summary", "payload"}:
                normalized_payload[key] = value
        return normalized_payload

    def _normalize_optional_text(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _build_default_tool_summary(
        self,
        invocation: ToolInvocation,
        *,
        success: bool,
        error: str | None,
    ) -> str:
        if success:
            return f"{invocation.tool_name} completed action '{invocation.action}'."
        if error:
            return f"{invocation.tool_name} failed action '{invocation.action}': {error}"
        return f"{invocation.tool_name} did not complete action '{invocation.action}'."

    def _default_execution_summary(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> str:
        return normalized_output.summary or self._build_default_tool_summary(
            invocation,
            success=normalized_output.success,
            error=normalized_output.error,
        )

    def _default_execution_details(
        self,
        task: Task,
        subtask: SubTask,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        details = [
            f"Goal context: {task.goal}",
            f"Objective: {subtask.objective}",
            f"Invocation: {invocation.model_dump()}",
        ]
        if normalized_output.payload:
            details.append(
                f"Normalized payload keys: {', '.join(sorted(normalized_output.payload.keys()))}"
            )
        return details

    def _default_execution_artifacts(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        del normalized_output
        return [f"tool:{invocation.tool_name}:{invocation.action}"]

    def _default_execution_next_actions(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        del invocation
        if normalized_output.success:
            return []
        return ["Inspect the tool output and invocation parameters before retrying."]

    @abstractmethod
    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        """Execute the assigned subtask and return a structured result."""
