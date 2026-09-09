import pytest

import todoist_client

BASE_URL = "https://api.todoist.com/api/v1"


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setenv("TODOIST_API_TOKEN", "test-token")


@pytest.fixture
def http(mocker):
    """Yields a mock httpx.Client instance already wired as context manager."""
    mock_instance = mocker.MagicMock()
    mock_instance.__enter__.return_value = mock_instance
    mock_instance.__exit__.return_value = False
    mocker.patch("httpx.Client", return_value=mock_instance)
    return mock_instance


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def test_create_task_minimal(http):
    http.post.return_value.json.return_value = {"id": "1", "content": "Buy milk"}
    result = todoist_client.create_task("Buy milk")
    http.post.assert_called_once_with(
        f"{BASE_URL}/tasks",
        json={"content": "Buy milk", "priority": 1},
        headers={"Authorization": "Bearer test-token"},
    )
    assert result["id"] == "1"


def test_create_task_with_all_fields(http):
    http.post.return_value.json.return_value = {"id": "2", "content": "Call dentist"}
    todoist_client.create_task("Call dentist", due_string="tomorrow", priority=4, project_id="proj-123")
    http.post.assert_called_once_with(
        f"{BASE_URL}/tasks",
        json={
            "content": "Call dentist",
            "due_string": "tomorrow",
            "priority": 4,
            "project_id": "proj-123",
        },
        headers={"Authorization": "Bearer test-token"},
    )


def test_create_task_omits_none_optional_fields(http):
    http.post.return_value.json.return_value = {"id": "3", "content": "Task"}
    todoist_client.create_task("Task", due_string=None, project_id=None)
    _, kwargs = http.post.call_args
    assert "due_string" not in kwargs["json"]
    assert "project_id" not in kwargs["json"]


def test_create_task_raises_on_non_dict_response(http):
    http.post.return_value.json.return_value = ["unexpected", "list"]
    with pytest.raises(ValueError, match="tipo inesperado"):
        todoist_client.create_task("Task")


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


def test_list_tasks_no_filter_passes_empty_params(http):
    http.get.return_value.json.return_value = [{"id": "1"}, {"id": "2"}]
    result = todoist_client.list_tasks()
    http.get.assert_called_once_with(
        f"{BASE_URL}/tasks",
        params={},
        headers={"Authorization": "Bearer test-token"},
    )
    assert result == [{"id": "1"}, {"id": "2"}]


def test_list_tasks_with_project_id(http):
    http.get.return_value.json.return_value = {"results": [{"id": "1"}], "next_cursor": None}
    result = todoist_client.list_tasks(project_id="proj-123")
    http.get.assert_called_once_with(
        f"{BASE_URL}/tasks",
        params={"project_id": "proj-123"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert result == [{"id": "1"}]


def test_list_tasks_returns_list_directly_when_response_is_list(http):
    tasks = [{"id": "a"}, {"id": "b"}]
    http.get.return_value.json.return_value = tasks
    assert todoist_client.list_tasks() == tasks


def test_list_tasks_returns_results_key_when_response_is_dict(http):
    http.get.return_value.json.return_value = {"results": [{"id": "x"}], "next_cursor": None}
    assert todoist_client.list_tasks() == [{"id": "x"}]


# ---------------------------------------------------------------------------
# delete_task / close_task / get_task
# ---------------------------------------------------------------------------


def test_delete_task_calls_delete(http):
    todoist_client.delete_task("task-999")
    http.delete.assert_called_once_with(
        f"{BASE_URL}/tasks/task-999",
        headers={"Authorization": "Bearer test-token"},
    )


def test_close_task_calls_post(http):
    todoist_client.close_task("task-42")
    http.post.assert_called_once_with(
        f"{BASE_URL}/tasks/task-42/close",
        headers={"Authorization": "Bearer test-token"},
    )


def test_get_task_returns_dict(http):
    http.get.return_value.json.return_value = {"id": "5", "content": "Read book"}
    result = todoist_client.get_task("5")
    http.get.assert_called_once_with(
        f"{BASE_URL}/tasks/5",
        headers={"Authorization": "Bearer test-token"},
    )
    assert result["content"] == "Read book"


def test_get_task_raises_on_non_dict_response(http):
    http.get.return_value.json.return_value = ["not", "a", "dict"]
    with pytest.raises(ValueError, match="tipo inesperado"):
        todoist_client.get_task("5")


# ---------------------------------------------------------------------------
# update_task_priority
# ---------------------------------------------------------------------------


def test_update_task_priority(http):
    http.post.return_value.json.return_value = {"id": "7", "priority": 4}
    result = todoist_client.update_task_priority("7", 4)
    http.post.assert_called_once_with(
        f"{BASE_URL}/tasks/7",
        json={"priority": 4},
        headers={"Authorization": "Bearer test-token"},
    )
    assert result["priority"] == 4
