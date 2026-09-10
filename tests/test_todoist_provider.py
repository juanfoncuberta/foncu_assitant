import pytest

import todoist_client
from todoist_provider import TodoistProvider


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setenv("TODOIST_API_TOKEN", "test-token")


@pytest.fixture
def provider():
    return TodoistProvider()


# ---------------------------------------------------------------------------
# resolve_project
# ---------------------------------------------------------------------------


def test_resolve_project_returns_existing(provider, mocker):
    mocker.patch.object(
        provider,
        "list_projects",
        return_value=[{"id": "p1", "name": "Work"}, {"id": "p2", "name": "Personal"}],
    )
    create = mocker.patch.object(provider, "create_project")

    result = provider.resolve_project("Work")

    assert result == {"id": "p1", "name": "Work"}
    create.assert_not_called()


def test_resolve_project_creates_when_not_found(provider, mocker):
    mocker.patch.object(provider, "list_projects", return_value=[{"id": "p1", "name": "Work"}])
    mocker.patch.object(provider, "create_project", return_value={"id": "p99", "name": "New Project"})

    result = provider.resolve_project("New Project")

    provider.create_project.assert_called_once_with("New Project")
    assert result == {"id": "p99", "name": "New Project"}


def test_resolve_project_name_match_is_case_insensitive(provider, mocker):
    mocker.patch.object(
        provider,
        "list_projects",
        return_value=[{"id": "p1", "name": "Work"}],
    )
    create = mocker.patch.object(provider, "create_project")

    result = provider.resolve_project("work")

    assert result["id"] == "p1"
    create.assert_not_called()


def test_resolve_project_empty_list_creates(provider, mocker):
    mocker.patch.object(provider, "list_projects", return_value=[])
    mocker.patch.object(provider, "create_project", return_value={"id": "p1", "name": "Solo"})

    result = provider.resolve_project("Solo")

    assert result["id"] == "p1"
    provider.create_project.assert_called_once_with("Solo")


def test_resolve_project_skips_non_dict_entries(provider, mocker):
    """list_projects may return malformed entries; they must be ignored safely."""
    mocker.patch.object(
        provider,
        "list_projects",
        return_value=[None, "bad_entry", {"id": "p2", "name": "Real"}],
    )
    create = mocker.patch.object(provider, "create_project")

    result = provider.resolve_project("Real")

    assert result["id"] == "p2"
    create.assert_not_called()
