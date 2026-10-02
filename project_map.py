"""
Persistent mapping from (chat_id, thread_id) to Todoist project_id.
Stored in assistant.db (created automatically).

Chats without Topics arrive with thread_id=None; stored internally as
_NONE_SENTINEL=0 to avoid NULL != NULL semantics in SQLite.
The composite key (chat_id, thread_id) ensures topics from different
Telegram chats never share a project mapping.
"""

import os
import sqlite3

import project_directory_map

DB_PATH = os.environ.get("DB_PATH", "assistant.db")

_NONE_SENTINEL = 0  # represents thread_id=None in the table


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # WAL: sin esto un escritor bloquea la base entera. Contra este fichero escriben
    # el hilo del bot, el de uvicorn (internal_api) y los workers de asyncio.to_thread.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topic_project (
            chat_id      INTEGER NOT NULL,
            thread_id    INTEGER NOT NULL,
            project_id   TEXT NOT NULL,
            project_name TEXT NOT NULL,
            PRIMARY KEY (chat_id, thread_id)
        )
        """
    )
    conn.commit()
    return conn


def get_project_id(chat_id: int, thread_id: int | None) -> str | None:
    key = _NONE_SENTINEL if thread_id is None else thread_id
    with _conn() as conn:
        row = conn.execute(
            "SELECT project_id FROM topic_project WHERE chat_id = ? AND thread_id = ?",
            (chat_id, key),
        ).fetchone()
        return row[0] if row else None


def set_project_id(chat_id: int, thread_id: int | None, project_id: str, project_name: str) -> None:
    key = _NONE_SENTINEL if thread_id is None else thread_id
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO topic_project (chat_id, thread_id, project_id, project_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                project_id   = excluded.project_id,
                project_name = excluded.project_name
            """,
            (chat_id, key, project_id, project_name),
        )



def get_project_label(chat_id: int, thread_id: int | None) -> str | None:
    """
    Nombre con el que se imputa el gasto de un topic.

    Si su proyecto tiene carpeta vinculada (vincular_carpeta_proyecto), el nombre de
    esa carpeta: es el mismo que usan las tareas de dev y casi nunca cambia. Si no,
    el nombre del proyecto en el gestor de tareas. None si el topic no tiene proyecto.
    """
    key = _NONE_SENTINEL if thread_id is None else thread_id
    with _conn() as conn:
        row = conn.execute(
            "SELECT project_id, project_name FROM topic_project WHERE chat_id = ? AND thread_id = ?",
            (chat_id, key),
        ).fetchone()
    if not row:
        return None
    project_id, project_name = row
    directorio = project_directory_map.get_directory(project_id)
    if directorio:
        return project_directory_map.label_for_directory(directorio)
    return project_name
