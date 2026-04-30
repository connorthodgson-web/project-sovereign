"""Adapter for controlled workspace-scoped file operations."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from core.logging import get_logger
from core.models import ToolInvocation
from tools.base_tool import BaseTool


class FileToolResult(BaseModel):
    """Structured result for a single workspace file operation."""

    success: bool
    operation: Literal["write", "read", "list"] | str
    workspace_path: str
    requested_path: str | None = None
    normalized_path: str | None = None
    actual_path: str | None = None
    file_path: str | None = None
    content: str | None = None
    content_preview: str | None = None
    listed_entries: list[str] = Field(default_factory=list)
    error: str | None = None


class FileTool(BaseTool):
    """Provides narrow filesystem access limited to the configured workspace."""

    name = "file_tool"
    default_output_dir_name = "created_items"

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.logger = get_logger(__name__)
        root = Path(workspace_root or settings.workspace_root)
        self.workspace_root = root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def supports(self, invocation: ToolInvocation) -> bool:
        return invocation.tool_name == self.name and invocation.action in {"write", "read", "list"}

    def execute(self, invocation: ToolInvocation) -> dict:
        operation = invocation.action
        parameters = invocation.parameters
        if operation == "write":
            result = self.write_file(parameters["path"], parameters.get("content", ""))
        elif operation == "read":
            result = self.read_file(parameters["path"])
        elif operation == "list":
            result = self.list_directory(parameters.get("path", "."))
        else:
            result = FileToolResult(
                success=False,
                operation=operation,
                workspace_path=str(self.workspace_root),
                error=f"Unsupported file operation: {operation}",
            )
        return result.model_dump()

    def write_file(self, path: str, content: str) -> FileToolResult:
        try:
            normalized_path, resolved = self._resolve_write_path(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return FileToolResult(
                success=True,
                operation="write",
                workspace_path=str(self.workspace_root),
                requested_path=path,
                normalized_path=normalized_path,
                actual_path=str(resolved),
                file_path=str(resolved),
                content_preview=self._preview(content),
            )
        except (OSError, ValueError) as exc:
            return FileToolResult(
                success=False,
                operation="write",
                workspace_path=str(self.workspace_root),
                requested_path=path,
                error=str(exc),
            )

    def read_file(self, path: str) -> FileToolResult:
        try:
            normalized_path, resolved = self._resolve_read_path(path)
            content = resolved.read_text(encoding="utf-8")
            return FileToolResult(
                success=True,
                operation="read",
                workspace_path=str(self.workspace_root),
                requested_path=path,
                normalized_path=normalized_path,
                actual_path=str(resolved),
                file_path=str(resolved),
                content=content,
                content_preview=self._preview(content),
            )
        except (OSError, ValueError) as exc:
            return FileToolResult(
                success=False,
                operation="read",
                workspace_path=str(self.workspace_root),
                requested_path=path,
                error=str(exc),
            )

    def list_directory(self, path: str = ".") -> FileToolResult:
        try:
            normalized_path, resolved = self._resolve_workspace_path(path)
            if not resolved.is_dir():
                raise ValueError(f"Path is not a directory inside the workspace: {path}")
            entries = sorted(item.name for item in resolved.iterdir())
            return FileToolResult(
                success=True,
                operation="list",
                workspace_path=str(self.workspace_root),
                requested_path=path,
                normalized_path=normalized_path,
                actual_path=str(resolved),
                file_path=str(resolved),
                listed_entries=entries,
            )
        except (OSError, ValueError) as exc:
            return FileToolResult(
                success=False,
                operation="list",
                workspace_path=str(self.workspace_root),
                requested_path=path,
                error=str(exc),
            )

    def _resolve_workspace_path(self, path: str) -> tuple[str, Path]:
        normalized = self._normalize_user_path(path)
        candidate = (self.workspace_root / normalized).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"Path escapes the configured workspace: {path}") from exc
        self.logger.info(
            "FILE_PATH_RESOLVED input=%r normalized=%r final=%s",
            path,
            normalized,
            candidate,
        )
        return normalized, candidate

    def _resolve_write_path(self, path: str) -> tuple[str, Path]:
        normalized = self._default_output_path(self._normalize_user_path(path))
        return self._resolve_workspace_path(str(normalized))

    def _resolve_read_path(self, path: str) -> tuple[str, Path]:
        normalized, resolved = self._resolve_workspace_path(path)
        if resolved.exists():
            return normalized, resolved
        defaulted_path = self._default_output_path(self._normalize_user_path(path))
        if str(defaulted_path) == normalized:
            return normalized, resolved
        return self._resolve_workspace_path(str(defaulted_path))

    def _default_output_path(self, path: str) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        if candidate == Path("."):
            return candidate
        if candidate.parent != Path("."):
            return candidate
        return Path(self.default_output_dir_name) / candidate

    def _normalize_user_path(self, path: str) -> str:
        raw = (path or ".").strip().strip("\"'")
        if not raw:
            return "."
        normalized = raw.replace("\\", "/")
        if normalized in {".", "./"}:
            return "."
        workspace_name = self.workspace_root.name.lower()
        lowered = normalized.lower()
        prefixes = (f"{workspace_name}/", "workspace/")
        stripped_prefix = True
        while stripped_prefix:
            stripped_prefix = False
            for prefix in prefixes:
                if lowered.startswith(prefix):
                    normalized = normalized[len(prefix) :]
                    lowered = normalized.lower()
                    stripped_prefix = True
                    break
        if normalized.startswith("/"):
            absolute = Path(normalized).resolve()
            try:
                relative = absolute.relative_to(self.workspace_root)
            except ValueError as exc:
                raise ValueError(f"Path escapes the configured workspace: {path}") from exc
            normalized = relative.as_posix()
        candidate = Path(normalized)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(self.workspace_root)
            except ValueError as exc:
                raise ValueError(f"Path escapes the configured workspace: {path}") from exc
            return relative.as_posix() or "."
        parts = [part for part in candidate.parts if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise ValueError(f"Path escapes the configured workspace: {path}")
        return Path(*parts).as_posix() if parts else "."

    def _preview(self, content: str, *, limit: int = 120) -> str:
        normalized = " ".join(content.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."
