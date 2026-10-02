"""
Maps Todoist project_id to a local absolute directory path.
Stored in assistant.db (same database as other modules).
"""

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "assistant.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # WAL: sin esto un escritor bloquea la base entera. Contra este fichero escriben
    # el hilo del bot, el de uvicorn (internal_api) y los workers de asyncio.to_thread.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_directory_map (
            project_id     TEXT PRIMARY KEY,
            directory_path TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_directory(project_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT directory_path FROM project_directory_map WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return row[0] if row else None


def set_directory(project_id: str, directory_path: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO project_directory_map (project_id, directory_path)
            VALUES (?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                directory_path = excluded.directory_path
            """,
            (project_id, directory_path),
        )


def label_for_directory(directory_path: str) -> str:
    """
    Nombre con el que se imputa el gasto de un proyecto que tiene carpeta: el de la
    carpeta. Unico sitio con esta regla: lo usan las tareas de dev y el chat, y si
    cada uno la escribiera a su manera el mismo proyecto saldria con dos nombres.
    """
    return os.path.basename(os.path.normpath(directory_path))
