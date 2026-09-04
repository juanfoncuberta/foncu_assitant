"""
Cliente para la API REST de Todoist.

IMPORTANTE — escala de prioridad (al revés que la app):
  API priority=4  →  app "P1" (urgente)
  API priority=3  →  app "P2" (alta)
  API priority=2  →  app "P3" (media)
  API priority=1  →  app "P4" (sin prioridad)
Siempre se trabaja con valores de API (1-4) en este módulo.
"""

import logging
import os

import httpx

BASE_URL = "https://api.todoist.com/api/v1"

logger = logging.getLogger(__name__)


def _headers() -> dict:
    token = os.environ["TODOIST_API_TOKEN"]
    return {"Authorization": f"Bearer {token}"}


def _parse_json_object(resp: httpx.Response) -> dict:
    data = resp.json()
    if not isinstance(data, dict):
        logger.error("Respuesta inesperada de Todoist (status=%s): %s", resp.status_code, resp.text)
        raise ValueError(f"Todoist devolvió un tipo inesperado ({type(data).__name__}): {resp.text}")
    return data


def create_task(
    content: str,
    due_string: str | None = None,
    priority: int = 1,
    project_id: str | None = None,
) -> dict:
    payload: dict = {"content": content, "priority": priority}
    if due_string:
        payload["due_string"] = due_string
    if project_id:
        payload["project_id"] = project_id
    with httpx.Client() as client:
        r = client.post(f"{BASE_URL}/tasks", json=payload, headers=_headers())
        r.raise_for_status()
        return _parse_json_object(r)


def list_tasks(project_id: str | None = None) -> list[dict]:
    params = {}
    if project_id:
        params["project_id"] = project_id
    with httpx.Client() as client:
        r = client.get(f"{BASE_URL}/tasks", params=params, headers=_headers())
        r.raise_for_status()
        data = r.json()
        return data["results"] if isinstance(data, dict) else data


def update_task_priority(task_id: str, priority: int) -> dict:
    with httpx.Client() as client:
        r = client.post(
            f"{BASE_URL}/tasks/{task_id}",
            json={"priority": priority},
            headers=_headers(),
        )
        r.raise_for_status()
        return _parse_json_object(r)


def get_task(task_id: str) -> dict:
    with httpx.Client() as client:
        r = client.get(f"{BASE_URL}/tasks/{task_id}", headers=_headers())
        r.raise_for_status()
        return _parse_json_object(r)


def close_task(task_id: str) -> None:
    with httpx.Client() as client:
        r = client.post(f"{BASE_URL}/tasks/{task_id}/close", headers=_headers())
        r.raise_for_status()


def delete_task(task_id: str) -> None:
    with httpx.Client() as client:
        r = client.delete(f"{BASE_URL}/tasks/{task_id}", headers=_headers())
        r.raise_for_status()


def create_project(name: str) -> dict:
    with httpx.Client() as client:
        r = client.post(f"{BASE_URL}/projects", json={"name": name}, headers=_headers())
        r.raise_for_status()
        return _parse_json_object(r)


def list_projects() -> list[dict]:
    with httpx.Client() as client:
        r = client.get(f"{BASE_URL}/projects", headers=_headers())
        r.raise_for_status()
        data = r.json()
        return data["results"] if isinstance(data, dict) else data
