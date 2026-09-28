"""
Semantic memory layer — embeddings stored in assistant.db alongside the
chronological history, enabling similarity-based recall across conversations.

El modelo de SentenceTransformer se carga perezosamente (ver _get_model), y main()
lo precalienta en un hilo aparte para que el import del modulo siga siendo barato.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "assistant.db")
_NONE_SENTINEL = 0
# Cosine distance threshold: 0 = identical, 1 = orthogonal.
# Values below this are considered "relevant".
# Tuned empirically with all-MiniLM-L6-v2 on Spanish text: related phrases
# land at ~0.39–0.57, clearly unrelated at 0.67+.
SIMILARITY_THRESHOLD = 0.62

logger = logging.getLogger(__name__)

# El modelo y sus dependencias (torch, numpy, sqlite_vec) se cargan perezosamente.
#
# Antes se instanciaban al importar el modulo, lo que hacia que importar main.py
# arrastrase ~20s y varios cientos de MB. Ese es el motivo real de que main.py nunca
# haya tenido tests: cualquier test que lo importara pagaba esa factura.
#
# Para no trasladarle la espera al primer mensaje, main() llama a warmup() en un
# hilo aparte al arrancar. Asi el import es barato y el modelo sigue listo a tiempo.
_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # otro hilo pudo cargarlo mientras esperabamos
                from sentence_transformers import SentenceTransformer

                logger.info("Loading sentence-transformers model (all-MiniLM-L6-v2)…")
                _model = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("Model loaded.")
    return _model


def warmup() -> None:
    """Precarga el modelo. Pensado para llamarse en un hilo daemon al arrancar."""
    try:
        _get_model()
    except Exception:
        logger.exception("Fallo al precargar el modelo de embeddings")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # WAL: sin esto un escritor bloquea la base entera. Contra este fichero escriben
    # el hilo del bot, el de uvicorn (internal_api) y los workers de asyncio.to_thread.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    import sqlite_vec

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
    import numpy as np

    return _get_model().encode(text, normalize_embeddings=True).astype(np.float32).tobytes()


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
