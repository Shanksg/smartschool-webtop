"""Tests for the integration's DataUpdateCoordinator.

Requires Home Assistant to be importable (pytest-homeassistant-custom-component
in CI). The coordinator's fetch/mint/retry/error-mapping is synchronous
(_fetch, run in the executor at runtime), so it is exercised directly with a
fake client — no running HA instance needed. Skipped cleanly if HA is absent.
"""

import os
import sys

import pytest

pytest.importorskip("homeassistant")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from homeassistant.exceptions import ConfigEntryAuthFailed  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.smartschool import coordinator as coord_mod  # noqa: E402
from custom_components.smartschool.api.bio import BioCredentials  # noqa: E402
from custom_components.smartschool.api.exceptions import (  # noqa: E402
    ApiError,
    RequestFailed,
    TokenExpired,
)
from custom_components.smartschool.api.models import Student  # noqa: E402

SmartSchoolCoordinator = coord_mod.SmartSchoolCoordinator

# a valid PupilCard body for one homework item
PUPILCARD_BODY = {
    "status": True,
    "data": [
        {
            "date": "2026-09-12T00:00:00",
            "hoursData": [{"scheduale": [
                {"subject_name": "מתמטיקה", "teacher": "T", "homeWork": "עמוד 45"}
            ]}],
        }
    ],
}
STUDENT = Student(student_id="stu-1", name="kid", class_code=4, class_number=1)


class FakeClient:
    """Stand-in for WebtopClient; each method can be told to raise."""

    def __init__(self, *, students=None, hw_body=None, messages=None, raise_on=None):
        self._students = students if students is not None else [STUDENT]
        self._hw = hw_body if hw_body is not None else PUPILCARD_BODY
        self._messages = messages if messages is not None else []
        self._raise_on = raise_on or {}
        self.token = "tok"
        self.closed = False

    def check_token(self):
        return True

    def _maybe(self, key):
        err = self._raise_on.get(key)
        if err:
            raise err

    def get_students(self):
        self._maybe("get_students")
        return self._students

    def get_homework(self, student):
        self._maybe("dashboard")
        return {"status": True, "data": {"dataTable": []}}

    def get_homework_pupilcard(self, params):
        self._maybe("pupilcard")
        return self._hw

    def get_messages_inbox(self):
        self._maybe("messages")
        return self._messages

    def close(self):
        self.closed = True


def make_coord(creds=None):
    c = SmartSchoolCoordinator.__new__(SmartSchoolCoordinator)
    c._creds = creds or BioCredentials(bio_login="B", unique_id="u", is_mobile=True)
    c._client = None
    return c


# ---------------------------------------------------------------- success
def test_fetch_success():
    c = make_coord()
    c._client = FakeClient()
    data = c._fetch()
    assert [s.student_id for s in data.students] == ["stu-1"]
    assert len(data.homework["stu-1"]) == 1
    assert data.homework["stu-1"][0].subject == "מתמטיקה"


def test_fetch_source_fallback_to_dashboard():
    # PupilCard fails -> dashboard is tried (returns empty, but no error)
    c = make_coord()
    c._client = FakeClient(raise_on={"pupilcard": ApiError("view is blocked")})
    data = c._fetch()
    assert data.homework["stu-1"] == []  # dashboard body had no homework


# ---------------------------------------------------------------- transient
def test_transient_api_error_becomes_update_failed():
    c = make_coord()
    c._client = FakeClient(raise_on={"get_students": ApiError("boom")})
    with pytest.raises(UpdateFailed):
        c._fetch()


def test_transient_request_failed_becomes_update_failed():
    c = make_coord()
    c._client = FakeClient(raise_on={"get_students": RequestFailed("timeout")})
    with pytest.raises(UpdateFailed):
        c._fetch()


def test_all_homework_sources_fail_is_update_failed():
    c = make_coord()
    c._client = FakeClient(raise_on={
        "pupilcard": RequestFailed("down"),
        "dashboard": RequestFailed("down"),
    })
    with pytest.raises(UpdateFailed):
        c._fetch()


# ---------------------------------------------------------------- token expiry / retry
def test_token_expired_midcycle_then_retry_succeeds(monkeypatch):
    c = make_coord()
    c._client = FakeClient(raise_on={"get_students": TokenExpired("401")})
    # the retry mints a fresh (working) client
    fresh = FakeClient()
    monkeypatch.setattr(c, "_mint_client", lambda: fresh)
    data = c._fetch()
    assert [s.student_id for s in data.students] == ["stu-1"]
    assert c._client is fresh


def test_exhausted_retry_becomes_auth_failed(monkeypatch):
    # both the initial client and the freshly minted one reject the token
    c = make_coord()
    c._client = FakeClient(raise_on={"get_students": TokenExpired("401")})
    monkeypatch.setattr(
        c, "_mint_client",
        lambda: FakeClient(raise_on={"get_students": TokenExpired("401 again")}),
    )
    with pytest.raises(ConfigEntryAuthFailed):
        c._fetch()


# ---------------------------------------------------------------- minting / auth mapping
def test_mint_client_rejected_credential_is_auth_failed(monkeypatch):
    class RejectingClient:
        def __init__(self, *a, **k):
            pass
        def login_by_bio(self, **kw):
            raise ApiError("status false")
        def close(self):
            pass
    monkeypatch.setattr(coord_mod, "WebtopClient", RejectingClient)
    c = make_coord()
    with pytest.raises(ConfigEntryAuthFailed):
        c._mint_client()


def test_mint_client_no_token_is_auth_failed(monkeypatch):
    class NoTokenClient:
        def __init__(self, *a, **k):
            pass
        def login_by_bio(self, **kw):
            return None
        def close(self):
            pass
    monkeypatch.setattr(coord_mod, "WebtopClient", NoTokenClient)
    c = make_coord()
    with pytest.raises(ConfigEntryAuthFailed):
        c._mint_client()


def test_mint_client_transient_propagates_not_auth(monkeypatch):
    # RequestFailed (transport) must NOT become ConfigEntryAuthFailed
    class FlakyClient:
        def __init__(self, *a, **k):
            pass
        def login_by_bio(self, **kw):
            raise RequestFailed("timeout")
        def close(self):
            pass
    monkeypatch.setattr(coord_mod, "WebtopClient", FlakyClient)
    c = make_coord()
    with pytest.raises(RequestFailed):
        c._mint_client()


def test_mint_client_success_returns_client(monkeypatch):
    class GoodClient:
        def __init__(self, *a, **k):
            self.token = ""
        def login_by_bio(self, **kw):
            self.token = "fresh"
            return "fresh"
        def close(self):
            pass
    monkeypatch.setattr(coord_mod, "WebtopClient", GoodClient)
    c = make_coord()
    client = c._mint_client()
    assert client.token == "fresh"


def test_transient_while_minting_becomes_update_failed(monkeypatch):
    # No existing client -> _ensure_client mints; a transient mint failure
    # must surface as UpdateFailed, not crash or reauth.
    class FlakyClient:
        def __init__(self, *a, **k):
            pass
        def login_by_bio(self, **kw):
            raise RequestFailed("timeout")
        def close(self):
            pass
    monkeypatch.setattr(coord_mod, "WebtopClient", FlakyClient)
    c = make_coord()  # c._client is None
    with pytest.raises(UpdateFailed):
        c._fetch()
