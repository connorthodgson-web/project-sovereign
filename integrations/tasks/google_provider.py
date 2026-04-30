"""Compatibility provider over the Google Tasks client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import Settings, settings
from integrations.tasks.google_client import GoogleTasksClient, NormalizedGoogleTask


@dataclass(frozen=True)
class GoogleTaskItem:
    task_id: str
    title: str
    status: str
    task_list_id: str = "@default"
    due: datetime | None = None
    notes: str | None = None
    updated: str | None = None
    completed: str | None = None
    position: str | None = None
    source: str = "google_tasks"


class GoogleTasksProvider:
    """Google Tasks provider used by assistant-facing services."""

    def __init__(
        self,
        *,
        runtime_settings: Settings | None = None,
        client: GoogleTasksClient | None = None,
    ) -> None:
        self.settings = runtime_settings or settings
        self.client = client or GoogleTasksClient(runtime_settings=self.settings)

    def readiness_blockers(self) -> list[str]:
        return list(self.client.readiness().blockers)

    def list_tasks(self, *, show_completed: bool = False, max_results: int = 20) -> list[GoogleTaskItem]:
        return [
            _to_google_task_item(item)
            for item in self.client.list_tasks(show_completed=show_completed, max_results=max_results)
        ]

    def create_task(self, *, title: str, due: datetime | None = None, notes: str | None = None) -> GoogleTaskItem:
        return _to_google_task_item(self.client.create_task(title=title, due=due, notes=notes))

    def complete_task(self, *, task_id: str, task_list_id: str | None = None) -> GoogleTaskItem:
        return _to_google_task_item(self.client.complete_task(task_id=task_id, task_list_id=task_list_id))


def _to_google_task_item(task: NormalizedGoogleTask) -> GoogleTaskItem:
    return GoogleTaskItem(
        task_id=task.task_id,
        title=task.title,
        status=task.status,
        task_list_id=task.task_list_id,
        due=task.due_datetime,
        notes=task.notes,
        updated=task.updated,
        completed=task.completed,
        position=task.position,
        source=task.source,
    )
