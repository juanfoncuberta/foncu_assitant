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

DB_PATH = os.environ.get("DB_PATH", "assistant.db")

_NONE_SENTINEL = 0  # represents thread_id=None in the table


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
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
