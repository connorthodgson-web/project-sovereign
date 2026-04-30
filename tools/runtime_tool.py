"""Minimal local runtime adapter for simple workspace-scoped shell execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import settings
from core.models import ToolInvocation
from tools.base_tool import BaseTool


class RuntimeToolResult(BaseModel):
    """Structured result for one narrow local runtime command."""

    success: bool
    command: str
    workspace_path: str
    exit_code: int | None = None
    stdout_preview: str | None = None
    stderr_preview: str | None = None
    timed_out: bool = False
    summary: str | None = None
    error: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class RuntimeTool(BaseTool):
    """Runs one simple shell command inside the configured workspace."""

    name = "runtime_tool"

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        timeout_seconds: float = 5.0,
        preview_limit: int = 240,
    ) -> None:
        root = Path(workspace_root or settings.workspace_root)
        self.workspace_root = root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.preview_limit = preview_limit

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name and invocation.action == "run"

    def execute(self, invocation: ToolInvocation) -> dict:
        command = (invocation.parameters.get("command") or "").strip()
        if not command:
            return RuntimeToolResult(
                success=False,
                command=command,
                workspace_path=str(self.workspace_root),
                summary="Runtime command could not be executed.",
                error="Runtime invocation is missing the required 'command' parameter.",
                payload={"command": command},
            ).model_dump()

        try:
            completed = subprocess.run(
                command,
                cwd=self.workspace_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            stdout_preview = self._preview(completed.stdout)
            stderr_preview = self._preview(completed.stderr)
            success = completed.returncode == 0
            summary = (
                f"Executed runtime command '{command}' successfully."
                if success
                else f"Runtime command '{command}' exited with code {completed.returncode}."
            )
            return RuntimeToolResult(
                success=success,
                command=command,
                workspace_path=str(self.workspace_root),
                exit_code=completed.returncode,
                stdout_preview=stdout_preview,
                stderr_preview=stderr_preview,
                summary=summary,
                error=None if success else self._build_error(completed.returncode, stderr_preview),
                payload={
                    "command": command,
                    "workspace_path": str(self.workspace_root),
                    "exit_code": completed.returncode,
                    "stdout_preview": stdout_preview,
                    "stderr_preview": stderr_preview,
                    "timed_out": False,
                },
            ).model_dump()
        except subprocess.TimeoutExpired as exc:
            stdout_preview = self._preview(exc.stdout)
            stderr_preview = self._preview(exc.stderr)
            return RuntimeToolResult(
                success=False,
                command=command,
                workspace_path=str(self.workspace_root),
                timed_out=True,
                stdout_preview=stdout_preview,
                stderr_preview=stderr_preview,
                summary=f"Runtime command '{command}' timed out.",
                error=f"Command timed out after {self.timeout_seconds} seconds.",
                payload={
                    "command": command,
                    "workspace_path": str(self.workspace_root),
                    "exit_code": None,
                    "stdout_preview": stdout_preview,
                    "stderr_preview": stderr_preview,
                    "timed_out": True,
                },
            ).model_dump()
        except OSError as exc:
            return RuntimeToolResult(
                success=False,
                command=command,
                workspace_path=str(self.workspace_root),
                summary=f"Runtime command '{command}' could not be started.",
                error=str(exc),
                payload={
                    "command": command,
                    "workspace_path": str(self.workspace_root),
                    "exit_code": None,
                    "stdout_preview": None,
                    "stderr_preview": None,
                    "timed_out": False,
                },
            ).model_dump()

    def _preview(self, content: str | bytes | None) -> str | None:
        if content is None:
            return None
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        normalized = " ".join(content.split())
        if not normalized:
            return None
        if len(normalized) <= self.preview_limit:
            return normalized
        return f"{normalized[: self.preview_limit - 3]}..."

    def _build_error(self, exit_code: int, stderr_preview: str | None) -> str:
        if stderr_preview:
            return f"Command exited with code {exit_code}: {stderr_preview}"
        return f"Command exited with code {exit_code}."
