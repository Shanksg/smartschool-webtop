"""Offline tests for the client envelope handling, rotation detection and
token storage. All HTTP is stubbed - nothing here touches the network."""

import json
from pathlib import Path

import pytest
import requests

from smartschool.client import WebtopClient
from smartschool.exceptions import ApiError, RequestFailed, TokenExpired, TokenMissing
from smartschool.models import Student, TokenState
from smartschool.session import TokenStore


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
    client = WebtopClient("tok-initial")

    def fake_post(url, json=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "json": json})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(client._session, "post", fake_post)
    return client


# ----------------------------------------------------------------------
# envelope handling
# ----------------------------------------------------------------------
def test_401_raises_token_expired(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(401, None, text="Unauthorized"))
    with pytest.raises(TokenExpired):
        client.init_dashboard()


def test_status_false_raises_api_error_with_description(monkeypatch):
    body = {"status": False, "data": None, "errorDescription": "view is blocked", "errorId": None}
    client = make_client(monkeypatch, FakeResponse(200, body))
    with pytest.raises(ApiError) as exc:
        client.init_dashboard()
    assert exc.value.error_description == "view is blocked"


def test_non_json_body_raises_request_failed(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, None, text="<html>nope</html>"))
    with pytest.raises(RequestFailed):
        client.init_dashboard()


def test_http_500_raises_request_failed(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(500, None, text="boom"))
    with pytest.raises(RequestFailed):
        client.init_dashboard()


def test_timeout_raises_request_failed(monkeypatch):
    client = make_client(monkeypatch, requests.exceptions.Timeout())
    with pytest.raises(RequestFailed):
        client.init_dashboard()


def test_successful_envelope_returns_body(monkeypatch):
    body = {"status": True, "data": [{"id": "abc", "classCode": 3}]}
    client = make_client(monkeypatch, FakeResponse(200, body))
    assert client.init_dashboard() == body


# ----------------------------------------------------------------------
# check_token replaces the old always-True validate_token
# ----------------------------------------------------------------------
def test_check_token_false_on_401(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(401, None, text=""))
    assert client.check_token() is False


def test_check_token_false_on_status_false(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, {"status": False, "errorDescription": "x"}))
    assert client.check_token() is False


def test_check_token_true_on_success(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, {"status": True, "data": True}))
    assert client.check_token() is True


# ----------------------------------------------------------------------
# student discovery - dynamic params, no frozen school year
# ----------------------------------------------------------------------
def test_get_students_from_list_payload(monkeypatch):
    body = {
        "status": True,
        "data": [
            {
                "id": "enc-id-1",
                "studentName": "דני כהן",
                "classCode": 3,
                "classNumber": 4,
                "studyYear": 2027,
            }
        ],
    }
    client = make_client(monkeypatch, FakeResponse(200, body))
    students = client.get_students()
    assert len(students) == 1
    s = students[0]
    assert s.student_id == "enc-id-1"
    assert (s.class_code, s.class_number, s.study_year) == (3, 4, 2027)


def test_get_students_from_wrapped_payload(monkeypatch):
    body = {"status": True, "data": {"students": [{"id": "x", "ClassCode": 5}]}}
    client = make_client(monkeypatch, FakeResponse(200, body))
    students = client.get_students()
    assert len(students) == 1 and students[0].class_code == 5


def test_get_students_empty_when_unrecognisable(monkeypatch):
    body = {"status": True, "data": {"config": {"theme": "dark"}}}
    client = make_client(monkeypatch, FakeResponse(200, body))
    assert client.get_students() == []


def test_student_from_api_builds_name_from_parts():
    s = Student.from_api({"id": "i", "firstName": "דני", "lastName": "כהן"})
    assert s.name == "דני כהן"


# ----------------------------------------------------------------------
# homework request shape
# ----------------------------------------------------------------------
def test_get_homework_sends_expected_payload(monkeypatch):
    capture = []
    client = make_client(monkeypatch, FakeResponse(200, {"status": True, "data": []}), capture)
    student = Student(student_id="enc", name="x", class_code=3, class_number=4)
    client.get_homework(student)
    assert capture[0]["json"] == {"id": "enc", "ClassCode": 3, "ClassNumber": 4}
    assert capture[0]["url"].endswith("/server/api/dashboard/GetHomeWork")


# ----------------------------------------------------------------------
# rotation detection
# ----------------------------------------------------------------------
def test_refresh_token_detects_rotation(monkeypatch):
    client = WebtopClient("old-token")

    def fake_post(url, json=None, timeout=None):
        # Simulate the server rotating the cookie.
        client._session.cookies.set("webToken", "new-token", domain=".smartschool.co.il")
        return FakeResponse(
            200,
            {"status": True, "data": True},
            headers={"Set-Cookie": "webToken=new-token; expires=Fri, 04 Sep 2026 10:00:00 GMT; path=/"},
        )

    monkeypatch.setattr(client._session, "post", fake_post)
    result = client.refresh_token()
    assert result.rotated is True
    assert result.new_token == "new-token"
    assert result.expires == "Fri, 04 Sep 2026 10:00:00 GMT"
    assert client.token == "new-token"


def test_refresh_token_reports_no_rotation(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, {"status": True, "data": True}))
    result = client.refresh_token()
    assert result.rotated is False
    assert result.new_token is None


def test_refresh_token_propagates_expiry(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(401, None, text=""))
    with pytest.raises(TokenExpired):
        client.refresh_token()


# ----------------------------------------------------------------------
# token handling
# ----------------------------------------------------------------------
def test_set_token_url_decodes():
    client = WebtopClient("abc%2Fdef%3D")
    assert client.token == "abc/def="


def test_tls_verification_on_by_default():
    assert WebtopClient("t")._session.verify is True


# ----------------------------------------------------------------------
# TokenStore
# ----------------------------------------------------------------------
def test_store_raises_when_no_token(tmp_path: Path):
    with pytest.raises(TokenMissing):
        TokenStore(tmp_path).load()


def test_pasted_token_wins_over_cache(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save(TokenState(token="cached", obtained_at="2026-01-01T00:00:00"))
    (tmp_path / "token.txt").write_text("freshly-pasted", encoding="utf-8")

    state = store.load()
    assert state.token == "freshly-pasted"
    assert state.rotated_count == 0


def test_cache_used_when_token_file_is_already_ingested(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save(
        TokenState(
            token="same",
            obtained_at="2026-01-01T00:00:00",
            rotated_count=7,
            pasted_token="same",
        )
    )
    (tmp_path / "token.txt").write_text("same", encoding="utf-8")

    state = store.load()
    assert state.token == "same"
    assert state.rotated_count == 7, "rotation history must not be lost"


def test_pre_upgrade_cache_without_pasted_token_is_reingested(tmp_path: Path):
    """A cache written before pasted_token existed has no record of the paste.

    Re-ingesting is the safe fallback: the token value is unchanged, so only the
    rotation counter resets. This is a one-time cost on upgrade.
    """
    store = TokenStore(tmp_path)
    store.save(TokenState(token="same", obtained_at="2026-01-01T00:00:00", rotated_count=7))
    (tmp_path / "token.txt").write_text("same", encoding="utf-8")

    state = store.load()
    assert state.token == "same", "the token itself must survive"
    assert state.rotated_count == 0
    assert state.pasted_token == "same", "and it is recorded from now on"


def test_record_rotation_increments_and_persists(tmp_path: Path):
    store = TokenStore(tmp_path)
    state = TokenState(token="t0", obtained_at="2026-01-01T00:00:00")
    store.save(state)

    updated = store.record_rotation(state, "t1", "Fri, 04 Sep 2026 10:00:00 GMT")
    assert updated.token == "t1"
    assert updated.rotated_count == 1

    reloaded = store.load()
    assert reloaded.token == "t1" and reloaded.rotated_count == 1


def test_store_migrates_legacy_per_username_cache(tmp_path: Path):
    """The old format was {"STUDENT1": {"token": ..., "timestamp": ...}}."""
    (tmp_path / "token_cache.json").write_text(
        json.dumps({"STUDENT1": {"token": "legacy-token", "timestamp": "2026-02-03T12:00:00"}}),
        encoding="utf-8",
    )
    state = TokenStore(tmp_path).load()
    assert state.token == "legacy-token"


def test_store_migrates_legacy_repr_string_cache(tmp_path: Path):
    """Some caches stored the inner dict as a Python repr string."""
    (tmp_path / "token_cache.json").write_text(
        json.dumps({"STUDENT1": "{'token': 'repr-token', 'timestamp': '2026-02-03T12:00:00'}"}),
        encoding="utf-8",
    )
    state = TokenStore(tmp_path).load()
    assert state.token == "repr-token"


def test_store_ignores_corrupt_cache(tmp_path: Path):
    (tmp_path / "token_cache.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "token.txt").write_text("fallback", encoding="utf-8")
    assert TokenStore(tmp_path).load().token == "fallback"


# ----------------------------------------------------------------------
# token replacement lifecycle - "paste today, paste a new one tomorrow"
# ----------------------------------------------------------------------
def test_rotation_is_not_undone_by_the_stale_token_file(tmp_path: Path):
    """Regression: after rotating, the cache is AHEAD of token.txt.

    Reloading must keep the rotated token, not revert to the original paste
    still sitting in the file.
    """
    store = TokenStore(tmp_path)
    (tmp_path / "token.txt").write_text("T1", encoding="utf-8")

    state = store.load()
    assert state.token == "T1"

    # Server rotates twice while token.txt still says T1.
    state = store.record_rotation(state, "T2", None)
    state = store.record_rotation(state, "T3", None)

    reloaded = store.load()
    assert reloaded.token == "T3", "must not revert to the stale pasted token"
    assert reloaded.rotated_count == 2


def test_a_genuinely_new_paste_overrides_a_rotated_cache(tmp_path: Path):
    """Tomorrow's replacement token must win, even mid-rotation."""
    store = TokenStore(tmp_path)
    (tmp_path / "token.txt").write_text("T1", encoding="utf-8")

    state = store.load()
    state = store.record_rotation(state, "T2", None)
    assert store.load().token == "T2"

    # Next day: paste a fresh token.
    (tmp_path / "token.txt").write_text("NEW", encoding="utf-8")
    fresh = store.load()
    assert fresh.token == "NEW"
    assert fresh.rotated_count == 0, "rotation counter resets for a new session"
    assert fresh.pasted_token == "NEW"


def test_repeated_loads_are_stable(tmp_path: Path):
    """Loading twice must not keep re-ingesting the same paste."""
    store = TokenStore(tmp_path)
    (tmp_path / "token.txt").write_text("T1", encoding="utf-8")

    first = store.load()
    first = store.record_rotation(first, "T2", None)
    for _ in range(3):
        assert store.load().token == "T2"


def test_expired_client_is_dropped_so_a_new_paste_is_picked_up(tmp_path: Path, monkeypatch):
    """_handle_expired must clear the client so the next cycle re-reads the file."""
    from smartschool.config import Config, Paths
    from smartschool.monitor import Monitor

    paths = Paths(root=tmp_path)
    paths.ensure()
    (paths.config_dir / "token.txt").write_text("T1", encoding="utf-8")

    monkeypatch.setenv("NOTIFIERS", "")
    monkeypatch.setenv("MQTT_BROKER", "")
    monitor = Monitor(Config(paths))

    assert monitor.connect() is True
    assert monitor.client is not None

    monitor._handle_expired()
    assert monitor.client is None, "client must be dropped on expiry"

    # Simulate the user pasting a replacement.
    (paths.config_dir / "token.txt").write_text("T2", encoding="utf-8")
    assert monitor.connect() is True
    assert monitor.client.token == "T2", "new paste must be picked up without a restart"


def test_refresh_token_tolerates_non_dict_body(monkeypatch):
    """CheckBackgroundToken returning a non-dict JSON must not crash body.get."""
    client = make_client(monkeypatch, FakeResponse(200, [1, 2, 3]))
    result = client.refresh_token()
    assert result.ok is False and result.rotated is False
