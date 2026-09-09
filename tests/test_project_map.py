import pytest

import project_map as pm


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "DB_PATH", str(tmp_path / "test.db"))


def test_get_returns_none_initially():
    assert pm.get_project_id(1, 1) is None


def test_set_and_get():
    pm.set_project_id(1, 1, "proj-123", "My Project")
    assert pm.get_project_id(1, 1) == "proj-123"


def test_upsert_updates_existing():
    pm.set_project_id(1, 1, "proj-123", "Old Name")
    pm.set_project_id(1, 1, "proj-456", "New Name")
    assert pm.get_project_id(1, 1) == "proj-456"


def test_none_thread_id_isolated_from_thread_one():
    """thread_id=None (sentinel=0) must not collide with thread_id=1."""
    pm.set_project_id(1, None, "proj-none", "No Thread")
    pm.set_project_id(1, 1, "proj-one", "Thread One")
    assert pm.get_project_id(1, None) == "proj-none"
    assert pm.get_project_id(1, 1) == "proj-one"


def test_isolation_by_chat():
    pm.set_project_id(1, 1, "proj-chat1", "Chat 1")
    pm.set_project_id(2, 1, "proj-chat2", "Chat 2")
    assert pm.get_project_id(1, 1) == "proj-chat1"
    assert pm.get_project_id(2, 1) == "proj-chat2"


def test_unknown_thread_returns_none():
    pm.set_project_id(1, 1, "proj-123", "Something")
    assert pm.get_project_id(1, 99) is None
    assert pm.get_project_id(99, 1) is None
