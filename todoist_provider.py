import todoist_client
from task_provider import TaskProvider


class TodoistProvider(TaskProvider):

    def create_task(
        self,
        content: str,
        due_string: str | None = None,
        priority: int = 1,
        project_id: str | None = None,
    ) -> dict:
        return todoist_client.create_task(content, due_string, priority, project_id)

    def list_tasks(self, project_id: str | None = None) -> list[dict]:
        return todoist_client.list_tasks(project_id)

    def update_task_priority(self, task_id: str, priority: int) -> dict:
        return todoist_client.update_task_priority(task_id, priority)

    def close_task(self, task_id: str) -> None:
        todoist_client.close_task(task_id)

    def delete_task(self, task_id: str) -> None:
        todoist_client.delete_task(task_id)

    def create_project(self, name: str) -> dict:
        return todoist_client.create_project(name)

    def list_projects(self) -> list[dict]:
        return todoist_client.list_projects()

    def resolve_project(self, project_name: str) -> dict:
        """Returns the project with the given name, creating it if it doesn't exist."""
        projects = self.list_projects()
        project = next(
            (p for p in projects if isinstance(p, dict) and p["name"].lower() == project_name.lower()),
            None,
        )
        if project is None:
            project = self.create_project(project_name)
        return project
