import pytest
import httpx

import digitalocean_client as dc

BASE_URL = "https://api.digitalocean.com/v2"


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "do-test-token")


@pytest.fixture
def http(mocker):
    """Yields a mock httpx.Client instance already wired as context manager."""
    mock_instance = mocker.MagicMock()
    mock_instance.__enter__.return_value = mock_instance
    mock_instance.__exit__.return_value = False
    mocker.patch("httpx.Client", return_value=mock_instance)
    return mock_instance


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------


def test_get_balance_calls_correct_url_and_headers(http):
    http.get.return_value.json.return_value = {"month_to_date_balance": "12.34"}
    dc.get_balance()
    http.get.assert_called_once_with(
        f"{BASE_URL}/customers/my/balance",
        headers={"Authorization": "Bearer do-test-token"},
    )


def test_get_balance_returns_json_response(http):
    payload = {"month_to_date_balance": "12.34", "account_balance": "100.00"}
    http.get.return_value.json.return_value = payload
    result = dc.get_balance()
    assert result == payload


def test_get_balance_raises_on_http_error(http):
    http.get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503", request=mocker_request(), response=mocker_response(503)
    )
    with pytest.raises(httpx.HTTPStatusError):
        dc.get_balance()


def test_get_balance_raises_if_token_missing(monkeypatch):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    with pytest.raises(KeyError):
        dc.get_balance()


# ---------------------------------------------------------------------------
# helpers for HTTP error construction
# ---------------------------------------------------------------------------


def mocker_request():
    from unittest.mock import MagicMock
    return MagicMock()


def mocker_response(status_code: int):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.status_code = status_code
    return r
