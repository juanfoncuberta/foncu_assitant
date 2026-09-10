import pytest

import conversation_memory as cm


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "DB_PATH", str(tmp_path / "test.db"))


def test_empty_history():
    assert cm.get_history(1, 1) == []


def test_add_and_get_history():
    cm.add_message(1, 1, "user", "hello")
    cm.add_message(1, 1, "assistant", "world")
    assert cm.get_history(1, 1) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]


def test_history_order_is_chronological():
    for i in range(3):
        cm.add_message(1, 1, "user", f"msg{i}")
    contents = [m["content"] for m in cm.get_history(1, 1)]
    assert contents == ["msg0", "msg1", "msg2"]


def test_history_respects_max_limit():
    total = cm.MAX_HISTORY + 5
    for i in range(total):
        cm.add_message(1, 1, "user", f"msg{i}")
    history = cm.get_history(1, 1)
    assert len(history) == cm.MAX_HISTORY
    assert history[0]["content"] == "msg5"
    assert history[-1]["content"] == f"msg{total - 1}"


def test_isolation_by_topic():
    cm.add_message(1, 1, "user", "topic 1")
    cm.add_message(1, 2, "user", "topic 2")
    assert cm.get_history(1, 1) == [{"role": "user", "content": "topic 1"}]
    assert cm.get_history(1, 2) == [{"role": "user", "content": "topic 2"}]


def test_isolation_by_chat():
    cm.add_message(1, 1, "user", "chat 1")
    cm.add_message(2, 1, "user", "chat 2")
    assert cm.get_history(1, 1) == [{"role": "user", "content": "chat 1"}]
    assert cm.get_history(2, 1) == [{"role": "user", "content": "chat 2"}]


def test_none_topic_is_isolated_from_topic_one():
    """topic_id=None must not share rows with topic_id=1 (the _NONE_SENTINEL=0 bug)."""
    cm.add_message(1, None, "user", "no topic")
    cm.add_message(1, 1, "user", "topic one")
    assert cm.get_history(1, None) == [{"role": "user", "content": "no topic"}]
    assert cm.get_history(1, 1) == [{"role": "user", "content": "topic one"}]


def test_reset_topic_clears_only_target():
    cm.add_message(1, 1, "user", "to delete")
    cm.add_message(1, 2, "user", "to keep")
    cm.reset_topic(1, 1)
    assert cm.get_history(1, 1) == []
    assert cm.get_history(1, 2) == [{"role": "user", "content": "to keep"}]


def test_reset_topic_clears_history_and_summary():
    cm.add_message(1, 1, "user", "msg")
    cm._set_summary(1, 1, "some summary")
    cm.reset_topic(1, 1)
    assert cm.get_history(1, 1) == []
    assert cm.get_summary(1, 1) is None


def test_reset_none_topic_leaves_other_topics():
    cm.add_message(1, None, "user", "no topic msg")
    cm.add_message(1, 1, "user", "topic 1 msg")
    cm.reset_topic(1, None)
    assert cm.get_history(1, None) == []
    assert cm.get_history(1, 1) == [{"role": "user", "content": "topic 1 msg"}]


def test_summary_initially_none():
    assert cm.get_summary(1, 1) is None


def test_set_and_get_summary():
    cm._set_summary(1, 1, "first summary")
    assert cm.get_summary(1, 1) == "first summary"


def test_summary_upsert():
    cm._set_summary(1, 1, "old")
    cm._set_summary(1, 1, "new")
    assert cm.get_summary(1, 1) == "new"


def test_summary_isolated_by_topic():
    cm._set_summary(1, 1, "summary topic 1")
    cm._set_summary(1, 2, "summary topic 2")
    assert cm.get_summary(1, 1) == "summary topic 1"
    assert cm.get_summary(1, 2) == "summary topic 2"


def test_different_chats_none_topic_are_isolated():
    """The real bug: two distinct chat_ids both with topic_id=None must never share data.
    Both store topic_id as _NONE_SENTINEL=0, so isolation depends on chat_id alone."""
    cm.add_message(111, None, "user", "mensaje del chat 111")
    cm.add_message(222, None, "user", "mensaje del chat 222")

    assert cm.get_history(111, None) == [{"role": "user", "content": "mensaje del chat 111"}]
    assert cm.get_history(222, None) == [{"role": "user", "content": "mensaje del chat 222"}]

    cm._set_summary(111, None, "resumen 111")
    cm._set_summary(222, None, "resumen 222")

    assert cm.get_summary(111, None) == "resumen 111"
    assert cm.get_summary(222, None) == "resumen 222"


# ---------------------------------------------------------------------------
# trim_and_summarize
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock


def _make_mock_client(summary_text: str = "resumen generado") -> MagicMock:
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=summary_text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


def test_trim_and_summarize_noop_when_at_limit():
    for i in range(cm.MAX_HISTORY):
        cm.add_message(1, 1, "user", f"msg{i}")
    mock_client = _make_mock_client()
    cm.trim_and_summarize(1, 1, mock_client)
    mock_client.messages.create.assert_not_called()
    assert len(cm.get_history(1, 1)) == cm.MAX_HISTORY


def test_trim_and_summarize_triggers_above_limit():
    for i in range(cm.MAX_HISTORY + 3):
        cm.add_message(1, 1, "user", f"msg{i}")
    mock_client = _make_mock_client("nuevo resumen")
    cm.trim_and_summarize(1, 1, mock_client)
    mock_client.messages.create.assert_called_once()
    assert cm.get_summary(1, 1) == "nuevo resumen"


def test_trim_and_summarize_deletes_compressed_messages():
    total = cm.MAX_HISTORY + 5
    for i in range(total):
        cm.add_message(1, 1, "user", f"msg{i}")
    cm.trim_and_summarize(1, 1, _make_mock_client())
    history = cm.get_history(1, 1)
    assert len(history) == cm.MAX_HISTORY
    assert history[0]["content"] == "msg5"
    assert history[-1]["content"] == f"msg{total - 1}"


def test_trim_and_summarize_combines_existing_summary():
    cm._set_summary(1, 1, "resumen previo")
    for i in range(cm.MAX_HISTORY + 2):
        cm.add_message(1, 1, "user", f"msg{i}")
    mock_client = _make_mock_client("resumen combinado")
    cm.trim_and_summarize(1, 1, mock_client)
    prompt_text = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "resumen previo" in prompt_text
    assert cm.get_summary(1, 1) == "resumen combinado"


def test_trim_and_summarize_no_existing_summary_prompt():
    for i in range(cm.MAX_HISTORY + 1):
        cm.add_message(1, 1, "user", f"msg{i}")
    mock_client = _make_mock_client("primer resumen")
    cm.trim_and_summarize(1, 1, mock_client)
    prompt_text = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Resumen previo" not in prompt_text
    assert cm.get_summary(1, 1) == "primer resumen"
