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


# ---------------------------------------------------------------- mint cleanup
class _TrackingClient:
    """login_by_bio behaviour is parametrised; records close()."""
    instances = []

    def __init__(self, *a, **k):
        self.closed = False
        self.token = ""
        _TrackingClient.instances.append(self)

    def login_by_bio(self, **kw):
        if self._mode == "reject":
            raise ApiError("status false")
        if self._mode == "transient":
            raise RequestFailed("timeout")
        if self._mode == "none":
            return None
        self.token = "fresh"
        return "fresh"

    def close(self):
        self.closed = True


def _mint_with(monkeypatch, mode):
    _TrackingClient.instances = []

    def factory(*a, **k):
        c = _TrackingClient()
        c._mode = mode
        return c

    monkeypatch.setattr(coord_mod, "WebtopClient", factory)
    return make_coord()


def test_mint_closes_client_on_rejected_credential(monkeypatch):
    c = _mint_with(monkeypatch, "reject")
    with pytest.raises(ConfigEntryAuthFailed):
        c._mint_client()
    assert _TrackingClient.instances[0].closed is True


def test_mint_closes_client_on_transient(monkeypatch):
    c = _mint_with(monkeypatch, "transient")
    with pytest.raises(RequestFailed):
        c._mint_client()
    assert _TrackingClient.instances[0].closed is True


def test_mint_closes_client_on_missing_token(monkeypatch):
    c = _mint_with(monkeypatch, "none")
    with pytest.raises(ConfigEntryAuthFailed):
        c._mint_client()
    assert _TrackingClient.instances[0].closed is True


def test_mint_does_not_close_on_success(monkeypatch):
    c = _mint_with(monkeypatch, "ok")
    client = c._mint_client()
    assert client.closed is False and client.token == "fresh"


# ---------------------------------------------------------------- empty discovery (finding 5)
def test_empty_student_list_is_update_failed():
    c = make_coord()
    c._client = FakeClient(students=[])
    with pytest.raises(UpdateFailed):
        c._fetch()


# ---------------------------------------------------------------- inbox isolation (finding 4)
def test_inbox_failure_keeps_homework_and_previous_messages():
    c = make_coord()
    # seed a previous snapshot with one message
    prev_msg = object()
    c.data = coord_mod.SmartSchoolData(students=[STUDENT], homework={}, messages=[prev_msg])
    c._client = FakeClient(raise_on={"messages": RequestFailed("inbox down")})
    data = c._fetch()
    # homework still published
    assert len(data.homework["stu-1"]) == 1
    # previous messages retained, not wiped
    assert data.messages == [prev_msg]
    assert data.messages_fresh is False


def test_inbox_failure_with_no_previous_snapshot_yields_empty():
    c = make_coord()
    c.data = None
    c._client = FakeClient(raise_on={"messages": ApiError("blip")})
    data = c._fetch()
    assert data.messages == [] and len(data.homework["stu-1"]) == 1
    assert data.messages_fresh is False


# ---------------------------------------------------------------- client cleanup on renewal (finding 2)
def test_ensure_client_closes_replaced_expired_client(monkeypatch):
    c = make_coord()
    old = FakeClient()
    old.check_token = lambda: False   # force renewal
    c._client = old
    fresh = FakeClient()
    monkeypatch.setattr(c, "_mint_client", lambda: fresh)
    got = c._ensure_client()
    assert got is fresh
    assert old.closed is True, "the replaced expired client must be closed"


def test_ensure_client_keeps_valid_client():
    c = make_coord()
    good = FakeClient()  # check_token() -> True
    c._client = good
    assert c._ensure_client() is good and good.closed is False


# ---------------------------------------------------------------- shutdown hook (finding 1)
def test_async_shutdown_client_closes_and_clears():
    c = make_coord()
    client = FakeClient()
    c._client = client
    c.async_shutdown_client()
    assert client.closed is True and c._client is None


def test_async_shutdown_client_noop_when_no_client():
    c = make_coord()
    c._client = None
    c.async_shutdown_client()  # must not raise


# ---------------------------------------------------------------- synthetic date uses HA tz (finding follow-up)
def test_fetch_homework_stamps_synthetic_date_in_ha_timezone(monkeypatch):
    """A dateless dashboard row must be stamped with 'today' in HA's timezone,
    the same clock the sensors use - not the host clock."""
    import datetime as _dt

    # Force PupilCard to fail so the dashboard fallback is used.
    c = make_coord()
    fake = FakeClient(raise_on={"pupilcard": ApiError("view is blocked")})
    # A dashboard body with a dateless homework row.
    fake.get_homework = lambda student: {
        "status": True,
        "data": {"dataTable": [{"lesson": "חשבון", "teacher": "T", "homeworkData": "עמוד 12"}]},
    }
    c._client = fake

    # Pin HA's "now" to a fixed date distinct from any host value.
    fixed = _dt.datetime(2026, 1, 15, 23, 30)
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: fixed)

    items, full = c._fetch_homework(fake, STUDENT)
    assert len(items) == 1
    assert items[0].date == "2026-01-15", "synthetic date must come from HA's clock"
    assert items[0].date_is_synthetic is True
    assert full is False, "dashboard fallback is a partial (today-only) window"


def test_collect_marks_pupilcard_full_and_dashboard_partial():
    # PupilCard succeeds -> full window.
    c = make_coord()
    c._client = FakeClient()
    data = c._fetch()
    assert data.full_window["stu-1"] is True

    # PupilCard fails -> dashboard fallback -> partial window.
    c2 = make_coord()
    c2._client = FakeClient(raise_on={"pupilcard": ApiError("view is blocked")})
    data2 = c2._fetch()
    assert data2.full_window["stu-1"] is False
