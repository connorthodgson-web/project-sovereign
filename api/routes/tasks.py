"""Task inspection routes."""

from fastapi import APIRouter

from core.models import Task
from core.state import task_state_store


router = APIRouter(tags=["tasks"])


@router.get("/tasks", response_model=list[Task])
def list_tasks() -> list[Task]:
    """List currently tracked tasks."""
    return task_state_store.list_tasks()

