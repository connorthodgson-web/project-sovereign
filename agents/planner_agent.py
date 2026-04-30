"""Planning-focused agent implementation."""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence
from core.planner import Planner


class PlannerAgent(BaseAgent):
    """Owns structured task planning and candidate-agent selection."""

    name = "planner_agent"

    def __init__(self, *, planner: Planner | None = None) -> None:
        self.planner = planner or Planner()

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        subtasks, planner_mode = self.planner.create_plan(
            task.goal,
            escalation_level=task.escalation_level,
        )
        candidate_map = {
            item.id: self.planner.candidate_agent_ids_for_subtask(item)
            for item in subtasks
        }
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Created a structured {planner_mode} plan with {len(subtasks)} subtasks.",
            tool_name="planning_agent",
            details=[
                f"Goal context: {task.goal}",
                f"Planning objective: {subtask.objective}",
                f"Planner mode: {planner_mode}",
            ],
            artifacts=[f"plan:{task.id}"],
            evidence=[
                ToolEvidence(
                    tool_name="planning_agent",
                    summary="Structured plan created for the current goal.",
                    payload={
                        "planner_mode": planner_mode,
                        "subtasks": [item.model_dump() for item in subtasks],
                        "candidate_agents": candidate_map,
                    },
                )
            ],
            blockers=[],
            next_actions=[],
        )
