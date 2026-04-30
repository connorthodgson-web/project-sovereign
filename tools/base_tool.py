"""Base abstraction for execution-oriented tool adapters."""

from abc import ABC, abstractmethod

from core.models import ToolInvocation


class BaseTool(ABC):
    """Base class for tools that wrap external execution capabilities."""

    name: str = "base_tool"

    def supports(self, invocation: ToolInvocation) -> bool:
        """Return whether this tool can handle the provided invocation."""

        return invocation.tool_name == self.name

    @abstractmethod
    def execute(self, invocation: ToolInvocation) -> dict:
        """Invoke the underlying capability and return structured output."""
