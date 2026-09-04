import os

from task_provider import TaskProvider
from todoist_provider import TodoistProvider

_PROVIDERS: dict[str, type[TaskProvider]] = {
    "todoist": TodoistProvider,
}


def get_task_provider() -> TaskProvider:
    name = os.environ.get("TASK_PROVIDER", "todoist").lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Proveedor desconocido: '{name}'. Opciones: {list(_PROVIDERS)}")
    return cls()
