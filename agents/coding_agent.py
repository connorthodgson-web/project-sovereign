"""Coding-focused agent implementation."""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.models import (
    AgentExecutionStatus,
    AgentResult,
    FileEvidence,
    NormalizedToolOutput,
    SubTask,
    Task,
    ToolEvidence,
    ToolInvocation,
)
from tools.file_tool import FileTool
from tools.registry import ToolRegistry, build_default_tool_registry


class CodingAgent(BaseAgent):
    """Handles implementation-oriented tasks such as code generation or edits."""

    name = "coding_agent"
    supported_tool_names = frozenset({"file_tool", "runtime_tool"})

    def __init__(
        self,
        file_tool: FileTool | None = None,
        tool_registry: ToolRegistry | None = None,
        supported_tool_names: frozenset[str] | None = None,
    ) -> None:
        self.tool_registry = tool_registry or build_default_tool_registry(file_tool=file_tool)
        self.supported_tool_names = supported_tool_names or frozenset({"file_tool", "runtime_tool"})

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        invocation = subtask.tool_invocation
        if invocation is None:
            focus_areas = self._infer_focus_areas(task.goal)
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.PLANNED,
                summary="Implementation work was mapped into concrete next steps, but no executable tool invocation was attached to this subtask.",
                details=[
                    f"Goal context: {task.goal}",
                    f"Coding objective: {subtask.objective}",
                    f"Suggested implementation areas: {', '.join(focus_areas)}",
                ],
                artifacts=[f"implementation-outline:{task.id}"],
                next_actions=[
                    "Attach a supported tool invocation during planning before executing this subtask.",
                ],
            )

        return self.execute_tool_invocation(
            task,
            subtask,
            tool_registry=self.tool_registry,
            validate_invocation=self._validate_invocation,
            build_evidence=self._build_execution_evidence,
            build_summary=self._build_summary,
            build_details=self._build_execution_details,
            build_artifacts=self._build_execution_artifacts,
            build_next_actions=self._build_execution_next_actions,
        )

    def _validate_invocation(self, invocation: ToolInvocation) -> str | None:
        if self.tool_registry.get(invocation.tool_name) is None:
            return f"Unsupported tool invocation: {invocation.tool_name}"
        if invocation.tool_name == "file_tool":
            if invocation.action not in {"write", "read", "list"}:
                return f"Unsupported file action: {invocation.action}"
            if invocation.action in {"write", "read"} and not invocation.parameters.get("path"):
                return "File invocation is missing the required 'path' parameter."
        if invocation.tool_name == "runtime_tool":
            if invocation.action != "run":
                return f"Unsupported runtime action: {invocation.action}"
            if not invocation.parameters.get("command"):
                return "Runtime invocation is missing the required 'command' parameter."
        return None

    def _build_execution_evidence(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[FileEvidence | ToolEvidence]:
        if invocation.tool_name == "file_tool":
            payload = normalized_output.payload
            return [
                FileEvidence(
                    tool_name=invocation.tool_name,
                    requested_path=self._payload_text(payload, "requested_path"),
                    normalized_path=self._payload_text(payload, "normalized_path"),
                    workspace_root=self._payload_text(payload, "workspace_path"),
                    actual_path=self._payload_text(payload, "actual_path") or self._payload_text(payload, "file_path"),
                    file_path=self._payload_text(payload, "file_path"),
                    operation=self._file_operation(payload, invocation),
                    content_preview=self._payload_text(payload, "content_preview"),
                    listed_entries=self._payload_list(payload, "listed_entries"),
                    verification_notes=[],
                )
            ]
        return [
            ToolEvidence(
                tool_name=invocation.tool_name,
                summary=normalized_output.summary,
                payload=normalized_output.payload,
                verification_notes=[],
            )
        ]

    def _build_execution_details(
        self,
        task: Task,
        subtask: SubTask,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        details = [
            f"Goal context: {task.goal}",
            f"Coding objective: {subtask.objective}",
            f"Invocation: {invocation.model_dump()}",
        ]
        payload = normalized_output.payload
        if invocation.tool_name == "file_tool":
            workspace_root = self._payload_text(payload, "workspace_path")
            operation = self._file_operation(payload, invocation)
            if workspace_root:
                details.append(f"Workspace root: {workspace_root}")
            details.append(f"File operation: {operation}")
            file_path = self._payload_text(payload, "file_path")
            if file_path:
                details.append(f"Resolved path: {file_path}")
            listed_entries = self._payload_list(payload, "listed_entries")
            if listed_entries:
                details.append(f"Listed {len(listed_entries)} entries.")
            content_preview = self._payload_text(payload, "content_preview")
            if content_preview:
                details.append(f"Content preview: {content_preview}")
            return details

        if normalized_output.payload:
            if invocation.tool_name == "runtime_tool":
                command = self._payload_text(payload, "command")
                exit_code = payload.get("exit_code")
                if command:
                    details.append(f"Runtime command: {command}")
                if "workspace_path" in payload:
                    details.append(f"Workspace root: {self._payload_text(payload, 'workspace_path')}")
                if exit_code is not None:
                    details.append(f"Exit code: {exit_code}")
                stdout_preview = self._payload_text(payload, "stdout_preview")
                stderr_preview = self._payload_text(payload, "stderr_preview")
                if stdout_preview:
                    details.append(f"Stdout preview: {stdout_preview}")
                if stderr_preview:
                    details.append(f"Stderr preview: {stderr_preview}")
                if payload.get("timed_out"):
                    details.append("Command timed out before completion.")
                return details
            details.append(
                f"Normalized payload keys: {', '.join(sorted(normalized_output.payload.keys()))}"
            )
        return details

    def _build_execution_artifacts(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        if invocation.tool_name == "file_tool":
            return [f"workspace:{self._file_operation(normalized_output.payload, invocation)}"]
        if invocation.tool_name == "runtime_tool":
            command = self._payload_text(normalized_output.payload, "command") or invocation.parameters.get("command", "")
            return [f"runtime:run:{command}"]
        return super()._default_execution_artifacts(invocation, normalized_output)

    def _build_execution_next_actions(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> list[str]:
        if normalized_output.success:
            return []
        if invocation.tool_name == "file_tool":
            return ["Retry with a valid path inside the configured workspace."]
        if invocation.tool_name == "runtime_tool":
            return ["Retry with a simple command that can run inside the configured workspace."]
        return super()._default_execution_next_actions(invocation, normalized_output)

    def _build_summary(
        self,
        invocation: ToolInvocation,
        normalized_output: NormalizedToolOutput,
    ) -> str:
        if invocation.tool_name != "file_tool":
            if invocation.tool_name == "runtime_tool":
                if normalized_output.summary:
                    return normalized_output.summary
                command = self._payload_text(normalized_output.payload, "command") or invocation.parameters.get("command", "the requested command")
                exit_code = normalized_output.payload.get("exit_code")
                if normalized_output.success:
                    return f"Executed runtime command '{command}' successfully."
                if normalized_output.payload.get("timed_out"):
                    return f"Runtime command '{command}' timed out."
                if exit_code is not None:
                    return f"Runtime command '{command}' exited with code {exit_code}."
            return normalized_output.summary or self._build_default_tool_summary(
                invocation,
                success=normalized_output.success,
                error=normalized_output.error,
            )

        operation = self._file_operation(normalized_output.payload, invocation)
        file_path = self._payload_text(normalized_output.payload, "file_path")
        if normalized_output.success:
            if operation == "write":
                return f"Created workspace file at {file_path}."
            if operation == "read":
                return f"Read workspace file at {file_path}."
            return f"Listed workspace directory at {file_path}."
        return f"Unable to {operation} inside the workspace: {normalized_output.error}"

    def _file_operation(
        self,
        payload: dict[str, object],
        invocation: ToolInvocation,
    ) -> str:
        operation = payload.get("operation")
        return str(operation) if operation is not None else invocation.action

    def _payload_text(self, payload: dict[str, object], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        text = str(value)
        return text or None

    def _payload_list(self, payload: dict[str, object], key: str) -> list[str]:
        value = payload.get(key)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    def _infer_focus_areas(self, goal: str) -> list[str]:
        lowered = goal.lower()
        areas = ["core orchestration", "api routes"]
        if any(keyword in lowered for keyword in ("agent", "router", "planner", "supervisor")):
            areas.append("agent pipeline")
        if any(keyword in lowered for keyword in ("ui", "frontend", "browser", "qa")):
            areas.append("browser-facing execution adapter")
        return areas
