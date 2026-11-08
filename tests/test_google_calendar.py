import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import google_calendar as gc


@pytest.fixture
def token_response(monkeypatch):
    monkeypatch.setattr(gc, "client_id", lambda: "client")
    monkeypatch.setattr(gc, "client_secret", lambda: "secret")

    def set_response(status, payload):
        monkeypatch.setattr(gc.httpx, "post", lambda *args, **kwargs: SimpleNamespace(
            status_code=status, json=lambda: payload,
        ))

    return set_response


def test_revoked_refresh_token_requires_reconnect(token_response):
    token_response(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(gc.CalendarReconnectRequired, match="Reconnect Google Calendar"):
        gc.access_token("old-token")


def test_expired_authorization_code_does_not_misidentify_refresh_token(token_response):
    token_response(400, {"error": "invalid_grant", "error_description": "Code expired"})
    with pytest.raises(ValueError, match="Code expired") as error:
        gc.exchange_code("old-code")
    assert not isinstance(error.value, gc.CalendarReconnectRequired)


def test_server_error_does_not_require_reconnect(token_response):
    token_response(503, {"error": "temporarily_unavailable"})
    with pytest.raises(ValueError, match="temporarily_unavailable") as error:
        gc.access_token("valid-token")
    assert not isinstance(error.value, gc.CalendarReconnectRequired)


def test_valid_refresh_token_returns_access_token(token_response):
    token_response(200, {"access_token": "new-access-token"})
    assert gc.access_token("valid-token") == "new-access-token"
