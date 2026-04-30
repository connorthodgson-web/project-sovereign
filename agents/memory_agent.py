"""Memory-focused agent implementation."""

from agents.base_agent import BaseAgent
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task


class MemoryAgent(BaseAgent):
    """Handles memory storage, summarization, and retrieval coordination."""

    name = "memory_agent"

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary="Captured the task context for this run without promoting it to durable long-term memory.",
            details=[
                f"Task goal for this run: {' '.join(task.goal.split())}",
                f"Memory objective: {subtask.objective}",
                f"Task title: {task.title}",
            ],
            artifacts=[f"task:{task.id}"],
            next_actions=[],
        )
