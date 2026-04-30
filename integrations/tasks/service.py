"""Assistant-facing Google Tasks service with honest readiness behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import Settings, settings
from integrations.tasks.google_provider import GoogleTaskItem, GoogleTasksProvider


@dataclass(frozen=True)
class TasksServiceResult:
    success: bool
    summary: str
    tasks: tuple[GoogleTaskItem, ...] = ()
    blockers: tuple[str, ...] = ()
    created_task: GoogleTaskItem | None = None
    completed_task: GoogleTaskItem | None = None


class GoogleTasksService:
    """Small assistant-safe wrapper around the configured task provider."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        provider: GoogleTasksProvider | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.provider = provider or GoogleTasksProvider(runtime_settings=self.settings)

    def readiness_blockers(self) -> list[str]:
        return self.provider.readiness_blockers()

    def list_tasks(self, *, due_on: datetime | None = None, include_completed: bool = False) -> TasksServiceResult:
        blockers = self.readiness_blockers()
        if blockers:
            return TasksServiceResult(
                success=False,
                summary="Google Tasks access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            tasks = tuple(self.provider.list_tasks(show_completed=include_completed))
        except (RuntimeError, httpx.HTTPError) as exc:
            return TasksServiceResult(
                success=False,
                summary="I couldn't read Google Tasks right now.",
                blockers=(str(exc),),
            )
        if due_on is not None:
            target_date = due_on.astimezone().date()
            tasks = tuple(
                task
                for task in tasks
                if task.due is not None and task.due.astimezone().date() == target_date
            )
        return TasksServiceResult(success=True, summary="Google Tasks loaded.", tasks=tasks)

    def create_task(self, *, title: str, due: datetime | None = None, notes: str | None = None) -> TasksServiceResult:
        if not title.strip():
            return TasksServiceResult(
                success=False,
                summary="Task creation needs a title.",
                blockers=("No task title was parsed from the request.",),
            )
        blockers = self.readiness_blockers()
        if blockers:
            return TasksServiceResult(
                success=False,
                summary="Google Tasks access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            task = self.provider.create_task(title=title.strip(), due=due, notes=notes)
        except (RuntimeError, httpx.HTTPError) as exc:
            return TasksServiceResult(
                success=False,
                summary="I couldn't create that Google Task right now.",
                blockers=(str(exc),),
            )
        return TasksServiceResult(
            success=True,
            summary="Google Task created.",
            tasks=(task,),
            created_task=task,
        )

    def complete_task(self, *, task_id: str, task_list_id: str | None = None) -> TasksServiceResult:
        if not task_id.strip():
            return TasksServiceResult(
                success=False,
                summary="Task completion needs a clear task.",
                blockers=("No task id was resolved from the request.",),
            )
        blockers = self.readiness_blockers()
        if blockers:
            return TasksServiceResult(
                success=False,
                summary="Google Tasks access is not configured in this runtime yet.",
                blockers=tuple(blockers),
            )
        try:
            task = self.provider.complete_task(task_id=task_id, task_list_id=task_list_id)
        except (RuntimeError, httpx.HTTPError, ValueError) as exc:
            return TasksServiceResult(
                success=False,
                summary="I couldn't mark that task done right now.",
                blockers=(str(exc),),
            )
        return TasksServiceResult(
            success=True,
            summary="Google Task completed.",
            tasks=(task,),
            completed_task=task,
        )
