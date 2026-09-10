import pytest

import semantic_memory as sm


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "DB_PATH", str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def mock_model(mocker):
    """Patches _get_model() so SentenceTransformer is never loaded in tests."""
    model = mocker.MagicMock()
    mocker.patch("semantic_memory._get_model", return_value=model)
    return model


# ---------------------------------------------------------------------------
# add_semantic_memory
# ---------------------------------------------------------------------------


def test_add_stores_entry(mock_model):
    mock_model.encode.return_value = [1.0, 0.0]
    sm.add_semantic_memory(1, "hello world")
    # Verify it can be retrieved via search
    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(1, "query", threshold=0.5)
    assert len(results) == 1
    assert results[0]["text"] == "hello world"


def test_add_captures_exception_silently(mock_model):
    mock_model.encode.side_effect = RuntimeError("model crashed")
    sm.add_semantic_memory(1, "text that fails")  # must not raise

    # Nothing was stored
    mock_model.encode.side_effect = None
    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(1, "query", threshold=0.5)
    assert results == []


# ---------------------------------------------------------------------------
# search_similar — threshold boundary
# ---------------------------------------------------------------------------


def test_search_includes_result_below_threshold(mock_model):
    """distance < threshold → entry is returned (the case that worried us)."""
    # Store a vector identical to the query → distance = 0.0
    mock_model.encode.return_value = [1.0, 0.0]
    sm.add_semantic_memory(1, "close text")

    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(1, "query", threshold=0.5)

    assert len(results) == 1
    assert results[0]["text"] == "close text"
    assert results[0]["distance"] < 0.5


def test_search_excludes_result_above_threshold(mock_model):
    """distance > threshold → entry is NOT returned (the case that worried us)."""
    # Store an orthogonal vector → cosine distance = 1.0
    mock_model.encode.return_value = [0.0, 1.0]
    sm.add_semantic_memory(1, "distant text")

    # Query with perpendicular vector
    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(1, "query", threshold=0.5)

    assert results == []


def test_search_mixed_results_respects_threshold(mock_model):
    """Only entries below the threshold are returned when both are present."""
    # Store close entry (distance ~ 0)
    mock_model.encode.return_value = [1.0, 0.0]
    sm.add_semantic_memory(1, "close")

    # Store distant entry (distance = 1.0)
    mock_model.encode.return_value = [0.0, 1.0]
    sm.add_semantic_memory(1, "distant")

    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(1, "query", threshold=0.5)

    texts = [r["text"] for r in results]
    assert "close" in texts
    assert "distant" not in texts


def test_search_results_ordered_by_distance(mock_model):
    """Results are sorted ascending by distance."""
    mock_model.encode.return_value = [1.0, 0.0]
    sm.add_semantic_memory(1, "identical")

    mock_model.encode.return_value = [0.8, 0.6]  # closer than [0.6, 0.8]
    sm.add_semantic_memory(1, "near")

    mock_model.encode.return_value = [0.6, 0.8]
    sm.add_semantic_memory(1, "further")

    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(1, "query", threshold=1.0)

    distances = [r["distance"] for r in results]
    assert distances == sorted(distances)


# ---------------------------------------------------------------------------
# search_similar — isolation by chat_id
# ---------------------------------------------------------------------------


def test_search_isolated_by_chat_id(mock_model):
    """Two different chat_ids must not share results."""
    mock_model.encode.return_value = [1.0, 0.0]
    sm.add_semantic_memory(111, "chat 111 entry")
    sm.add_semantic_memory(222, "chat 222 entry")

    mock_model.encode.return_value = [1.0, 0.0]
    results_111 = sm.search_similar(111, "query", threshold=0.5)
    results_222 = sm.search_similar(222, "query", threshold=0.5)

    assert len(results_111) == 1
    assert results_111[0]["text"] == "chat 111 entry"
    assert len(results_222) == 1
    assert results_222[0]["text"] == "chat 222 entry"


def test_search_empty_for_unknown_chat(mock_model):
    mock_model.encode.return_value = [1.0, 0.0]
    sm.add_semantic_memory(1, "some text")

    mock_model.encode.return_value = [1.0, 0.0]
    results = sm.search_similar(99, "query", threshold=0.5)
    assert results == []


# ---------------------------------------------------------------------------
# search_similar — exception handling
# ---------------------------------------------------------------------------


def test_search_returns_empty_on_exception(mock_model):
    mock_model.encode.side_effect = RuntimeError("encode failed")
    results = sm.search_similar(1, "query")
    assert results == []
