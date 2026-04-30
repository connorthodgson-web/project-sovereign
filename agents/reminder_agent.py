"""Reminder scheduling agent implementation."""

from __future__ import annotations

from datetime import datetime

from agents.base_agent import BaseAgent
from app.config import settings
from core.interaction_context import get_interaction_context
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence
from integrations.openrouter_client import OpenRouterClient
from integrations.reminders.adapter import APSchedulerReminderAdapter
from integrations.reminders.contracts import ReminderAdapter, ReminderRequest
from integrations.reminders.parsing import parse_one_time_reminder_request_with_fallback


class ReminderSchedulerAgent(BaseAgent):
    """Owns one-time reminder parsing, scheduling, and durable follow-up setup."""

    name = "reminder_scheduler_agent"

    def __init__(
        self,
        *,
        reminder_adapter: ReminderAdapter | None = None,
        openrouter_client: OpenRouterClient | None = None,
    ) -> None:
        self.reminder_adapter = reminder_adapter or APSchedulerReminderAdapter()
        self.openrouter_client = openrouter_client or OpenRouterClient()

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        interaction = get_interaction_context()
        if interaction is None or not interaction.channel_id:
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="Reminder scheduling needs a live delivery target before I can promise a proactive follow-up.",
                details=[
                    f"Goal context: {task.goal}",
                    f"Reminder objective: {subtask.objective}",
                ],
                blockers=[
                    "No delivery context was available for this reminder request.",
                ],
                next_actions=[
                    "Run this request from a configured Slack conversation so I know where to send the reminder.",
                ],
            )

        parse_outcome = parse_one_time_reminder_request_with_fallback(
            task.goal,
            timezone_name=settings.scheduler_timezone,
            openrouter_client=self.openrouter_client,
        )
        parsed = parse_outcome.parsed
        if parsed is None:
            failure_reason = parse_outcome.failure_reason or (
                "The reminder time and summary could not be parsed from the current message."
            )
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="I recognized a reminder request, but I couldn't confidently schedule a one-time reminder from it.",
                details=[
                    f"Goal context: {task.goal}",
                    f"Reminder objective: {subtask.objective}",
                ],
                blockers=[
                    failure_reason,
                ],
                next_actions=[
                    "Try a one-time format like 'remind me in 10 mins to check the deployment' or 'remind me at 6 pm that class starts soon'.",
                ],
            )

        result = self.reminder_adapter.schedule(
            ReminderRequest(
                summary=parsed.summary,
                schedule=parsed.deliver_at.isoformat(),
                delivery_channel="slack",
                recipient=interaction.user_id,
                channel=interaction.channel_id,
                metadata={
                    "source": self.name,
                    "channel_id": interaction.channel_id,
                    "user_id": interaction.user_id or "",
                    "schedule_phrase": parsed.schedule_phrase,
                    "delivery_text": f"Reminder: {parsed.summary}",
                },
            )
        )
        if not result.success or not result.reminder_id:
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary=result.summary,
                details=[
                    f"Goal context: {task.goal}",
                    f"Reminder objective: {subtask.objective}",
                    f"Delivery channel: {interaction.channel_id}",
                ],
                blockers=result.blockers,
                next_actions=[
                    "Fix the reminder scheduler or outbound Slack readiness blockers and retry.",
                ],
            )

        scheduled_for = _format_human_time(parsed.deliver_at)
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Scheduled a reminder for {scheduled_for} to {parsed.summary}.",
            tool_name="reminder_scheduler",
            details=[
                f"Goal context: {task.goal}",
                f"Reminder objective: {subtask.objective}",
                f"Reminder id: {result.reminder_id}",
                f"Scheduled for: {parsed.deliver_at.isoformat()}",
                f"Delivery target: Slack channel {interaction.channel_id}",
            ],
            artifacts=[f"reminder:{result.reminder_id}"],
            evidence=[
                ToolEvidence(
                    tool_name="reminder_scheduler",
                    summary="Scheduled a one-time reminder with outbound Slack delivery.",
                    payload={
                        "reminder_id": result.reminder_id,
                        "summary": parsed.summary,
                        "deliver_at": parsed.deliver_at.isoformat(),
                        "delivery_channel": "slack",
                        "channel_id": interaction.channel_id,
                        "parser": parsed.parser,
                        "confidence": parsed.confidence,
                    },
                )
            ],
            blockers=[],
            next_actions=[],
        )


def _format_human_time(deliver_at: datetime) -> str:
    local_time = deliver_at.astimezone()
    return local_time.strftime("%I:%M %p").lstrip("0")
