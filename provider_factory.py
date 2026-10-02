"""
Factory de proveedores: el unico sitio donde se elige que implementacion concreta
hay detras de cada interfaz (TaskProvider, UsageProvider...).

Cada funcion importa SOLO los modulos de su dominio, dentro de la propia funcion.
Asi, quien pide un lector de consumo no carga Todoist (ni httpx), y un proveedor
que no se puede importar no tumba a los de otro dominio. Para anadir un proveedor:
su clase en su modulo y una linea en el registro de su dominio.
"""

import os

from task_provider import TaskProvider
from usage_provider import UsageProvider


def _task_providers() -> dict[str, type[TaskProvider]]:
    from todoist_provider import TodoistProvider

    return {"todoist": TodoistProvider}


def get_task_provider() -> TaskProvider:
    providers = _task_providers()
    name = os.environ.get("TASK_PROVIDER", "todoist").lower()
    cls = providers.get(name)
    if cls is None:
        raise ValueError(f"Proveedor desconocido: '{name}'. Opciones: {list(providers)}")
    return cls()


def _usage_providers() -> dict[str, type[UsageProvider]]:
    from anthropic_usage_provider import AnthropicApiUsageProvider, ClaudeCodeUsageProvider

    return {cls.name: cls for cls in (AnthropicApiUsageProvider, ClaudeCodeUsageProvider)}


def get_usage_provider(name: str) -> UsageProvider:
    """Lector de consumo del cliente de IA indicado (ver usage_provider.py)."""
    providers = _usage_providers()
    cls = providers.get(name)
    if cls is None:
        raise ValueError(
            f"Proveedor de consumo desconocido: '{name}'. Opciones: {list(providers)}"
        )
    return cls()
