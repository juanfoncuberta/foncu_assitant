import pytest

import project_directory_map as pdm


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(pdm, "DB_PATH", str(tmp_path / "test.db"))


def test_get_returns_none_initially():
    assert pdm.get_directory("proj-123") is None


def test_set_and_get():
    pdm.set_directory("proj-123", "/home/user/myproject")
    assert pdm.get_directory("proj-123") == "/home/user/myproject"


def test_upsert_updates_existing():
    pdm.set_directory("proj-123", "/old/path")
    pdm.set_directory("proj-123", "/new/path")
    assert pdm.get_directory("proj-123") == "/new/path"


def test_projects_are_isolated():
    pdm.set_directory("proj-a", "/path/a")
    pdm.set_directory("proj-b", "/path/b")
    assert pdm.get_directory("proj-a") == "/path/a"
    assert pdm.get_directory("proj-b") == "/path/b"


def test_unknown_project_returns_none():
    pdm.set_directory("proj-123", "/some/path")
    assert pdm.get_directory("proj-999") is None
