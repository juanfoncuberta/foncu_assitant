"""
Semantic memory layer — embeddings stored in assistant.db alongside the
chronological history, enabling similarity-based recall across conversations.

The SentenceTransformer model is loaded once at module import time so it is
ready before the first message arrives.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

import numpy as np
import sqlite_vec
from sentence_transformers import SentenceTransformer

DB_PATH = os.environ.get("DB_PATH", "assistant.db")
_NONE_SENTINEL = 0
# Cosine distance threshold: 0 = identical, 1 = orthogonal.
# Values below this are considered "relevant".
# Tuned empirically with all-MiniLM-L6-v2 on Spanish text: related phrases
# land at ~0.39–0.57, clearly unrelated at 0.67+.
SIMILARITY_THRESHOLD = 0.62

logger = logging.getLogger(__name__)

logger.info("Loading sentence-transformers model (all-MiniLM-L6-v2)…")
_model = SentenceTransformer("all-MiniLM-L6-v2")
logger.info("Model loaded.")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_memory (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER NOT NULL,
            topic_id  INTEGER NOT NULL,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            embedding BLOB NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _key(topic_id: int | None) -> int:
    return _NONE_SENTINEL if topic_id is None else topic_id


def _embed(text: str) -> bytes:
    return _model.encode(text, normalize_embeddings=True).astype(np.float32).tobytes()


def add_semantic_memory(chat_id: int, topic_id: int | None, role: str, content: str) -> None:
    key = _key(topic_id)
    now = datetime.now(timezone.utc).isoformat()
    try:
        embedding = _embed(content)
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO semantic_memory (chat_id, topic_id, role, content, embedding, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, key, role, content, embedding, now),
            )
    except Exception:
        logger.exception("Error storing semantic memory (chat=%s topic=%s)", chat_id, topic_id)


def search_similar(
    chat_id: int,
    query: str,
    top_k: int = 5,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Return up to top_k messages from chat_id whose cosine distance to query < threshold."""
    try:
        query_bytes = _embed(query)
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT role, content, distance FROM (
                    SELECT role, content,
                           vec_distance_cosine(embedding, ?) AS distance
                    FROM semantic_memory
                    WHERE chat_id = ?
                ) WHERE distance < ?
                ORDER BY distance ASC
                LIMIT ?
                """,
                (query_bytes, chat_id, threshold, top_k),
            ).fetchall()
        return [{"role": r[0], "content": r[1], "distance": r[2]} for r in rows]
    except Exception:
        logger.exception("Error searching semantic memory (chat=%s)", chat_id)
        return []
