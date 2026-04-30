"""Agent adapter layer for local tools and future managed agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from agents.base_agent import BaseAgent
from app.config import Settings, settings
from core.assistant import AssistantLayer
from core.logging import get_logger
from core.models import (
    AgentDescriptor,
    AgentExecutionStatus,
    AgentProvider,
    AgentResult,
    AssistantDecision,
    ChatResponse,
    ExecutionEscalation,
    RequestMode,
    SubTask,
    Task,
    TaskStatus,
)
from core.request_trace import current_request_trace


class AgentAdapter(ABC):
    """Common contract for local and managed agent execution backends."""

    descriptor: AgentDescriptor

    def __init__(self) -> None:
        self.logger = get_logger(__name__)

    @property
    def agent_id(self) -> str:
        return self.descriptor.agent_id

    @property
    def aliases(self) -> list[str]:
        return self.descriptor.aliases

    @property
    def enabled(self) -> bool:
        return self.descriptor.enabled

    def supports_capability(self, capability: str) -> bool:
        return capability in self.descriptor.capabilities

    @abstractmethod
    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        """Execute a structured task/subtask through this adapter."""


class LocalAgentAdapter(AgentAdapter):
    """Wrap an existing local BaseAgent behind the shared adapter contract."""

    def __init__(
        self,
        *,
        descriptor: AgentDescriptor,
        agent: BaseAgent,
    ) -> None:
        super().__init__()
        self.descriptor = descriptor
        self.agent = agent

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        trace = current_request_trace()
        if trace is not None:
            trace.set_metadata("agent_adapter", self.agent_id)
        self.logger.info(
            "AGENT_ADAPTER_START agent_id=%s provider=%s task=%s subtask=%s",
            self.agent_id,
            self.descriptor.provider.value,
            task.id,
            subtask.id,
        )
        result = self.agent.run(task, subtask)
        self.logger.info(
            "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
            self.agent_id,
            result.status.value,
            result.tool_name,
        )
        return result


class ManagedAgentStubAdapter(AgentAdapter):
    """Disabled placeholder for future managed-agent integrations."""

    def __init__(
        self,
        *,
        descriptor: AgentDescriptor,
        required_settings: Iterable[str],
        status_note: str,
        runtime_settings: Settings | None = None,
    ) -> None:
        super().__init__()
        self.descriptor = descriptor
        self.required_settings = tuple(required_settings)
        self.status_note = status_note
        self.runtime_settings = runtime_settings or settings
        self.descriptor.enabled = self._is_enabled()

    def _is_enabled(self) -> bool:
        if not self.required_settings:
            return False
        return all(bool(getattr(self.runtime_settings, field, None)) for field in self.required_settings)

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        missing = [
            field.upper()
            for field in self.required_settings
            if not getattr(self.runtime_settings, field, None)
        ]
        blocker = (
            f"{self.descriptor.display_name} is disabled because missing config: {', '.join(missing)}."
            if missing
            else f"{self.descriptor.display_name} is disabled in this runtime."
        )
        self.logger.info(
            "AGENT_ADAPTER_START agent_id=%s provider=%s task=%s subtask=%s",
            self.agent_id,
            self.descriptor.provider.value,
            task.id,
            subtask.id,
        )
        result = AgentResult(
            subtask_id=subtask.id,
            agent=self.agent_id,
            status=AgentExecutionStatus.BLOCKED,
            summary=blocker,
            tool_name=None,
            details=[
                f"Goal context: {task.goal}",
                f"Objective: {subtask.objective}",
                self.status_note,
            ],
            blockers=[blocker],
            next_actions=["Configure the required integration settings before retrying this managed agent."],
        )
        self.logger.info(
            "AGENT_ADAPTER_END agent_id=%s status=%s tool=%s",
            self.agent_id,
            result.status.value,
            result.tool_name,
        )
        return result


class AssistantAgentAdapter:
    """Metadata-backed adapter for lightweight conversational handling."""

    descriptor = AgentDescriptor(
        agent_id="assistant_agent",
        display_name="Assistant Agent",
        provider=AgentProvider.LOCAL,
        capabilities=["assistant", "self_knowledge", "conversation"],
        cost_tier="low",
        risk_level="low",
        input_schema={"message": "string"},
        output_schema={"response": "chat_response"},
        evidence_schema={"type": "conversation"},
        supports_async=False,
        requires_credentials=False,
        enabled=True,
    )

    def __init__(self, *, assistant_layer: AssistantLayer) -> None:
        self.assistant_layer = assistant_layer

    def build_response(self, message: str, decision: AssistantDecision) -> ChatResponse:
        return self.assistant_layer.build_answer_response(message, decision)


def build_stub_task(*, goal: str, agent_id: str) -> Task:
    """Create a tiny task shell for adapters that need honest blocked responses."""

    return Task(
        goal=goal,
        title=goal,
        description=goal,
        status=TaskStatus.BLOCKED,
        request_mode=RequestMode.ACT,
        escalation_level=ExecutionEscalation.SINGLE_ACTION,
    )

