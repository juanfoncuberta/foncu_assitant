from abc import ABC, abstractmethod


class TaskProvider(ABC):

    @abstractmethod
    def create_task(
        self,
        content: str,
        due_string: str | None = None,
        priority: int = 1,
        project_id: str | None = None,
    ) -> dict: ...

    @abstractmethod
    def list_tasks(self, project_id: str | None = None) -> list[dict]: ...

    @abstractmethod
    def update_task_priority(self, task_id: str, priority: int) -> dict: ...

    @abstractmethod
    def close_task(self, task_id: str) -> None: ...

    @abstractmethod
    def delete_task(self, task_id: str) -> None: ...

    @abstractmethod
    def create_project(self, name: str) -> dict: ...

    @abstractmethod
    def list_projects(self) -> list[dict]: ...
