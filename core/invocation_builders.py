"""Lightweight invocation builders for deterministic planning."""

from __future__ import annotations

import os
import re
import shlex
from abc import ABC
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from core.browser_requests import extract_first_url, extract_obvious_browser_request
from core.models import FileEvidence, ToolInvocation
from core.state import task_state_store


class BuiltInvocation(BaseModel):
    """Planner-facing metadata for a concrete executable tool invocation."""

    invocation: ToolInvocation
    execution_agent: str = "coding_agent"
    execution_title: str
    execution_description: str
    execution_objective: str
    review_title: str = "Review tool execution result"
    review_description: str = "Verify the tool execution produced a real result before stopping."
    review_objective: str


class InvocationBuilder(Protocol):
    """Small protocol for extensible deterministic invocation building."""

    def can_build(self, goal: str) -> bool:
        """Return whether this builder can translate the goal into a tool invocation."""

    def build(self, goal: str) -> BuiltInvocation:
        """Build invocation metadata for the provided goal."""


class BaseBuiltInvocationBuilder(ABC):
    """Shared metadata helpers for deterministic invocation builders."""

    execution_agent = "coding_agent"

    def _build_execution_metadata(
        self,
        goal: str,
        *,
        invocation: ToolInvocation,
        execution_title: str,
        execution_description: str,
        review_title: str,
        review_description: str,
    ) -> BuiltInvocation:
        return BuiltInvocation(
            invocation=invocation,
            execution_agent=self.execution_agent,
            execution_title=execution_title,
            execution_description=execution_description,
            execution_objective=f"Execute the supported tool operation for: {goal}",
            review_title=review_title,
            review_description=review_description,
            review_objective=f"Review the tool execution result for: {goal}",
        )


class FileToolInvocationBuilder(BaseBuiltInvocationBuilder):
    """Deterministic builder for simple workspace file operations."""

    def can_build(self, goal: str) -> bool:
        lowered = goal.lower()
        has_explicit_path = bool(re.search(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+", goal))
        has_file_action = re.search(r"\b(create|write|read|open|make)\b", lowered) is not None
        return any(phrase in lowered for phrase in ("list files", "list the files", "show files", "list workspace")) or (
            has_file_action
            and ("file" in lowered or has_explicit_path)
        ) or (has_explicit_path and "list" in lowered) or re.search(
            r"\b(?:now\s+)?create one (?:called|named)\b",
            lowered,
        ) is not None

    def build(self, goal: str) -> BuiltInvocation:
        lowered = goal.lower()
        if any(phrase in lowered for phrase in ("list files", "list the files", "show files", "list workspace")) or re.search(
            r"\b(?:list|show)\s+[A-Za-z0-9_./\\-]+$",
            lowered,
        ):
            invocation = ToolInvocation(
                tool_name="file_tool",
                action="list",
                parameters={"path": self._extract_directory_path(goal)},
            )
        elif (re.search(r"\b(read|open)\b", lowered) or "show the file" in lowered) and not re.search(
            r"\b(save|write|create)\b",
            lowered,
        ):
            invocation = ToolInvocation(
                tool_name="file_tool",
                action="read",
                parameters={"path": self._extract_file_path(goal)},
            )
        else:
            invocation = ToolInvocation(
                tool_name="file_tool",
                action="write",
                parameters={
                    "path": self._extract_file_path(goal),
                    "content": self._infer_file_content(goal),
                },
            )

        return self._build_execution_metadata(
            goal,
            invocation=invocation,
            execution_title="Execute workspace file task",
            execution_description="Use the constrained workspace file path for a simple create, read, or list operation.",
            review_title="Review workspace file result",
            review_description="Verify the file operation produced a real result before stopping.",
        )

    def _extract_file_path(self, goal: str) -> str:
        cleaned_goal = goal.replace('"', "").replace("'", "")
        lowered = goal.lower()

        explicit_path_match = re.search(
            r"\b(?:at|in|to|called|named)\s+([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+)\b",
            cleaned_goal,
        )
        if explicit_path_match:
            return explicit_path_match.group(1)

        named_without_extension = re.search(
            r"\b(?:called|named)\s+([A-Za-z0-9_./\\-]+)\b",
            cleaned_goal,
        )
        if named_without_extension:
            candidate = named_without_extension.group(1).strip(".,:;")
            inferred_extension = self._infer_extension(lowered) or self._infer_extension_from_recent_file_context()
            if inferred_extension and "." not in Path(candidate).name:
                return f"{candidate}{inferred_extension}"
            return candidate

        if "readme" in lowered:
            return "README.md"

        for token in cleaned_goal.split():
            normalized = token.strip(".,:;")
            if "." in normalized and not normalized.lower().endswith(("task.", "task", "goal")):
                suffix = Path(normalized).suffix
                if suffix:
                    return normalized
        inferred_extension = self._infer_extension(lowered)
        if inferred_extension == ".py":
            return "script.py"
        if inferred_extension is None:
            inferred_extension = self._infer_extension_from_recent_file_context()
        if inferred_extension:
            return f"workspace{inferred_extension}"
        return "workspace.txt"

    def _infer_file_content(self, goal: str) -> str:
        stripped = goal.strip()
        for pattern in (
            r"\bexplaining that\s+(.+)$",
            r"\bthat says\s+(.+)$",
            r"\bwith content\s+(.+)$",
            r"\bsaying\s+(.+)$",
        ):
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip().strip("\"'").rstrip(".")
        if '"' in stripped:
            parts = stripped.split('"')
            for candidate in parts[1::2]:
                if "." not in candidate:
                    return candidate
        if "'" in stripped:
            parts = stripped.split("'")
            for candidate in parts[1::2]:
                if "." not in candidate:
                    return candidate

        lowered = goal.lower()
        if "readme" in lowered:
            return "# Project Sovereign\n\nA simple README created by Project Sovereign.\n"
        if "python file" in lowered or lowered.endswith(".py"):
            return 'print("Hello from Project Sovereign!")\n'
        if "greeting" in lowered:
            return "Hello from Project Sovereign!"
        if "hello" in lowered:
            return "Hello!"
        return "Created by Project Sovereign."

    def _extract_directory_path(self, goal: str) -> str:
        cleaned_goal = goal.replace('"', "").replace("'", "")
        match = re.search(
            r"\b(?:list|show)\s+(?:the\s+)?(?:files\s+)?(?:in|inside|under)\s+([A-Za-z0-9_./\\-]+)\b",
            cleaned_goal,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip(".,:;")
        direct_match = re.search(
            r"\b(?:list|show)\s+([A-Za-z0-9_./\\-]+)\b",
            cleaned_goal,
            flags=re.IGNORECASE,
        )
        if direct_match:
            return direct_match.group(1).strip(".,:;")
        return "."

    def _infer_extension(self, lowered_goal: str) -> str | None:
        if "python file" in lowered_goal or "python script" in lowered_goal:
            return ".py"
        if "readme" in lowered_goal:
            return ".md"
        if "json file" in lowered_goal:
            return ".json"
        if "markdown file" in lowered_goal or "md file" in lowered_goal:
            return ".md"
        if "text file" in lowered_goal or "txt file" in lowered_goal:
            return ".txt"
        return None

    def _infer_extension_from_recent_file_context(self) -> str | None:
        for task in task_state_store.list_tasks():
            for result in reversed(task.results):
                for evidence in result.evidence:
                    if isinstance(evidence, FileEvidence) and evidence.file_path:
                        suffix = Path(evidence.file_path).suffix
                        if suffix:
                            return suffix
        return None


class RuntimeToolInvocationBuilder(BaseBuiltInvocationBuilder):
    """Deterministic builder for very small local runtime goals."""

    explicit_prefixes = ("run ", "execute ", "shell ", "command ")

    def can_build(self, goal: str) -> bool:
        lowered = " ".join(goal.lower().split())
        return lowered.startswith(self.explicit_prefixes) or (
            "current directory" in lowered
            and any(keyword in lowered for keyword in ("contents", "list", "show", "check"))
            and "shell" in lowered
        )

    def build(self, goal: str) -> BuiltInvocation:
        command = self._extract_command(goal)
        invocation = ToolInvocation(
            tool_name="runtime_tool",
            action="run",
            parameters={"command": command},
        )
        return self._build_execution_metadata(
            goal,
            invocation=invocation,
            execution_title="Execute local runtime command",
            execution_description="Run one narrow local shell command inside the configured workspace.",
            review_title="Review runtime command result",
            review_description="Verify the runtime command actually completed and returned concrete output metadata.",
        )

    def _extract_command(self, goal: str) -> str:
        normalized = " ".join(goal.strip().split())
        lowered = normalized.lower()
        for prefix in self.explicit_prefixes:
            if lowered.startswith(prefix):
                return self._strip_wrapping_quotes(normalized[len(prefix) :].strip())
        if (
            "current directory" in lowered
            and any(keyword in lowered for keyword in ("contents", "list", "show", "check"))
            and "shell" in lowered
        ):
            return "dir" if os.name == "nt" else "ls"
        return shlex.quote(normalized)

    def _strip_wrapping_quotes(self, value: str) -> str:
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
            and value.count(value[0]) == 2
        ):
            return value[1:-1].strip()
        return value


class SlackMessagingInvocationBuilder(BaseBuiltInvocationBuilder):
    """Deterministic builder for explicit outbound Slack requests."""

    execution_agent = "communications_agent"

    def can_build(self, goal: str) -> bool:
        lowered = " ".join(goal.lower().split())
        if "slack" not in lowered:
            return False
        return any(term in lowered for term in ("send", "message", "dm", "direct message", "notify"))

    def build(self, goal: str) -> BuiltInvocation:
        action = self._infer_action(goal)
        invocation = ToolInvocation(
            tool_name="slack_messaging_tool",
            action=action,
            parameters=self._build_parameters(goal, action=action),
        )
        review_title = "Review Slack delivery result"
        return self._build_execution_metadata(
            goal,
            invocation=invocation,
            execution_title="Send outbound Slack message",
            execution_description="Use the live Slack outbound path to send a channel message or DM with provider evidence.",
            review_title=review_title,
            review_description="Verify Slack delivery returned real provider evidence before calling it complete.",
        )

    def _build_parameters(self, goal: str, *, action: str) -> dict[str, str]:
        parameters: dict[str, str] = {
            "message_text": self._extract_message_text(goal),
        }
        channel_id = self._extract_channel_id(goal)
        channel_name = self._extract_channel_name(goal)
        user_id = self._extract_user_id(goal)
        if action == "send_channel_message":
            if channel_id:
                parameters["channel_id"] = channel_id
                parameters["target"] = channel_id
            elif channel_name:
                parameters["channel"] = channel_name
                parameters["target"] = channel_name
        else:
            if channel_id and channel_id.upper().startswith("D"):
                parameters["channel_id"] = channel_id
                parameters["target"] = channel_id
            elif user_id:
                parameters["user_id"] = user_id
                parameters["target"] = user_id
        return parameters

    def _infer_action(self, goal: str) -> str:
        lowered = goal.lower()
        if " dm " in f" {lowered} " or "direct message" in lowered:
            return "send_dm"
        return "send_channel_message"

    def _extract_message_text(self, goal: str) -> str:
        quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', goal)
        for pair in quoted:
            candidate = next((item for item in pair if item), "").strip()
            if candidate:
                return candidate
        lowered = goal.lower()
        markers = (" saying ", " that says ", " with the message ", " with message ", ": ")
        for marker in markers:
            index = lowered.find(marker)
            if index >= 0:
                candidate = goal[index + len(marker) :].strip(" .")
                if candidate:
                    return candidate.strip("\"'")
        return ""

    def _extract_channel_name(self, goal: str) -> str | None:
        match = re.search(r"(#[A-Za-z0-9._-]+)", goal)
        if match:
            return match.group(1)
        return None

    def _extract_channel_id(self, goal: str) -> str | None:
        match = re.search(r"\b([CDG][A-Z0-9]{2,})\b", goal)
        if match:
            return match.group(1)
        return None

    def _extract_user_id(self, goal: str) -> str | None:
        mention = re.search(r"<@([A-Z0-9]{2,})>", goal)
        if mention:
            return mention.group(1)
        match = re.search(r"\b(U[A-Z0-9]{2,})\b", goal)
        if match:
            return match.group(1)
        return None


class BrowserToolInvocationBuilder(BaseBuiltInvocationBuilder):
    """Deterministic builder for direct URL-based browser actions."""

    execution_agent = "browser_agent"

    def can_build(self, goal: str) -> bool:
        return extract_obvious_browser_request(goal) is not None

    def build(self, goal: str) -> BuiltInvocation:
        browser_request = extract_obvious_browser_request(goal)
        url = browser_request.url if browser_request is not None else ""
        invocation = ToolInvocation(
            tool_name="browser_tool",
            action=browser_request.action if browser_request is not None else self._infer_action(goal),
            parameters={
                "url": url,
                "objective": goal,
                "require_screenshot": "true" if self._explicitly_requests_screenshot(goal) else "false",
            },
        )
        return self._build_execution_metadata(
            goal,
            invocation=invocation,
            execution_title="Open page in browser",
            execution_description="Use the live browser path to open the requested URL, capture page details, and keep evidence.",
            review_title="Review browser evidence",
            review_description="Verify the browser run produced real page evidence before calling it complete.",
        )

    def _infer_action(self, goal: str) -> str:
        lowered = goal.lower()
        if "inspect" in lowered:
            return "inspect"
        if "summarize" in lowered:
            return "summarize"
        return "open"

    def _extract_url(self, goal: str) -> str | None:
        return extract_first_url(goal)

    def _explicitly_requests_screenshot(self, goal: str) -> bool:
        lowered = goal.lower()
        return any(term in lowered for term in ("screenshot", "screen shot", "capture an image", "visual proof"))
