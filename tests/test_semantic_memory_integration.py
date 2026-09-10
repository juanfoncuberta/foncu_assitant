"""
Integration tests for semantic_memory.py — run with the REAL SentenceTransformer
model and sqlite_vec extension (no mocks).

These tests are slow because they load the full all-MiniLM-L6-v2 model on first
run (~90 MB from HuggingFace cache).  Exclude them from fast test runs with:

    pytest -m "not integration"

Run only integration tests with:

    pytest -m integration

Requirements: the project venv must be active (numpy, sqlite-vec, and
sentence-transformers must be installed).

Implementation note
-------------------
test_semantic_memory.py injects MagicMock stubs into sys.modules during pytest
collection so the unit tests can run without heavy dependencies.  This test
module imports semantic_memory *inside* each test function (not at module
level) and clears those stubs first, so the module is reloaded with the real
packages every time.
"""

import sys

import pytest


@pytest.mark.integration
def test_similar_sentences_returned_dissimilar_excluded(tmp_path, monkeypatch):
    """
    End-to-end check: store two semantically similar sentences and one clearly
    unrelated one, then verify search_similar returns the similar pair and
    excludes the unrelated sentence — using the real model and the real
    vec_distance_cosine SQL function.
    """
    # Clear any MagicMock stubs injected by unit test collection, then import
    # semantic_memory fresh so it uses the real sentence-transformers + sqlite_vec.
    for _dep in ("numpy", "sqlite_vec", "sentence_transformers", "semantic_memory"):
        sys.modules.pop(_dep, None)

    import semantic_memory as sm  # noqa: PLC0415 (intentional deferred import)

    # Redirect all DB writes to a temp file so the real assistant.db is untouched.
    monkeypatch.setattr(sm, "DB_PATH", str(tmp_path / "integration_test.db"))

    chat_id = 777_001  # arbitrary; isolated by chat_id within the temp DB

    # Measured distances from "tengo una reunión mañana" with all-MiniLM-L6-v2:
    #   "reunión de trabajo mañana"          → 0.255  (well inside threshold)
    #   "cita de equipo mañana"              → 0.379  (inside threshold)
    #   "temperatura máxima hoy en Madrid"   → 0.678  (outside threshold 0.62)
    sm.add_semantic_memory(chat_id, None, "user", "reunión de trabajo mañana")
    sm.add_semantic_memory(chat_id, None, "user", "cita de equipo mañana")
    sm.add_semantic_memory(chat_id, None, "user", "temperatura máxima hoy en Madrid")

    results = sm.search_similar(chat_id, "tengo una reunión mañana")
    found = {r["content"] for r in results}

    assert "reunión de trabajo mañana" in found, (
        "Expected 'reunión de trabajo mañana' to be semantically close to the query"
    )
    assert "cita de equipo mañana" in found, (
        "Expected 'cita de equipo mañana' to be semantically close to the query"
    )
    assert "temperatura máxima hoy en Madrid" not in found, (
        "Expected 'temperatura máxima hoy en Madrid' to be too distant from the query"
    )
