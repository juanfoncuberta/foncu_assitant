from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
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


# ---------------------------------------------------------------------------
# HTTP error paths — raise_for_status must propagate, not fail silently
# ---------------------------------------------------------------------------


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        str(status_code),
        request=MagicMock(),
        response=MagicMock(status_code=status_code),
    )


def test_create_task_raises_on_429(http):
    http.post.return_value.raise_for_status.side_effect = _http_error(429)
    with pytest.raises(httpx.HTTPStatusError):
        todoist_client.create_task("Task")


def test_create_task_raises_on_401(http):
    http.post.return_value.raise_for_status.side_effect = _http_error(401)
    with pytest.raises(httpx.HTTPStatusError):
        todoist_client.create_task("Task")


def test_list_tasks_raises_on_429(http):
    http.get.return_value.raise_for_status.side_effect = _http_error(429)
    with pytest.raises(httpx.HTTPStatusError):
        todoist_client.list_tasks()


def test_get_task_raises_on_401(http):
    http.get.return_value.raise_for_status.side_effect = _http_error(401)
    with pytest.raises(httpx.HTTPStatusError):
        todoist_client.get_task("5")


# ---------------------------------------------------------------------------
# get_completed_tasks
# ---------------------------------------------------------------------------

COMPLETED_URL = f"{BASE_URL}/tasks/completed/by_completion_date"


def _completed_response(items: list[dict], next_cursor: str | None = None) -> MagicMock:
    mock = MagicMock()
    mock.json.return_value = {"items": items, "next_cursor": next_cursor}
    return mock


def test_get_completed_tasks_default_date_range(http, mocker):
    fixed_now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
    mocker.patch("todoist_client.datetime", wraps=datetime)
    mocker.patch.object(todoist_client, "datetime", wraps=datetime)

    http.get.return_value = _completed_response([{"id": "t1", "content": "Done task"}])

    result = todoist_client.get_completed_tasks()

    assert result == [{"id": "t1", "content": "Done task"}]
    call_params = http.get.call_args[1]["params"]
    assert "since" in call_params
    assert "until" in call_params


def test_get_completed_tasks_custom_dates(http):
    http.get.return_value = _completed_response([{"id": "t2"}])

    result = todoist_client.get_completed_tasks(since="2026-09-01T00:00:00Z", until="2026-09-08T00:00:00Z")

    call_params = http.get.call_args[1]["params"]
    assert call_params["since"] == "2026-09-01T00:00:00Z"
    assert call_params["until"] == "2026-09-08T00:00:00Z"
    assert result == [{"id": "t2"}]


def test_get_completed_tasks_with_project_id(http):
    http.get.return_value = _completed_response([{"id": "t3"}])

    todoist_client.get_completed_tasks(project_id="proj-42")

    call_params = http.get.call_args[1]["params"]
    assert call_params["project_id"] == "proj-42"


def test_get_completed_tasks_no_project_id_omits_param(http):
    http.get.return_value = _completed_response([])

    todoist_client.get_completed_tasks()

    call_params = http.get.call_args[1]["params"]
    assert "project_id" not in call_params


def test_get_completed_tasks_pagination(http):
    page1 = _completed_response([{"id": "t1"}, {"id": "t2"}], next_cursor="cursor-abc")
    page2 = _completed_response([{"id": "t3"}], next_cursor=None)
    http.get.side_effect = [page1, page2]

    result = todoist_client.get_completed_tasks()

    assert http.get.call_count == 2
    assert result == [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]
    # Second call must include the cursor
    second_params = http.get.call_args_list[1][1]["params"]
    assert second_params["cursor"] == "cursor-abc"


def test_get_completed_tasks_empty_result(http):
    http.get.return_value = _completed_response([])
    assert todoist_client.get_completed_tasks() == []


def test_get_completed_tasks_raises_on_http_error(http):
    http.get.return_value.raise_for_status.side_effect = _http_error(403)
    with pytest.raises(httpx.HTTPStatusError):
        todoist_client.get_completed_tasks()
