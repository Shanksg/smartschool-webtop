"""Regression tests for the PR review fixes."""

import json
from pathlib import Path

import pytest

from smartschool.bio import BioCredentials
from smartschool.client import WebtopClient
from smartschool.exceptions import ApiError, RequestFailed
from smartschool.models import HomeworkItem, Message, Student, TokenState
from smartschool.session import TokenStore
from smartschool.state import SeenState


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


def _client(monkeypatch, response):
    c = WebtopClient("t")
    monkeypatch.setattr(c._session, "post", lambda *a, **k: response)
    return c


# ---- #5 bio.load rejects non-object JSON ----
@pytest.mark.parametrize("payload", ["[]", "null", "42", '"str"'])
def test_bio_load_rejects_non_object_json(tmp_path, payload):
    (tmp_path / "bio_credentials.json").write_text(payload, encoding="utf-8")
    assert BioCredentials.load(tmp_path) is None


# ---- #6 get_students filters a mixed list ----
def test_get_students_skips_non_dicts_in_fallback_list(monkeypatch):
    body = {"status": True, "data": {"misc": [{"id": "a", "classCode": 3}, None, "junk"]}}
    students = _client(monkeypatch, FakeResponse(200, body)).get_students()
    assert len(students) == 1 and students[0].student_id == "a"


# ---- #8 fetch_homework raises on total failure instead of returning [] ----
def _monitor(tmp_path, monkeypatch):
    from smartschool.config import Config, Paths
    from smartschool.monitor import Monitor

    monkeypatch.setenv("NOTIFIERS", "")
    monkeypatch.setenv("MQTT_BROKER", "")
    paths = Paths(root=tmp_path)
    paths.ensure()
    (paths.config_dir / "token.txt").write_text("tok", encoding="utf-8")
    m = Monitor(Config(paths))
    m.connect()
    return m


def test_fetch_homework_raises_when_all_sources_fail(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    student = Student(student_id="x", name="kid", class_code=3, class_number=1)

    def boom(*a, **k):
        raise RequestFailed("down")

    monkeypatch.setattr(m.client, "get_homework", boom)
    monkeypatch.setattr(m.client, "get_homework_pupilcard", boom)
    with pytest.raises(RequestFailed):
        m.fetch_homework(student)


def test_check_once_skips_state_on_fetch_failure(tmp_path, monkeypatch):
    """An outage must not wipe seen state and re-notify on recovery."""
    m = _monitor(tmp_path, monkeypatch)
    student = Student(student_id="x", name="kid", class_code=3, class_number=1)
    monkeypatch.setattr(m.client, "get_students", lambda: [student])

    # Pre-seed one assignment as already seen.
    hw = HomeworkItem(subject="מתמטיקה", homework="p45", date="2026-09-02")
    m.state.diff("kid", [hw])
    m.state.save()

    monkeypatch.setattr(m, "fetch_homework", lambda s: (_ for _ in ()).throw(RequestFailed("down")))
    notified = []
    monkeypatch.setattr(m.notifier, "notify_new_homework", lambda n, i: notified.append(i) or True)

    m.check_once()
    # state untouched -> on recovery the item is still 'seen', not re-notified
    assert m.state.diff("kid", [hw])[0] == []
    assert notified == []


# ---- #10 delivery failure rolls back so items retry ----
def test_message_retries_when_delivery_fails(tmp_path):
    state = SeenState(tmp_path / "m.json", label="message")
    msg = Message(subject="s", sender="x", sent_at="2026-09-02T10:00:00")
    new, _ = state.diff("__inbox__", [msg])
    assert len(new) == 1
    # simulate delivery failure -> roll back
    state.unsee("__inbox__", new)
    # next poll re-detects it
    assert len(state.diff("__inbox__", [msg])[0]) == 1


def test_unsee_is_safe_on_unknown_bucket(tmp_path):
    state = SeenState(tmp_path / "m.json")
    state.unsee("nope", [HomeworkItem(subject="a", homework="b", date="2026-09-02")])  # no raise


# ---- #9 _pupilcard_params matching ----
def test_pupilcard_params_no_cross_student_fallback(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    m.config.students = [
        {"name": "Alice", "student_params": {"studentID": "alice-id", "classCode": 3}},
        {"name": "Bob", "student_params": {"studentID": "bob-id", "classCode": 4}},
    ]
    # a discovered student with no runtime class data and matching neither entry
    unknown = Student(student_id="carol-id", name="Carol", class_code=None)
    assert m._pupilcard_params(unknown) is None, "must not borrow another child's params"

    # matches by id
    bob = Student(student_id="bob-id", name="Bob", class_code=None)
    assert m._pupilcard_params(bob)["studentID"] == "bob-id"


def test_pupilcard_single_configured_student_fallback(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    m.config.students = [{"name": "Only", "student_params": {"studentID": "only-id", "classCode": 3}}]
    unknown = Student(student_id="different", name="Other", class_code=None)
    assert m._pupilcard_params(unknown)["studentID"] == "only-id", "sole config is a safe fallback"


# ---- #13 record_rotation does not inherit old expiry ----
def test_record_rotation_drops_old_expiry(tmp_path):
    store = TokenStore(tmp_path)
    old = TokenState(token="old", expires_at="2020-01-01T00:00:00+00:00")
    updated = store.record_rotation(old, "new", expires=None)
    assert updated.expires_at is None, "a rotated token must not inherit the old absolute expiry"
    # so minutes_remaining falls back to the fresh obtained_at TTL
    assert updated.minutes_remaining(9) > 400


# ---- #1 connect() mints the first token via bio when none pasted ----
def test_connect_uses_bio_when_no_token(tmp_path, monkeypatch):
    from smartschool.config import Config, Paths
    from smartschool.monitor import Monitor

    monkeypatch.setenv("NOTIFIERS", "")
    monkeypatch.setenv("MQTT_BROKER", "")
    paths = Paths(root=tmp_path)
    paths.ensure()
    # no token.txt; only a bio credential
    (paths.config_dir / "bio_credentials.json").write_text(
        json.dumps({"bioLogin": "B", "uniqueId": "u", "selectedUser": "s", "isMobile": True}),
        encoding="utf-8",
    )
    m = Monitor(Config(paths))
    monkeypatch.setattr(WebtopClient, "login_by_bio", lambda self, **kw: "MINTED")
    assert m.connect() is True
    assert m.token_state.token == "MINTED"
