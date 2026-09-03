"""Tests for the school message inbox: model, client, state and notification.

Fixtures are the real payload shape captured from
messageBox/GetMessagesInbox on 2026-09-03.
"""

import json
from pathlib import Path

import pytest

from smartschool.client import WebtopClient
from smartschool.exceptions import ApiError, TokenExpired
from smartschool.models import Message
from smartschool.notifiers import Notifier
from smartschool.state import SeenState

# Exact wire shape, including the misleading student_F_name / student_L_name
# fields that actually hold the *sender's* name, and 0/1 ints for booleans.
RAW_MESSAGES = [
    {
        "typeID": 1,
        "subject": "הודעת בית ספר לדוגמה",
        "sendingDate": "2026-09-02T13:13:00",
        "userTitle": None,
        "student_F_name": "דוד",
        "student_L_name": "כהן",
        "userType": 1,
        "classCode": "0",
        "classNumber": 0,
        "hasRead": 1,
        "filesWereAttached": 0,
        "rowNumber": 1,
    },
    {
        "typeID": 1,
        "subject": "הודעה נוספת לדוגמה",
        "sendingDate": "2026-09-01T08:30:00",
        "student_F_name": "שרה",
        "student_L_name": "מזרחי",
        "hasRead": 0,
        "filesWereAttached": 1,
        "rowNumber": 2,
    },
]


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
    client = WebtopClient("tok")

    def fake_post(url, json=None, timeout=None):
        if capture is not None:
            capture.append({"url": url, "json": json})
        return response

    monkeypatch.setattr(client._session, "post", fake_post)
    return client


# ----------------------------------------------------------------------
# model
# ----------------------------------------------------------------------
def test_sender_is_built_from_the_misnamed_student_fields():
    m = Message.from_api(RAW_MESSAGES[0])
    # student_F_name/student_L_name are the sender, family name first.
    assert m.sender == "כהן דוד"
    assert m.subject.startswith("הודעת בית ספר")


def test_int_flags_become_booleans():
    read, unread = Message.from_api(RAW_MESSAGES[0]), Message.from_api(RAW_MESSAGES[1])
    assert read.has_read is True and read.has_files is False
    assert unread.has_read is False and unread.has_files is True


def test_identity_ignores_read_state():
    """Reading a message in the browser must not make it look new again."""
    raw_unread = dict(RAW_MESSAGES[0], hasRead=0)
    assert Message.from_api(RAW_MESSAGES[0]).identity() == Message.from_api(raw_unread).identity()


def test_identity_differs_per_message():
    a, b = Message.from_api(RAW_MESSAGES[0]), Message.from_api(RAW_MESSAGES[1])
    assert a.identity() != b.identity()


def test_model_tolerates_missing_fields():
    m = Message.from_api({"subject": "bare"})
    assert m.subject == "bare"
    assert m.sender == "" and m.has_read is False and m.has_files is False


def test_model_falls_back_to_from_title_for_sender():
    m = Message.from_api({"subject": "x", "fromTitle": "מנהלת בית הספר"})
    assert m.sender == "מנהלת בית הספר"


# ----------------------------------------------------------------------
# client
# ----------------------------------------------------------------------
def test_get_messages_inbox_sends_expected_payload(monkeypatch):
    capture = []
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": RAW_MESSAGES}), capture
    )
    msgs = client.get_messages_inbox()
    assert len(msgs) == 2
    assert capture[0]["url"].endswith("/server/api/messageBox/GetMessagesInbox")
    assert capture[0]["json"] == {
        "PageId": 1,
        "LabelId": 0,
        "HasRead": None,   # None = all messages, read and unread
        "SearchQuery": "",
    }


def test_get_messages_inbox_handles_empty(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, {"status": True, "data": []}))
    assert client.get_messages_inbox() == []


def test_get_messages_inbox_tolerates_non_list_data(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(200, {"status": True, "data": {"oops": 1}}))
    assert client.get_messages_inbox() == []


def test_get_messages_inbox_skips_non_dict_entries(monkeypatch):
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": True, "data": [RAW_MESSAGES[0], "junk", None]})
    )
    assert len(client.get_messages_inbox()) == 1


def test_get_messages_inbox_propagates_expiry(monkeypatch):
    client = make_client(monkeypatch, FakeResponse(401, None, text=""))
    with pytest.raises(TokenExpired):
        client.get_messages_inbox()


def test_get_messages_inbox_raises_on_status_false(monkeypatch):
    client = make_client(
        monkeypatch, FakeResponse(200, {"status": False, "errorDescription": "nope"})
    )
    with pytest.raises(ApiError):
        client.get_messages_inbox()


# ----------------------------------------------------------------------
# state
# ----------------------------------------------------------------------
def test_new_messages_reported_once(tmp_path: Path):
    state = SeenState(tmp_path / "m.json", label="message")
    msgs = [Message.from_api(r) for r in RAW_MESSAGES]

    new, _ = state.diff("__inbox__", msgs)
    assert len(new) == 2
    new, _ = state.diff("__inbox__", msgs)
    assert new == [], "already-seen messages must not re-notify"


def test_reading_a_message_does_not_renotify(tmp_path: Path):
    state = SeenState(tmp_path / "m.json", label="message")
    state.diff("__inbox__", [Message.from_api(RAW_MESSAGES[1])])
    # same message, now marked read in the browser
    read = Message.from_api(dict(RAW_MESSAGES[1], hasRead=1))
    assert state.diff("__inbox__", [read])[0] == []


def test_message_state_persists(tmp_path: Path):
    path = tmp_path / "m.json"
    msgs = [Message.from_api(r) for r in RAW_MESSAGES]
    s1 = SeenState(path, label="message")
    s1.diff("__inbox__", msgs)
    s1.save()
    assert SeenState(path, label="message").diff("__inbox__", msgs)[0] == []


def test_message_state_is_separate_from_homework(tmp_path: Path):
    """Homework and messages must not share a state file or bucket."""
    from smartschool.models import HomeworkItem

    hw_state = SeenState(tmp_path / "hw.json", label="homework")
    msg_state = SeenState(tmp_path / "msg.json", label="message")
    hw_state.diff("student", [HomeworkItem(subject="חשבון", homework="x", date="2026-09-02")])
    hw_state.save()
    # A fresh message state must still consider the inbox unseen.
    assert msg_state.is_first_run_for("__inbox__") is True


# ----------------------------------------------------------------------
# notification
# ----------------------------------------------------------------------
class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent = []
        self.apobj = _FakeApprise()
        self.mqtt_client = None
        self._discovery_sent = set()

    def _send(self, title, body):
        self.sent.append((title, body))
        return True


class _FakeApprise:
    def __len__(self):
        return 1


def test_notification_lists_newest_first_with_flags():
    n = RecordingNotifier()
    n.notify_new_messages([Message.from_api(r) for r in RAW_MESSAGES])
    title, body = n.sent[0]
    assert "2 new messages" in title
    # newest (09-02) must precede older (09-01)
    assert body.index("הודעת בית ספר") < body.index("הודעה נוספת")
    assert "has attachment" in body, "attachment flag must be surfaced"
    assert "unread" in body


def test_single_message_title_is_singular():
    n = RecordingNotifier()
    n.notify_new_messages([Message.from_api(RAW_MESSAGES[0])])
    assert n.sent[0][0] == "SmartSchool - new message"


def test_no_messages_sends_nothing():
    n = RecordingNotifier()
    n.notify_new_messages([])
    assert n.sent == []


# ----------------------------------------------------------------------
# MQTT
# ----------------------------------------------------------------------
class CapturingMqtt:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload


def test_message_state_payload():
    n = RecordingNotifier()
    n.mqtt_client = CapturingMqtt()
    n.publish_messages_state([Message.from_api(r) for r in RAW_MESSAGES])

    topic = "smartschool/inbox/state"
    payload = json.loads(n.mqtt_client.published[topic])
    assert payload["total"] == 2
    assert payload["unread"] == 1
    assert payload["latest"].startswith("הודעת בית ספר"), "latest = newest message"
    assert "הודעה נוספת" in payload["details"]


def test_latest_is_clipped_for_ha_state_limit():
    """HA caps sensor states at 255 chars."""
    n = RecordingNotifier()
    n.mqtt_client = CapturingMqtt()
    long_subject = "א" * 400
    n.publish_messages_state([Message.from_api({"subject": long_subject, "sendingDate": "2026-09-02T10:00:00"})])
    payload = json.loads(n.mqtt_client.published["smartschool/inbox/state"])
    assert len(payload["latest"]) <= 240


def test_empty_inbox_state():
    n = RecordingNotifier()
    n.mqtt_client = CapturingMqtt()
    n.publish_messages_state([])
    payload = json.loads(n.mqtt_client.published["smartschool/inbox/state"])
    assert payload == {
        **payload,
        "total": 0,
        "unread": 0,
        "latest": "No messages",
        "details": "No messages",
    }


def test_messages_discovery_publishes_five_sensors_once():
    n = RecordingNotifier()
    n.mqtt_client = CapturingMqtt()
    n.publish_messages_discovery()
    configs = [t for t in n.mqtt_client.published if t.startswith("homeassistant/sensor/")]
    assert len(configs) == 5
    # its own device, not attached to a student
    cfg = json.loads(n.mqtt_client.published[configs[0]])
    assert cfg["device"]["name"] == "SmartSchool - Messages"

    n.mqtt_client.published.clear()
    n.publish_messages_discovery()
    assert n.mqtt_client.published == {}, "discovery must publish only once"


# ----------------------------------------------------------------------
# monitor integration
# ----------------------------------------------------------------------
def _monitor(tmp_path, monkeypatch, messages, seed_quietly="1", enabled="1"):
    from smartschool.config import Config, Paths
    from smartschool.monitor import Monitor

    monkeypatch.setenv("NOTIFIERS", "")
    monkeypatch.setenv("MQTT_BROKER", "")
    monkeypatch.setenv("SEED_QUIETLY", seed_quietly)
    monkeypatch.setenv("MESSAGES_ENABLED", enabled)

    paths = Paths(root=tmp_path)
    paths.ensure()
    (paths.config_dir / "token.txt").write_text("tok", encoding="utf-8")

    m = Monitor(Config(paths))
    assert m.connect()
    monkeypatch.setattr(m.client, "get_messages_inbox", lambda **kw: messages)

    sent = []
    monkeypatch.setattr(m.notifier, "notify_new_messages", lambda msgs: sent.append(msgs))
    return m, sent


def test_monitor_seeds_inbox_quietly_on_first_run(tmp_path, monkeypatch):
    msgs = [Message.from_api(r) for r in RAW_MESSAGES]
    m, sent = _monitor(tmp_path, monkeypatch, msgs)

    m.check_messages()
    assert sent == [], "first run must not notify about pre-existing messages"
    # ...but it must be recorded
    assert m.messages_state.is_first_run_for("__inbox__") is False


def test_monitor_notifies_about_a_genuinely_new_message(tmp_path, monkeypatch):
    msgs = [Message.from_api(RAW_MESSAGES[0])]
    m, sent = _monitor(tmp_path, monkeypatch, msgs)
    m.check_messages()          # seeds
    assert sent == []

    # a new message arrives
    newer = Message.from_api(
        {"subject": "אסיפת הורים", "sendingDate": "2026-09-03T09:00:00",
         "student_F_name": "מיכל", "student_L_name": "לוי", "hasRead": 0}
    )
    m.client.get_messages_inbox = lambda **kw: [newer] + msgs
    m.check_messages()

    assert len(sent) == 1
    assert [x.subject for x in sent[0]] == ["אסיפת הורים"]


def test_monitor_notifies_on_first_run_when_seeding_disabled(tmp_path, monkeypatch):
    msgs = [Message.from_api(r) for r in RAW_MESSAGES]
    m, sent = _monitor(tmp_path, monkeypatch, msgs, seed_quietly="0")
    m.check_messages()
    assert len(sent) == 1 and len(sent[0]) == 2


def test_monitor_skips_inbox_when_disabled(tmp_path, monkeypatch):
    called = []
    msgs = [Message.from_api(RAW_MESSAGES[0])]
    m, sent = _monitor(tmp_path, monkeypatch, msgs, enabled="0")
    m.client.get_messages_inbox = lambda **kw: called.append(1) or msgs
    m.check_messages()
    assert called == [], "MESSAGES_ENABLED=0 must not hit the API"
    assert sent == []


def test_message_failure_does_not_break_homework(tmp_path, monkeypatch):
    """The inbox is a bonus; homework is the job."""
    from smartschool.exceptions import RequestFailed

    m, sent = _monitor(tmp_path, monkeypatch, [])

    def boom(**kw):
        raise RequestFailed("inbox exploded")

    m.client.get_messages_inbox = boom
    homework_ran = []
    monkeypatch.setattr(m, "check_once", lambda: homework_ran.append(1))

    m.check_all()   # must not raise
    assert homework_ran == [1], "homework check must still run"
    assert sent == []


def test_expired_token_during_message_check_drops_client(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path, monkeypatch, [])

    def expired(**kw):
        raise TokenExpired("401")

    m.client.get_messages_inbox = expired
    m.check_messages()
    assert m.client is None, "expiry must drop the client so a new paste is picked up"
