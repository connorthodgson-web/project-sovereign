"""Lightweight registry for bounded tool lookup and execution."""

from __future__ import annotations

from core.models import ToolInvocation
from tools.base_tool import BaseTool
from tools.browser_tool import BrowserTool
from tools.file_tool import FileTool
from tools.runtime_tool import RuntimeTool
from tools.slack_messaging_tool import SlackMessagingTool
from tools.web_search_tool import WebSearchTool


class ToolRegistry:
    """Register tools by name and provide a narrow execution boundary."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        self._tools[tool.name] = tool
        return tool

    def get(self, tool_name: str) -> BaseTool | None:
        return self._tools.get(tool_name)

    def list_tool_names(self) -> list[str]:
        return sorted(self._tools)

    def supports_invocation(self, invocation: ToolInvocation) -> bool:
        tool = self.get(invocation.tool_name)
        if tool is None:
            return False
        return tool.supports(invocation)

    def execute(self, invocation: ToolInvocation) -> dict:
        tool = self.get(invocation.tool_name)
        if tool is None:
            raise ValueError(f"Unsupported tool invocation: {invocation.tool_name}")
        if not tool.supports(invocation):
            raise ValueError(
                f"Tool '{invocation.tool_name}' does not support action '{invocation.action}'."
            )
        return tool.execute(invocation)


def build_default_tool_registry(
    *,
    browser_tool: BrowserTool | None = None,
    file_tool: FileTool | None = None,
    runtime_tool: RuntimeTool | None = None,
    slack_messaging_tool: SlackMessagingTool | None = None,
    web_search_tool: WebSearchTool | None = None,
) -> ToolRegistry:
    """Create the bounded registry used by the current execution slice."""

    registry = ToolRegistry()
    registry.register(browser_tool or BrowserTool())
    registry.register(file_tool or FileTool())
    registry.register(runtime_tool or RuntimeTool())
    registry.register(slack_messaging_tool or SlackMessagingTool())
    registry.register(web_search_tool or WebSearchTool())
    return registry
