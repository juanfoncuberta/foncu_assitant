"""
Conversation memory per (chat_id, topic_id) pair.
Tables in assistant.db:
  - conversation_history: raw messages (user/assistant), up to MAX_HISTORY per (chat, topic)
  - conversation_summary: accumulated summary per (chat, topic) when history grows

Key: (chat_id, topic_id). thread_id=None is stored as _NONE_SENTINEL to avoid NULL != NULL
in SQLite comparisons.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "assistant.db")
_NONE_SENTINEL = 0
MAX_HISTORY = 15

logger = logging.getLogger(__name__)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER NOT NULL,
            topic_id  INTEGER NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_summary (
            chat_id      INTEGER NOT NULL,
            topic_id     INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (chat_id, topic_id)
        )
        """
    )
    conn.commit()
    return conn


def _key(topic_id: int | None) -> int:
    return _NONE_SENTINEL if topic_id is None else topic_id


def add_message(chat_id: int, topic_id: int | None, role: str, content: str) -> None:
    key = _key(topic_id)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversation_history (chat_id, topic_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (chat_id, key, role, content, now),
        )


def get_history(chat_id: int, topic_id: int | None) -> list[dict]:
    """Returns the last MAX_HISTORY messages for (chat_id, topic_id) as a list of role/content dicts."""
    key = _key(topic_id)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM conversation_history
            WHERE chat_id = ? AND topic_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, key, MAX_HISTORY),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def get_summary(chat_id: int, topic_id: int | None) -> str | None:
    key = _key(topic_id)
    with _conn() as conn:
        row = conn.execute(
            "SELECT summary_text FROM conversation_summary WHERE chat_id = ? AND topic_id = ?",
            (chat_id, key),
        ).fetchone()
    return row[0] if row else None


def _set_summary(chat_id: int, topic_id: int | None, summary: str) -> None:
    key = _key(topic_id)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO conversation_summary (chat_id, topic_id, summary_text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, topic_id) DO UPDATE SET
                summary_text = excluded.summary_text,
                updated_at   = excluded.updated_at
            """,
            (chat_id, key, summary, now),
        )


def reset_topic(chat_id: int, topic_id: int | None) -> None:
    key = _key(topic_id)
    with _conn() as conn:
        conn.execute("DELETE FROM conversation_history WHERE chat_id = ? AND topic_id = ?", (chat_id, key))
        conn.execute("DELETE FROM conversation_summary WHERE chat_id = ? AND topic_id = ?", (chat_id, key))


def trim_and_summarize(chat_id: int, topic_id: int | None, claude_client) -> None:
    """If history exceeds MAX_HISTORY, summarize the oldest messages and delete them."""
    key = _key(topic_id)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content FROM conversation_history WHERE chat_id = ? AND topic_id = ? ORDER BY id",
            (chat_id, key),
        ).fetchall()

    if len(rows) <= MAX_HISTORY:
        return

    overflow_count = len(rows) - MAX_HISTORY
    to_summarize = rows[:overflow_count]
    existing_summary = get_summary(chat_id, topic_id)

    messages_text = "\n".join(f"{r[1]}: {r[2]}" for r in to_summarize)

    if existing_summary:
        prompt = (
            f"Resumen previo de la conversación:\n{existing_summary}\n\n"
            f"Mensajes adicionales a incorporar:\n{messages_text}\n\n"
            "Genera un resumen conciso (3-5 frases) que combine el resumen previo con los mensajes adicionales, "
            "conservando los puntos importantes de ambos."
        )
    else:
        prompt = (
            f"Mensajes de conversación:\n{messages_text}\n\n"
            "Genera un resumen conciso (3-5 frases) de estos mensajes."
        )

    try:
        response = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        new_summary = response.content[0].text
        _set_summary(chat_id, topic_id, new_summary)
        overflow_ids = [r[0] for r in to_summarize]
        with _conn() as conn:
            conn.execute(
                f"DELETE FROM conversation_history WHERE id IN ({','.join('?' * len(overflow_ids))})",
                overflow_ids,
            )
        logger.info("History compressed: %d messages summarized for chat=%s topic=%s", overflow_count, chat_id, topic_id)
    except Exception:
        logger.exception("Error summarizing history for chat=%s topic=%s", chat_id, topic_id)
