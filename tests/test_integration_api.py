"""Covers the vendored API copy under custom_components/smartschool/api/.

It is a copy of the standalone `smartschool` core (loguru swapped for stdlib
logging) that ships inside the HACS integration. These tests import it the way
the integration does and re-check the behaviours that matter, so the copy can't
silently drift from the original. No Home Assistant needed.
"""

import json
import os
import sys

import pytest

# Make `api` importable as a top-level package (as it resolves inside the
# integration) without importing custom_components.smartschool.__init__, which
# pulls in Home Assistant.
_API_PARENT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "smartschool"
)
if _API_PARENT not in sys.path:
    sys.path.insert(0, _API_PARENT)

import api.bio as bio  # noqa: E402
import api.client as client_mod  # noqa: E402
import api.exceptions as exc  # noqa: E402
import api.homework as hw  # noqa: E402
import api.models as models  # noqa: E402


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


def _client(monkeypatch, response, capture=None):
    c = client_mod.WebtopClient("tok")

    def fake_post(url, json=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "json": json})
        return response

    monkeypatch.setattr(c._session, "post", fake_post)
    return c


# ---- homework ----
def test_vendored_pupilcard_parser():
    items = hw.from_pupilcard(
        [{"date": "2026-09-12T00:00:00", "hoursData": [{"scheduale": [
            {"subject_name": "מתמטיקה", "teacher": "X", "homeWork": "עמוד 45"},
            {"subject_name": "אנגלית", "teacher": "Y", "homeWork": "אין"},  # filtered
        ]}]}]
    )
    assert [i.subject for i in items] == ["מתמטיקה"]


def test_vendored_no_homework_filter():
    assert hw.is_no_homework("אין") and hw.is_no_homework("לא הוזן")
    assert not hw.is_no_homework("עמוד 45")


def test_vendored_dashboard_parser():
    items = hw.from_dashboard(
        {"dataTable": [{"lesson": "חשבון", "teacher": "T", "homeworkData": "עמוד 12"}]},
        default_date="2026-09-12",
    )
    assert len(items) == 1 and items[0].subject == "חשבון"


# ---- models ----
def test_vendored_message_model():
    m = models.Message.from_api(
        {"subject": "s", "student_F_name": "a", "student_L_name": "b", "hasRead": 1, "filesWereAttached": 1}
    )
    assert m.sender == "b a" and m.has_read is True and m.has_files is True


def test_vendored_homework_identity_ignores_teacher():
    a = models.HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-12")
    b = models.HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-12T00:00:00", teacher="z")
    assert a.identity() == b.identity()


# ---- bio ----
def test_vendored_bio_rejects_non_object(tmp_path):
    (tmp_path / "bio_credentials.json").write_text("[]", encoding="utf-8")
    assert bio.BioCredentials.load(tmp_path) is None


def test_vendored_bio_defaults_is_mobile_true(tmp_path):
    (tmp_path / "bio_credentials.json").write_text(
        json.dumps({"bioLogin": "B", "uniqueId": "u"}), encoding="utf-8"
    )
    assert bio.BioCredentials.load(tmp_path).is_mobile is True


# ---- client ----
def test_vendored_client_401_raises_token_expired(monkeypatch):
    c = _client(monkeypatch, FakeResponse(401, None, text=""))
    with pytest.raises(exc.TokenExpired):
        c.init_dashboard()


def test_vendored_client_status_false_raises_api_error(monkeypatch):
    c = _client(monkeypatch, FakeResponse(200, {"status": False, "errorDescription": "view is blocked"}))
    with pytest.raises(exc.ApiError):
        c.init_dashboard()


def test_vendored_login_by_bio_payload(monkeypatch):
    cap = []
    c = _client(monkeypatch, FakeResponse(200, {"status": True, "data": {"token": "T"}}), cap)
    tok = c.login_by_bio(bio_login="B", device_id="", selected_user="s", unique_id="u", is_mobile=True, mode="2")
    assert tok == "T"
    assert cap[0]["url"].endswith("/server/api/user/loginByBio")
    assert cap[0]["json"]["param5"] == "true" and cap[0]["json"]["param2"] == ""


def test_vendored_client_tls_verify_on_by_default():
    assert client_mod.WebtopClient("t")._session.verify is True


def test_vendored_refresh_token_tolerates_non_dict_body(monkeypatch):
    """CheckBackgroundToken returning a JSON array/scalar must not crash."""
    c = _client(monkeypatch, FakeResponse(200, [1, 2, 3]))
    result = c.refresh_token()
    assert result.ok is False and result.rotated is False
