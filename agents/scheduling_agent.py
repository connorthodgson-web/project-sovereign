"""Scheduling / Personal Operations agent for reminders and calendar work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from agents.base_agent import BaseAgent
from agents.reminder_agent import ReminderSchedulerAgent
from app.config import settings
from core.models import AgentExecutionStatus, AgentResult, SubTask, Task, ToolEvidence
from integrations.calendar.parsing import (
    CalendarEventDraft,
    CalendarEventUpdateDraft,
    CalendarQuery,
    parse_calendar_event_reference,
    parse_calendar_event_request,
    parse_calendar_event_update_request,
    parse_calendar_query,
)
from integrations.calendar.service import CalendarService, CalendarServiceResult
from integrations.reminders.contracts import ReminderAdapter
from integrations.tasks.google_provider import GoogleTaskItem
from integrations.tasks.service import GoogleTasksService, TasksServiceResult


@dataclass(frozen=True)
class SchedulingTargetResolution:
    """Scheduling Agent interpretation of which existing object a request means."""

    event_id: str | None
    summary: str
    matches: tuple[Any, ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True)
class SchedulingUpdateInterpretation:
    """Scheduling Agent interpretation of an existing-event update."""

    update: CalendarEventUpdateDraft | None
    target: SchedulingTargetResolution
    question: str | None = None
    missing_field: str | None = None


@dataclass(frozen=True)
class TaskTargetResolution:
    """Scheduling Agent interpretation of which existing Google Task a request means."""

    task_id: str | None
    summary: str
    task_list_id: str | None = None
    matches: tuple[Any, ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True)
class TaskRequestInterpretation:
    """Scheduling Agent interpretation of a Google Tasks request."""

    action: str | None
    title: str | None = None
    due: datetime | None = None
    due_label: str | None = None
    target: TaskTargetResolution | None = None
    question: str | None = None
    missing_field: str | None = None


class SchedulingPersonalOpsAgent(BaseAgent):
    """Specialist under Personal Ops for calendar, reminders, and time-based requests.

    The CEO/Supervisor remains the user-facing operator. This agent owns the
    scheduling tool boundary: calendar reads/writes, reminder delegation,
    confirmation-safe event updates/deletes, and natural scheduling evidence.
    """

    name = "scheduling_agent"

    def __init__(
        self,
        *,
        calendar_service: CalendarService | None = None,
        tasks_service: GoogleTasksService | None = None,
        reminder_adapter: ReminderAdapter | None = None,
    ) -> None:
        self.calendar_service = calendar_service or CalendarService(runtime_settings=settings)
        self.tasks_service = tasks_service or GoogleTasksService(runtime_settings=settings)
        self.reminder_agent = ReminderSchedulerAgent(reminder_adapter=reminder_adapter)

    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        lowered = f"{task.goal} {subtask.objective}".lower()
        if self.is_task_request(lowered):
            interpretation = self.interpret_task_request(task.goal)
            if interpretation.action == "list":
                due_on = interpretation.due if interpretation.due_label == "today" else None
                result = self.list_tasks(due_on=due_on)
                return self._tasks_agent_result(result, subtask_id=subtask.id, action="list")
            if interpretation.action == "create" and interpretation.title:
                result = self.create_task(title=interpretation.title, due=interpretation.due)
                return self._tasks_agent_result(result, subtask_id=subtask.id, action="create")
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary=interpretation.question or "Which task do you mean?",
                tool_name="google_tasks",
                blockers=[interpretation.question or "The task request needs a clearer target."],
            )
        if "calendar" in lowered or "event" in lowered or "appointment" in lowered or "meeting" in lowered:
            query = self.interpret_calendar_query(task.goal)
            if query is None:
                return AgentResult(
                    subtask_id=subtask.id,
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary="Scheduling agent needs a clearer calendar range or event action.",
                    tool_name="google_calendar",
                    blockers=["Calendar request did not include a supported date range or event action."],
                    next_actions=["Ask for a range like today, tomorrow, this week, or a specific calendar event action."],
                )
            start = query.day
            end = query.end_day or query.day
            if query.end_day is None:
                result = self.calendar_service.events_for_day(target_day=query.day)
            else:
                result = self.calendar_service.list_events(start=start, end=end)
            if not result.success:
                return AgentResult(
                    subtask_id=subtask.id,
                    agent=self.name,
                    status=AgentExecutionStatus.BLOCKED,
                    summary=result.summary,
                    tool_name="google_calendar",
                    blockers=list(result.blockers),
                )
            return AgentResult(
                subtask_id=subtask.id,
                agent=self.name,
                status=AgentExecutionStatus.COMPLETED,
                summary=f"Loaded {len(result.events)} Google Calendar event(s) for {query.label}.",
                tool_name="google_calendar",
                evidence=[
                    ToolEvidence(
                        tool_name="google_calendar",
                        summary="Read Google Calendar events through the Scheduling / Personal Ops Agent.",
                        payload={
                            "source": "google_calendar",
                            "range_label": query.label,
                            "events": [
                                {
                                    "event_id": event.event_id,
                                    "calendar_id": event.calendar_id,
                                    "title": event.title,
                                    "start": event.start.isoformat(),
                                    "end": event.end.isoformat(),
                                    "timezone": event.timezone,
                                    "location": event.location,
                                    "description_snippet": event.description_snippet,
                                    "attendees_count": event.attendees_count,
                                    "htmlLink": event.html_link,
                                }
                                for event in result.events
                            ],
                        },
                    )
                ],
            )
        if "remind me" in lowered or "reminder" in lowered:
            delegated = self.reminder_agent.run(task, subtask)
            return delegated.model_copy(update={"agent": self.name})
        return AgentResult(
            subtask_id=subtask.id,
            agent=self.name,
            status=AgentExecutionStatus.BLOCKED,
            summary="Scheduling agent is reserved for calendar, reminder, and time-based personal operations.",
            blockers=["No scheduling or personal-ops intent was detected."],
        )

    def read_calendar(self, message: str, *, subtask_id: str = "scheduling-calendar-read") -> AgentResult:
        query = self.interpret_calendar_query(message)
        if query is None:
            return AgentResult(
                subtask_id=subtask_id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary="I need a clearer calendar range before I can read your calendar.",
                tool_name="google_calendar",
                blockers=["Calendar request did not include a supported range like today, tomorrow, this week, or next event."],
            )
        start = query.day
        end = query.end_day or query.day
        result = self.calendar_service.events_for_day(target_day=query.day) if query.end_day is None else self.calendar_service.list_events(start=start, end=end)
        if not result.success:
            return AgentResult(
                subtask_id=subtask_id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary=result.summary,
                tool_name="google_calendar",
                blockers=list(result.blockers),
            )
        return AgentResult(
            subtask_id=subtask_id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Loaded {len(result.events)} Google Calendar event(s) for {query.label}.",
            tool_name="google_calendar",
            evidence=[
                ToolEvidence(
                    tool_name="google_calendar",
                    summary="Read Google Calendar events through the Scheduling / Personal Ops Agent.",
                    payload={
                        "source": "google_calendar",
                        "range_label": query.label,
                        "events": [
                            {
                                "event_id": event.event_id,
                                "calendar_id": event.calendar_id,
                                "title": event.title,
                                "start": event.start.isoformat(),
                                "end": event.end.isoformat(),
                                "timezone": event.timezone,
                                "location": event.location,
                                "description_snippet": event.description_snippet,
                                "attendees_count": event.attendees_count,
                                "htmlLink": event.html_link,
                            }
                            for event in result.events
                        ],
                    },
                )
            ],
        )

    def interpret_calendar_query(self, message: str) -> CalendarQuery | None:
        """Interpret calendar read intent.

        The supervisor should only decide that scheduling is relevant. This
        specialist owns the supported calendar-read meanings and delegates
        reusable date parsing to the calendar parsing helpers.
        """

        return parse_calendar_query(message, timezone_name=settings.scheduler_timezone)

    def is_calendar_read_request(self, message: str) -> bool:
        return self.interpret_calendar_query(message) is not None

    def interpret_calendar_create(self, message: str) -> CalendarEventDraft | None:
        return parse_calendar_event_request(
            message,
            timezone_name=settings.scheduler_timezone,
        )

    def is_calendar_create_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        if self.interpret_calendar_create(lowered) is not None:
            return True
        if (
            lowered.startswith(("add ", "create ", "schedule ", "put "))
            and "reminder" not in lowered
            and not re.search(r"\b(tasks?|task list|to-do|todo|gmail|email)\b", lowered)
            and re.search(r"\b(?:at|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", lowered)
        ):
            return True
        return (
            any(lowered.startswith(prefix) for prefix in ("add an event", "add event", "create an event", "schedule an event", "put an event", "put event"))
            or (
                lowered.startswith(("add ", "create ", "schedule ", "put "))
                and any(
                    token in lowered
                    for token in (
                        " appointment",
                        " meeting",
                        " tomorrow",
                        " today",
                        " monday",
                        " tuesday",
                        " wednesday",
                        " thursday",
                        " friday",
                        " saturday",
                        " sunday",
                    )
                )
                and "reminder" not in lowered
            )
        )

    def is_calendar_delete_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        return "reminder" not in lowered and any(word in lowered for word in ("delete", "remove", "cancel")) and any(
            token in lowered for token in ("calendar", "event", "appointment", "meeting")
        )

    def is_calendar_update_request(self, message: str) -> bool:
        lowered = message.lower().strip()
        return "reminder" not in lowered and any(word in lowered for word in ("move", "update", "change", "reschedule")) and any(
            token in lowered for token in ("calendar", "event", "appointment", "meeting")
        )

    def resolve_calendar_target(self, message: str, operator_context: Any) -> SchedulingTargetResolution:
        event_id = parse_calendar_event_reference(message)
        if event_id:
            return SchedulingTargetResolution(
                event_id=event_id,
                summary=self._calendar_target_summary(operator_context, event_id),
            )

        if self._contains_referent_phrase(message):
            referents = operator_context.resolve_recent_referents(
                object_type="calendar_event",
                pronoun_text=message,
            )
            if len(referents) > 1:
                return SchedulingTargetResolution(
                    event_id=None,
                    summary="that event",
                    matches=tuple(referents),
                    ambiguous=True,
                )
            if len(referents) == 1 and referents[0].object_id:
                return SchedulingTargetResolution(
                    event_id=referents[0].object_id,
                    summary=referents[0].summary or "that event",
                    matches=tuple(referents),
                )

        title_matches = self._match_recent_calendar_titles(message, operator_context)
        if len(title_matches) > 1:
            return SchedulingTargetResolution(
                event_id=None,
                summary="that event",
                matches=tuple(title_matches),
                ambiguous=True,
            )
        if len(title_matches) == 1 and title_matches[0].object_id:
            return SchedulingTargetResolution(
                event_id=title_matches[0].object_id,
                summary=title_matches[0].summary or "that event",
                matches=tuple(title_matches),
            )

        return SchedulingTargetResolution(event_id=None, summary="that event")

    def interpret_calendar_update(self, message: str, operator_context: Any) -> SchedulingUpdateInterpretation:
        target = self.resolve_calendar_target(message, operator_context)
        if target.ambiguous:
            return SchedulingUpdateInterpretation(
                update=None,
                target=target,
                question="Which event do you mean?",
                missing_field="calendar_event_id",
            )

        direct = None
        if self._has_concrete_update_fields(message):
            direct = parse_calendar_event_update_request(
                message,
                timezone_name=settings.scheduler_timezone,
            )
        if direct is not None:
            direct = self._preserve_referent_time_context(direct, message, operator_context)
            return SchedulingUpdateInterpretation(update=direct, target=target)

        if target.event_id is None:
            return SchedulingUpdateInterpretation(
                update=None,
                target=target,
                question="Which calendar event should I update?",
                missing_field="calendar_event_id",
            )

        schedule = self.extract_schedule_after_update(message)
        if schedule:
            update = parse_calendar_event_update_request(
                f"move event {target.event_id} to {schedule}",
                timezone_name=settings.scheduler_timezone,
            )
            if update is not None:
                update = self._preserve_referent_time_context(update, f"move event {target.event_id} to {schedule}", operator_context)
                return SchedulingUpdateInterpretation(update=update, target=target)
            if self._contains_day(schedule):
                return SchedulingUpdateInterpretation(
                    update=None,
                    target=target,
                    question=f"What time should I use for {schedule.rstrip(' .')}?",
                    missing_field="calendar_update_fields",
                )

        return SchedulingUpdateInterpretation(
            update=None,
            target=target,
            question="I'm not sure what you want to change about the event. What should I update?",
            missing_field="calendar_update_fields",
        )

    def extract_schedule_after_update(self, message: str) -> str | None:
        match = re.search(r"\b(?:to|for)\s+(?P<schedule>.+)$", message, flags=re.IGNORECASE)
        if match:
            return match.group("schedule").strip()
        cleaned = re.sub(
            r"\b(?:event|appointment|meeting)\s+(?:id\s*)?[A-Za-z0-9_\-@.]+",
            "",
            message,
            flags=re.IGNORECASE,
        )
        fallback = re.search(
            r"\b(?P<schedule>(?:today|tomorrow|this\s+\w+|next\s+\w+|\w+day)(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            cleaned,
            flags=re.IGNORECASE,
        )
        return fallback.group("schedule").strip() if fallback else None

    def create_calendar_event(self, draft: CalendarEventDraft, *, send_updates: bool = False) -> CalendarServiceResult:
        return self.calendar_service.create_event(
            title=draft.title,
            start=draft.start,
            end=draft.end,
            description=draft.description,
            attendees=list(draft.attendees),
            send_updates=send_updates,
        )

    def update_calendar_event(
        self,
        *,
        event_id: str,
        updates: dict[str, Any],
        send_updates: bool = False,
    ) -> CalendarServiceResult:
        return self.calendar_service.update_event(
            event_id=event_id,
            updates=updates,
            send_updates=send_updates,
        )

    def delete_calendar_event(self, *, event_id: str, send_updates: bool = False) -> CalendarServiceResult:
        return self.calendar_service.delete_event(
            event_id=event_id,
            send_updates=send_updates,
        )

    def interpret_task_request(self, message: str, operator_context: Any | None = None) -> TaskRequestInterpretation:
        normalized = " ".join(message.strip().split())
        lowered = normalized.lower()
        created = self._parse_task_create(normalized)
        if created is not None:
            title, due, due_label = created
            if not title:
                return TaskRequestInterpretation(
                    action="create",
                    question="What should I add to your tasks?",
                    missing_field="google_task_title",
                )
            return TaskRequestInterpretation(
                action="create",
                title=title,
                due=due,
                due_label=due_label,
            )
        if self._looks_like_task_completion(lowered):
            target = self.resolve_task_target(normalized, operator_context)
            if target.ambiguous:
                return TaskRequestInterpretation(
                    action="complete",
                    target=target,
                    question="Which task do you mean?",
                    missing_field="google_task_id",
                )
            if target.task_id is None:
                return TaskRequestInterpretation(
                    action="complete",
                    target=target,
                    question="Which task should I mark done?",
                    missing_field="google_task_id",
                )
            return TaskRequestInterpretation(action="complete", target=target)
        if self._looks_like_task_list(lowered):
            due = self._parse_task_due(normalized)
            return TaskRequestInterpretation(
                action="list",
                due=due[0],
                due_label=due[1],
            )
        return TaskRequestInterpretation(action=None, question="I need a clearer task action.")

    def is_task_request(self, message: str, operator_context: Any | None = None) -> bool:
        lowered = " ".join(message.lower().strip().split())
        if self._parse_task_create(message) is not None or self._looks_like_task_list(lowered):
            return True
        if self._looks_like_task_completion(lowered):
            if any(token in lowered for token in ("task", "tasks", "to-do", "todo")):
                return True
            if operator_context is not None:
                if operator_context.resolve_recent_referents(object_type="google_task", pronoun_text=message):
                    return True
            return self._looks_like_task_completion_reference(lowered)
        return False

    def list_tasks(self, *, due_on: datetime | None = None) -> TasksServiceResult:
        return self.tasks_service.list_tasks(due_on=due_on)

    def create_task(self, *, title: str, due: datetime | None = None, notes: str | None = None) -> TasksServiceResult:
        return self.tasks_service.create_task(title=title, due=due, notes=notes)

    def complete_task(self, *, task_id: str, task_list_id: str | None = None) -> TasksServiceResult:
        return self.tasks_service.complete_task(task_id=task_id, task_list_id=task_list_id)

    def resolve_task_target(self, message: str, operator_context: Any | None) -> TaskTargetResolution:
        if operator_context is None:
            return TaskTargetResolution(task_id=None, summary="that task")
        direct = self._parse_task_id_reference(message)
        if direct:
            return TaskTargetResolution(task_id=direct, summary=self._task_target_summary(operator_context, direct))

        if self._contains_referent_phrase(message):
            referents = operator_context.resolve_recent_referents(
                object_type="google_task",
                pronoun_text=message,
            )
            if len(referents) > 1:
                return TaskTargetResolution(
                    task_id=None,
                    summary="that task",
                    matches=tuple(referents),
                    ambiguous=True,
                )
            if len(referents) == 1 and referents[0].object_id:
                return TaskTargetResolution(
                    task_id=referents[0].object_id,
                    summary=referents[0].summary or "that task",
                    task_list_id=str((referents[0].metadata or {}).get("task_list_id") or "") or None,
                    matches=tuple(referents),
                )

        title_matches = self._match_recent_task_titles(message, operator_context)
        if len(title_matches) > 1:
            return TaskTargetResolution(
                task_id=None,
                summary="that task",
                matches=tuple(title_matches),
                ambiguous=True,
            )
        if len(title_matches) == 1 and title_matches[0].object_id:
            return TaskTargetResolution(
                task_id=title_matches[0].object_id,
                summary=title_matches[0].summary or "that task",
                task_list_id=str((title_matches[0].metadata or {}).get("task_list_id") or "") or None,
                matches=tuple(title_matches),
            )
        return TaskTargetResolution(task_id=None, summary="that task")

    def _tasks_agent_result(self, result: TasksServiceResult, *, subtask_id: str, action: str) -> AgentResult:
        if not result.success:
            return AgentResult(
                subtask_id=subtask_id,
                agent=self.name,
                status=AgentExecutionStatus.BLOCKED,
                summary=result.summary,
                tool_name="google_tasks",
                blockers=list(result.blockers),
            )
        return AgentResult(
            subtask_id=subtask_id,
            agent=self.name,
            status=AgentExecutionStatus.COMPLETED,
            summary=f"Google Tasks {action} completed.",
            tool_name="google_tasks",
            evidence=[
                ToolEvidence(
                    tool_name="google_tasks",
                    summary="Used Google Tasks through the Scheduling / Personal Ops Agent.",
                    payload={
                        "source": "google_tasks",
                        "tasks": [self._task_payload(item) for item in result.tasks],
                        "created_task_id": result.created_task.task_id if result.created_task else None,
                        "completed_task_id": result.completed_task.task_id if result.completed_task else None,
                    },
                )
            ],
        )

    def _parse_task_create(self, message: str) -> tuple[str, datetime | None, str | None] | None:
        text = " ".join(message.strip().split())
        lowered = text.lower()
        if any(token in lowered for token in ("calendar", "reminder", "gmail", "email")):
            return None
        match = re.match(
            r"^(?:please\s+)?(?:add|create|make|put)\s+(?:a\s+)?(?:task\s+)?(?P<title>.+?)\s+(?:to|on)\s+(?:my\s+)?(?:tasks|task list|to-do list|todo list)(?P<due_phrase>\s+(?:for|due|on)\s+.+)?$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            match = re.match(
                r"^(?:please\s+)?(?:add|create|make|put)\s+(?:a\s+)?task\s+(?:to\s+)?(?P<title>.+)$",
                text,
                flags=re.IGNORECASE,
            )
        if not match:
            return None
        title = match.group("title").strip(" .'\"")
        due_phrase = (match.groupdict().get("due_phrase") or "").strip()
        due, label = self._parse_task_due(f"{title} {due_phrase}".strip())
        title = self._strip_task_due_phrase(title)
        title = re.sub(r"^(?:called|named)\s+", "", title, flags=re.IGNORECASE).strip(" .'\"")
        return title, due, label

    def _looks_like_task_list(self, lowered: str) -> bool:
        if any(token in lowered for token in ("calendar", "reminder", "gmail", "email")):
            return False
        return bool(
            re.search(r"\b(?:what|which|show|list|read)\b", lowered)
            and re.search(r"\b(?:tasks|task list|to-do|todo)\b", lowered)
        )

    def _looks_like_task_completion(self, lowered: str) -> bool:
        if any(token in lowered for token in ("calendar", "event", "reminder", "gmail", "email")):
            return False
        return bool(
            re.search(r"^(?:please\s+)?mark\s+.+\s+(?:done|complete|completed|finished)$", lowered)
            or re.search(r"^(?:please\s+)?complete\s+.+", lowered)
            or re.search(r"^(?:please\s+)?check\s+off\s+.+", lowered)
            or re.search(r"^(?:please\s+)?finish\s+(?:that|this|it|the\s+)?(?:one|task|first|second|third|1st|2nd|3rd)?$", lowered)
            or re.search(r"^(?:please\s+)?done\s+with\s+.+", lowered)
        )

    def _looks_like_task_completion_reference(self, lowered: str) -> bool:
        return bool(
            re.search(r"^(?:please\s+)?mark\s+(?:that|this|it|the\s+.+)\s+(?:done|complete|completed|finished)$", lowered)
            or re.search(r"^(?:please\s+)?complete\s+(?:that|this|it|the\s+(?:first|second|third|1st|2nd|3rd|last)\s+one)$", lowered)
            or re.search(r"^(?:please\s+)?check\s+off\s+(?:that|this|it|the\s+.+)$", lowered)
            or re.search(r"^(?:please\s+)?finish\s+(?:that|this|it|the\s+(?:first|second|third|1st|2nd|3rd|last)\s+one)$", lowered)
            or re.search(r"^(?:please\s+)?done\s+with\s+.+", lowered)
        )

    def _parse_task_due(self, text: str) -> tuple[datetime | None, str | None]:
        lowered = text.lower()
        now = datetime.now(ZoneInfo(settings.scheduler_timezone)).astimezone()
        if re.search(r"\bdue\s+today\b|\btoday\b", lowered):
            return now.replace(hour=0, minute=0, second=0, microsecond=0), "today"
        if re.search(r"\bdue\s+tomorrow\b|\btomorrow\b", lowered):
            target = now + timedelta(days=1)
            return target.replace(hour=0, minute=0, second=0, microsecond=0), "tomorrow"
        weekday_match = re.search(
            r"\b(?:due\s+)?(?:on\s+)?(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            lowered,
        )
        if weekday_match:
            names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            target_weekday = names.index(weekday_match.group("weekday"))
            days_ahead = (target_weekday - now.weekday()) % 7 or 7
            target = now + timedelta(days=days_ahead)
            return target.replace(hour=0, minute=0, second=0, microsecond=0), weekday_match.group("weekday")
        return None, None

    def _strip_task_due_phrase(self, title: str) -> str:
        cleaned = re.sub(
            r"\s+(?:for\s+|due\s+|on\s+)?(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            "",
            title,
            flags=re.IGNORECASE,
        )
        return cleaned.strip(" .")

    def _parse_task_id_reference(self, message: str) -> str | None:
        match = re.search(r"\btask\s+(?:id\s*)?(?P<id>[A-Za-z0-9_\-@.]{2,})\b", message, flags=re.IGNORECASE)
        return match.group("id") if match else None

    def _task_target_summary(self, operator_context: Any, task_id: str | None) -> str:
        if not task_id:
            return "that task"
        state = operator_context.get_short_term_state()
        for item in state.last_actionable_objects:
            if item.object_type == "google_task" and item.object_id == task_id:
                return item.summary
        return "that task"

    def _match_recent_task_titles(self, message: str, operator_context: Any) -> list[Any]:
        state = operator_context.get_short_term_state()
        candidates = [item for item in state.last_actionable_objects if item.object_type == "google_task"]
        if not candidates:
            return []
        needle = self._task_title_match_text(message)
        if not needle:
            return []
        needle_tokens = set(re.findall(r"[a-z0-9]+", needle.lower()))
        if not needle_tokens:
            return []
        matches = []
        for item in candidates:
            summary_tokens = set(re.findall(r"[a-z0-9]+", item.summary.lower()))
            if needle_tokens and needle_tokens.issubset(summary_tokens):
                matches.append(item)
        return matches[:3]

    def _task_title_match_text(self, message: str) -> str:
        text = " ".join(message.strip().split())
        text = re.sub(r"^(?:please\s+)?(?:mark|complete|finish)\s+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:done|complete|completed|finished|task|tasks|the|my)\b", "", text, flags=re.IGNORECASE)
        return text.strip(" .")

    def _task_payload(self, task: GoogleTaskItem) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "task_list_id": task.task_list_id,
            "title": task.title,
            "status": task.status,
            "due": task.due.isoformat() if task.due else None,
            "notes": task.notes,
            "updated": task.updated,
            "completed": task.completed,
            "position": task.position,
            "source": task.source,
        }

    def _preserve_referent_time_context(
        self,
        update: CalendarEventUpdateDraft,
        message: str,
        operator_context: Any,
    ) -> CalendarEventUpdateDraft:
        if "start" not in update.updates or "end" not in update.updates:
            return update
        if self._contains_explicit_day(message):
            return update
        referent = self._recent_event_by_id(operator_context, update.event_id)
        if referent is None:
            return update
        metadata = getattr(referent, "metadata", {}) or {}
        original_start_text = metadata.get("start")
        original_end_text = metadata.get("end")
        if not isinstance(original_start_text, str) or not isinstance(original_end_text, str):
            return update
        try:
            original_start = datetime.fromisoformat(original_start_text).astimezone()
            original_end = datetime.fromisoformat(original_end_text).astimezone()
            parsed_start = datetime.fromisoformat(str(update.updates["start"]["dateTime"])).astimezone()
        except (TypeError, ValueError, KeyError):
            return update
        duration = max(original_end - original_start, timedelta(minutes=1))
        hour = parsed_start.hour
        if not re.search(r"\b(?:am|pm)\b", message, flags=re.IGNORECASE) and original_start.hour >= 12 and hour < 12:
            hour += 12
        preserved_start = original_start.replace(
            hour=hour,
            minute=parsed_start.minute,
            second=parsed_start.second,
            microsecond=0,
        )
        preserved_end = preserved_start + duration
        updates = dict(update.updates)
        updates["start"] = {"dateTime": preserved_start.isoformat()}
        updates["end"] = {"dateTime": preserved_end.isoformat()}
        description = f"time to {preserved_start.strftime('%I:%M %p').lstrip('0')}"
        return CalendarEventUpdateDraft(
            event_id=update.event_id,
            updates=updates,
            description=description,
        )

    def _recent_event_by_id(self, operator_context: Any, event_id: str) -> Any | None:
        state = operator_context.get_short_term_state()
        for item in state.last_actionable_objects:
            if item.object_type == "calendar_event" and item.object_id == event_id:
                return item
        return None

    def _calendar_target_summary(self, operator_context: Any, event_id: str | None) -> str:
        if not event_id:
            return "that event"
        referent = self._recent_event_by_id(operator_context, event_id)
        return referent.summary if referent is not None else "that event"

    def _match_recent_calendar_titles(self, message: str, operator_context: Any) -> list[Any]:
        state = operator_context.get_short_term_state()
        candidates = [item for item in state.last_actionable_objects if item.object_type == "calendar_event"]
        if not candidates:
            return []
        needle = self._title_match_text(message)
        if not needle:
            return []
        needle_tokens = set(re.findall(r"[a-z0-9]+", needle.lower()))
        if not needle_tokens:
            return []
        matches = []
        for item in candidates:
            summary_tokens = set(re.findall(r"[a-z0-9]+", item.summary.lower()))
            if needle_tokens and needle_tokens.issubset(summary_tokens):
                matches.append(item)
        return matches[:3]

    def _title_match_text(self, message: str) -> str:
        text = " ".join(message.strip().split())
        text = re.sub(r"^(?:please\s+)?(?:delete|remove|cancel|move|update|change|reschedule)\s+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:calendar|event|appointment|meeting)\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:to|for)\s+.+$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(today|tomorrow|this\s+\w+|next\s+\w+|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", "", text, flags=re.IGNORECASE)
        return text.strip(" .")

    def _has_concrete_update_fields(self, message: str) -> bool:
        normalized = " ".join(message.strip().split())
        without_reference = re.sub(
            r"\b(?:event|appointment|meeting)\s+(?:id\s*)?[A-Za-z0-9_\-@.]+",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        return bool(
            re.search(r"\b(?:title|name|rename|called|summary|location|description|notes?)\s+(?:to|as)\b", without_reference, re.IGNORECASE)
            or re.search(r"\b(?:move|reschedule|change|update)\b.+?\bto\s+", without_reference, re.IGNORECASE)
        )

    def _contains_day(self, text: str) -> bool:
        return bool(
            re.search(
                r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next\s+\w+|this\s+\w+)\b",
                text,
                re.IGNORECASE,
            )
        )

    def _contains_explicit_day(self, text: str) -> bool:
        return self._contains_day(text)

    def _contains_referent_phrase(self, text: str) -> bool:
        lowered = f" {' '.join(text.lower().split())} "
        return any(
            token in lowered
            for token in (
                " it ",
                " that ",
                " this ",
                " the last one ",
                " last one ",
                " first ",
                " second ",
                " third ",
                " 1st ",
                " 2nd ",
                " 3rd ",
            )
        )


def format_event_time(value: datetime) -> str:
    return value.astimezone().strftime("%B %d at %I:%M %p").replace(" 0", " ").lstrip("0")


def looks_like_calendar_read_request(message: str) -> bool:
    """Shared lightweight CEO check: scheduling agent owns the actual interpretation."""

    return parse_calendar_query(message, timezone_name=settings.scheduler_timezone) is not None


def looks_like_google_tasks_request(message: str) -> bool:
    """Shared lightweight CEO check: Scheduling Agent owns actual task interpretation."""

    return SchedulingPersonalOpsAgent().is_task_request(message)
