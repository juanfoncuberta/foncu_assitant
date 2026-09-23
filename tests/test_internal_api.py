from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import internal_api
from internal_api import app

client = TestClient(app)

_SAMPLE_ENTRIES = [
    {
        "id": 1,
        "task_content": "add login handler",
        "branch": "feat/login-handler-120000",
        "summary": "Added login handler to resolve auth gap.",
        "created_at": "2026-09-15T10:00:00",
    },
    {
        "id": 2,
        "task_content": "fix null check",
        "branch": "fix/null-check-130000",
        "summary": "Fixed null check that caused crash on empty input.",
        "created_at": "2026-09-14T09:00:00",
    },
]


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(internal_api, "_API_KEY", "test-api-key")


def _auth_headers(key: str = "test-api-key") -> dict:
    return {"X-API-Key": key}


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_dev_log_without_key_returns_401():
    with patch("internal_api.get_recent_dev_log_entries", return_value=[]):
        r = client.get("/dev-log")
    assert r.status_code == 401


def test_dev_log_with_wrong_key_returns_401():
    with patch("internal_api.get_recent_dev_log_entries", return_value=[]):
        r = client.get("/dev-log", headers=_auth_headers("wrong-key"))
    assert r.status_code == 401


def test_dev_log_with_no_configured_key_returns_503(monkeypatch):
    monkeypatch.setattr(internal_api, "_API_KEY", None)
    with patch("internal_api.get_recent_dev_log_entries", return_value=[]):
        r = client.get("/dev-log", headers=_auth_headers())
    assert r.status_code == 503


def test_dev_log_with_correct_key_returns_200():
    with patch("internal_api.get_recent_dev_log_entries", return_value=[]):
        r = client.get("/dev-log", headers=_auth_headers())
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /dev-log behavior (with a valid key)
# ---------------------------------------------------------------------------


def test_dev_log_default_since_days():
    with patch("internal_api.get_recent_dev_log_entries", return_value=_SAMPLE_ENTRIES) as mock:
        client.get("/dev-log", headers=_auth_headers())
    mock.assert_called_once_with(since_days=7)


def test_dev_log_custom_since_days():
    with patch("internal_api.get_recent_dev_log_entries", return_value=_SAMPLE_ENTRIES) as mock:
        client.get("/dev-log?since_days=30", headers=_auth_headers())
    mock.assert_called_once_with(since_days=30)


def test_dev_log_returns_entries_as_json():
    with patch("internal_api.get_recent_dev_log_entries", return_value=_SAMPLE_ENTRIES):
        r = client.get("/dev-log", headers=_auth_headers())
    data = r.json()
    assert len(data) == 2
    assert data[0]["task_content"] == "add login handler"
    assert data[1]["branch"] == "fix/null-check-130000"


def test_dev_log_empty_result():
    with patch("internal_api.get_recent_dev_log_entries", return_value=[]):
        r = client.get("/dev-log", headers=_auth_headers())
    assert r.json() == []


def test_dev_log_all_fields_present():
    with patch("internal_api.get_recent_dev_log_entries", return_value=_SAMPLE_ENTRIES):
        r = client.get("/dev-log", headers=_auth_headers())
    entry = r.json()[0]
    for key in ("id", "task_content", "branch", "summary", "created_at"):
        assert key in entry
