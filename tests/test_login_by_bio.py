"""Tests for login_by_bio.

Context: this endpoint would remove the manual daily token paste entirely, and
the mechanism is real (the SPA uses it to restore sessions, with a 365-day
credential). It is unusable on the account this was built against - the
browser reported bioLogin/deviceId/SavedUser all absent and an empty
IndexedDB - so it is covered by offline tests only, and the monitor never
calls it.
"""

import json

import pytest

from smartschool.client import WebtopClient
from smartschool.exceptions import ApiError, TokenExpired


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client(monkeypatch, response, capture=None):
    client = WebtopClient("old-token")

    def fake_post(url, json=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "json": json})
        return response

    monkeypatch.setattr(client._session, "post", fake_post)
    return client


def test_sends_the_payload_shape_from_the_bundle(monkeypatch):
    capture = []
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": {"token": "new"}}), capture
    )
    client.login_by_bio(
        bio_login="BIO",
        device_id="DEV",
        selected_user="SEL",
        unique_id="UID",
        is_mobile=False,
        mode="2",
    )
    assert capture[0]["url"].endswith("/server/api/user/loginByBio")
    assert capture[0]["json"] == {
        "id": "BIO",
        "param1": "2",
        "param2": "DEV",
        "param3": "SEL",
        "param4": "UID",
        "param5": "false",
    }


def test_mode_selects_remember_me_vs_biometric(monkeypatch):
    capture = []
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": {"token": "t"}}), capture
    )
    client.login_by_bio(bio_login="B", device_id="D", mode="1")
    assert capture[0]["json"]["param1"] == "1"


def test_is_mobile_is_sent_as_a_lowercase_string(monkeypatch):
    capture = []
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": {"token": "t"}}), capture
    )
    client.login_by_bio(bio_login="B", device_id="D", is_mobile=True)
    assert capture[0]["json"]["param5"] == "true", "the bundle sends isMobile.toString()"


def test_returns_and_installs_the_new_token(monkeypatch):
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": {"token": "fresh-token"}})
    )
    token = client.login_by_bio(bio_login="B", device_id="D")
    assert token == "fresh-token"
    assert client.token == "fresh-token", "a successful call must install the token"


def test_returns_none_when_no_token_in_response(monkeypatch):
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": {"userId": 1}})
    )
    assert client.login_by_bio(bio_login="B", device_id="D") is None
    assert client.token == "old-token", "must not disturb the existing token"


def test_returns_none_when_data_is_not_an_object(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, {"status": True, "data": []}))
    assert client.login_by_bio(bio_login="B", device_id="D") is None


def test_status_false_raises(monkeypatch):
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": False, "errorDescription": None})
    )
    with pytest.raises(ApiError):
        client.login_by_bio(bio_login="B", device_id="D")


def test_401_raises_token_expired(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(401, None, text=""))
    with pytest.raises(TokenExpired):
        client.login_by_bio(bio_login="B", device_id="D")


def test_missing_optional_params_default_to_empty(monkeypatch):
    capture = []
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": {"token": "t"}}), capture
    )
    client.login_by_bio(bio_login="B", device_id="D")
    assert capture[0]["json"]["param3"] == ""
    assert capture[0]["json"]["param4"] == ""
