"""
Semantic memory: stores text embeddings per chat_id and retrieves similar entries.

Uses SentenceTransformer (lazy-loaded) for encoding and cosine distance for similarity.
Threshold is a distance, not a score: entries with distance < threshold are included.
"""

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "assistant.db")
logger = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            text       TEXT NOT NULL,
            embedding  TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


def add_semantic_memory(chat_id: int, text: str) -> None:
    try:
        embedding = list(_get_model().encode(text))
        now = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            conn.execute(
                "INSERT INTO semantic_memory (chat_id, text, embedding, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, text, json.dumps(embedding), now),
            )
    except Exception:
        logger.exception("Error adding semantic memory for chat=%s", chat_id)


def search_similar(
    chat_id: int,
    query: str,
    threshold: float = 0.5,
    k: int = 5,
) -> list[dict]:
    """Returns entries for chat_id whose cosine distance to query is strictly below threshold."""
    try:
        query_emb = list(_get_model().encode(query))
        with _conn() as conn:
            rows = conn.execute(
                "SELECT text, embedding FROM semantic_memory WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
        results = []
        for text, emb_json in rows:
            emb = json.loads(emb_json)
            dist = _cosine_distance(query_emb, emb)
            if dist < threshold:
                results.append({"text": text, "distance": dist})
        results.sort(key=lambda x: x["distance"])
        return results[:k]
    except Exception:
        logger.exception("Error searching semantic memory for chat=%s", chat_id)
        return []
