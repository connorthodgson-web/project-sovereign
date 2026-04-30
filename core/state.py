"""In-memory task state tracking for early development."""

from collections.abc import Iterable

from core.models import AgentResult, ObjectiveState, SubTask, Task, TaskStatus, utcnow


class TaskStateStore:
    """Temporary in-memory task registry.

    Replace this with a durable backend such as Supabase/Postgres before
    relying on long-running operator workflows.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def add_task(self, task: Task) -> Task:
        task.updated_at = utcnow()
        self._tasks[task.id] = task
        return task

    def list_tasks(self) -> list[Task]:
        return sorted(
            self._tasks.values(),
            key=lambda task: task.created_at,
            reverse=True,
        )

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def update_task(self, task: Task) -> Task:
        task.updated_at = utcnow()
        self._tasks[task.id] = task
        return task

    def update_status(self, task_id: str, status: TaskStatus) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.status = status
        return self.update_task(task)

    def set_subtasks(self, task_id: str, subtasks: list[SubTask], *, planner_mode: str) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.subtasks = subtasks
        task.planner_mode = planner_mode
        task.status = TaskStatus.PLANNED
        return self.update_task(task)

    def update_objective_state(self, task_id: str, objective_state: ObjectiveState) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.objective_state = objective_state
        return self.update_task(task)

    def update_subtask(self, task_id: str, subtask: SubTask) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None

        updated = False
        for index, existing in enumerate(task.subtasks):
            if existing.id == subtask.id:
                task.subtasks[index] = subtask
                updated = True
                break

        if not updated:
            task.subtasks.append(subtask)

        return self.update_task(task)

    def add_result(self, task_id: str, result: AgentResult) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.results.append(result)
        return self.update_task(task)

    def replace_results(self, task_id: str, results: list[AgentResult]) -> Task | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.results = results
        return self.update_task(task)

    def seed(self, tasks: Iterable[Task]) -> None:
        for task in tasks:
            self.add_task(task)


task_state_store = TaskStateStore()
