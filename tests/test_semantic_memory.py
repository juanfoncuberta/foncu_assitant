"""
Tests for semantic_memory.py (production version: sqlite_vec + SentenceTransformer).

numpy, sqlite_vec, and sentence_transformers are NOT installed in the test
environment — they live in the Docker image at runtime.  We inject MagicMock
stubs into sys.modules before importing the module so the top-level imports
and the module-level `_model = SentenceTransformer(...)` succeed without
loading 857 MB of model weights.

After import, every test patches two functions:
  _conn  — replaced by a real sqlite3 connection that registers a pure-Python
            implementation of vec_distance_cosine, so the SQL queries run
            without the C extension.
  _embed — replaced by a queue-based stub that returns controlled float32
            blobs, so tests can set exact distances without a real model.

What IS verified:
  - The calibrated SIMILARITY_THRESHOLD (0.62) is preserved.
  - add_semantic_memory stores role, content, and embedding correctly.
  - search_similar filters by chat_id and applies the distance threshold.
  - distance < threshold → result is included.
  - distance > threshold → result is excluded.
  - Results are ordered ascending by distance.
  - top_k caps the result count.
  - Exceptions in _embed are swallowed by both add and search.

What is NOT verified (because _conn and _embed are replaced):
  - That SentenceTransformer produces semantically meaningful embeddings.
  - That sqlite_vec.load correctly registers vec_distance_cosine in SQLite.
  - That _embed's float32 encoding matches what vec_distance_cosine expects.
"""

import math
import sqlite3
import struct
import sys
from unittest.mock import MagicMock

import pytest

for _dep in ("numpy", "sqlite_vec", "sentence_transformers"):
    sys.modules.setdefault(_dep, MagicMock())

import semantic_memory as sm

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_TABLE_DDL = """
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


def _f32(*values: float) -> bytes:
    """Pack floats into a float32 byte blob — the format _embed produces."""
    return struct.pack(f"{len(values)}f", *values)


def _cosine_dist(blob_a: bytes, blob_b: bytes) -> float:
    """Pure-Python cosine distance; registered as vec_distance_cosine in SQLite."""
    blob_a, blob_b = bytes(blob_a), bytes(blob_b)
    n = len(blob_a) // 4
    a = struct.unpack(f"{n}f", blob_a)
    b = struct.unpack(f"{n}f", blob_b)
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """
    Replaces _conn with a real sqlite3 connection that:
      - lives in a temp directory (test isolation)
      - has vec_distance_cosine registered as a Python function
      - has the production table schema
    """
    db_path = str(tmp_path / "sem.db")
    monkeypatch.setattr(sm, "DB_PATH", db_path)

    def fake_conn():
        conn = sqlite3.connect(db_path)
        conn.create_function("vec_distance_cosine", 2, _cosine_dist)
        conn.execute(_TABLE_DDL)
        conn.commit()
        return conn

    monkeypatch.setattr(sm, "_conn", fake_conn)


@pytest.fixture(autouse=True)
def embed_queue(monkeypatch):
    """
    Replaces _embed with a queue-based stub.  Tests that need controlled
    embeddings append bytes to the returned list; when the queue is empty
    _embed returns _f32(1.0, 0.0) as default (identical to the default query
    vector used in most tests, giving cosine distance = 0.0).
    """
    queue: list[bytes] = []
    monkeypatch.setattr(sm, "_embed", lambda _text: queue.pop(0) if queue else _f32(1.0, 0.0))
    return queue


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------


def test_calibrated_threshold_preserved():
    """SIMILARITY_THRESHOLD must not be changed without empirical data."""
    assert sm.SIMILARITY_THRESHOLD == 0.62


# ---------------------------------------------------------------------------
# add_semantic_memory
# ---------------------------------------------------------------------------


def test_add_stores_entry():
    sm.add_semantic_memory(1, None, "user", "hello world")
    results = sm.search_similar(1, "query")
    assert len(results) == 1
    assert results[0]["content"] == "hello world"
    assert results[0]["role"] == "user"


def test_add_preserves_role():
    sm.add_semantic_memory(1, None, "assistant", "my reply")
    assert sm.search_similar(1, "query")[0]["role"] == "assistant"


def test_add_with_topic_id_stores_and_is_searchable():
    """topic_id is stored but search_similar does not filter by it."""
    sm.add_semantic_memory(1, 42, "user", "thread 42 message")
    contents = [r["content"] for r in sm.search_similar(1, "query")]
    assert "thread 42 message" in contents


def test_add_captures_exception_silently(monkeypatch):
    def explode(_text):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(sm, "_embed", explode)
    sm.add_semantic_memory(1, None, "user", "text")  # must not raise


# ---------------------------------------------------------------------------
# search_similar — threshold boundary (the cases that matter most)
# ---------------------------------------------------------------------------


def test_search_includes_result_below_threshold(embed_queue):
    """distance < threshold → entry is returned."""
    embed_queue.append(_f32(1.0, 0.0))  # stored embedding for "close text"
    sm.add_semantic_memory(1, None, "user", "close text")

    embed_queue.append(_f32(1.0, 0.0))  # query embedding; identical → distance = 0.0
    results = sm.search_similar(1, "query", threshold=0.5)

    assert len(results) == 1
    assert results[0]["content"] == "close text"
    assert results[0]["distance"] < 0.5


def test_search_excludes_result_above_threshold(embed_queue):
    """distance > threshold → entry is NOT returned."""
    embed_queue.append(_f32(0.0, 1.0))  # orthogonal to query → distance = 1.0
    sm.add_semantic_memory(1, None, "user", "distant text")

    embed_queue.append(_f32(1.0, 0.0))  # query
    results = sm.search_similar(1, "query", threshold=0.5)

    assert results == []


def test_search_mixed_results_respects_threshold(embed_queue):
    """Only the entry below the threshold is returned when both are present."""
    embed_queue.append(_f32(1.0, 0.0))  # close → distance 0.0
    sm.add_semantic_memory(1, None, "user", "close")

    embed_queue.append(_f32(0.0, 1.0))  # distant → distance 1.0
    sm.add_semantic_memory(1, None, "user", "distant")

    embed_queue.append(_f32(1.0, 0.0))  # query
    results = sm.search_similar(1, "query", threshold=0.5)

    contents = [r["content"] for r in results]
    assert "close" in contents
    assert "distant" not in contents


def test_search_results_ordered_by_distance(embed_queue):
    """Results are sorted ascending by distance."""
    # All three are unit vectors; distances from [1,0]: 0.0, 0.2, 0.4
    embed_queue.append(_f32(1.0, 0.0))   # dist 0.0 from query
    sm.add_semantic_memory(1, None, "user", "identical")

    embed_queue.append(_f32(0.8, 0.6))   # dist 0.2
    sm.add_semantic_memory(1, None, "user", "near")

    embed_queue.append(_f32(0.6, 0.8))   # dist 0.4
    sm.add_semantic_memory(1, None, "user", "further")

    embed_queue.append(_f32(1.0, 0.0))   # query
    results = sm.search_similar(1, "query", threshold=1.0)

    distances = [r["distance"] for r in results]
    assert distances == sorted(distances)


# ---------------------------------------------------------------------------
# search_similar — isolation by chat_id
# ---------------------------------------------------------------------------


def test_search_isolated_by_chat_id():
    """Entries from different chat_ids must not bleed into each other."""
    sm.add_semantic_memory(111, None, "user", "chat 111 entry")
    sm.add_semantic_memory(222, None, "user", "chat 222 entry")

    results_111 = sm.search_similar(111, "query")
    results_222 = sm.search_similar(222, "query")

    assert len(results_111) == 1
    assert results_111[0]["content"] == "chat 111 entry"
    assert len(results_222) == 1
    assert results_222[0]["content"] == "chat 222 entry"


def test_search_empty_for_unknown_chat():
    sm.add_semantic_memory(1, None, "user", "some text")
    assert sm.search_similar(99, "query") == []


# ---------------------------------------------------------------------------
# search_similar — top_k and result shape
# ---------------------------------------------------------------------------


def test_search_top_k_limits_results():
    for i in range(5):
        sm.add_semantic_memory(1, None, "user", f"message {i}")
    results = sm.search_similar(1, "query", top_k=2)
    assert len(results) <= 2


def test_search_result_has_expected_keys():
    sm.add_semantic_memory(1, None, "user", "test message")
    results = sm.search_similar(1, "query")
    assert set(results[0].keys()) == {"role", "content", "distance"}


# ---------------------------------------------------------------------------
# search_similar — exception handling
# ---------------------------------------------------------------------------


def test_search_captures_exception(monkeypatch):
    def explode(_text):
        raise RuntimeError("encode failed")

    monkeypatch.setattr(sm, "_embed", explode)
    assert sm.search_similar(1, "query") == []
