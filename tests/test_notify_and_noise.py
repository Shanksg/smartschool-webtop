"""Tests for the no-homework noise filter, the notification window and the
first-run seeding guard. No network, no real notifier."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from smartschool.homework import from_pupilcard, is_no_homework
from smartschool.models import HomeworkItem
from smartschool.notifiers import Notifier
from smartschool.state import HomeworkState


# ----------------------------------------------------------------------
# noise filter
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["אין", "אין שיעורי בית", "לא הוזן", "ללא", "none", "N/A", "-", "--", "", "   ", "אין.", "אין!"],
)
def test_no_homework_values_are_filtered(text):
    assert is_no_homework(text) is True


@pytest.mark.parametrize(
    "text",
    ["עמוד 12 תרגילים 1-5", "שיעורי בית לדוגמה", "אין צורך להביא מחשבון", "read chapter 4"],
)
def test_real_homework_is_kept(text):
    assert is_no_homework(text) is False


def test_no_homework_values_configurable(monkeypatch):
    monkeypatch.setenv("NO_HOMEWORK_VALUES", "nothing,שום דבר")
    assert is_no_homework("nothing") is True
    assert is_no_homework("שום דבר") is True
    # defaults no longer apply once overridden
    assert is_no_homework("אין") is False


def test_pupilcard_drops_the_none_entry():
    """Live case: the חשבון teacher typed 'אין' (none) on 2026-09-01."""
    data = [
        {
            "date": "2026-09-01T00:00:00",
            "hoursData": [
                {
                    "scheduale": [
                        {"subject_name": "חשבון", "teacher": "אברהם רותי", "homeWork": "אין"},
                        {"subject_name": "אנגלית", "teacher": "לוי מיכל", "homeWork": "תרגול לדוגמה"},
                    ]
                }
            ],
        }
    ]
    items = from_pupilcard(data)
    assert [i.subject for i in items] == ["אנגלית"]


# ----------------------------------------------------------------------
# notification window - any date, not just today
# ----------------------------------------------------------------------
class RecordingNotifier(Notifier):
    """Notifier with Apprise/MQTT replaced by a capture list."""

    def __init__(self):
        self.sent = []
        self.apobj = _FakeApprise()
        self.mqtt_client = None
        self._discovery_sent = set()
        self._last_state = {}

    def _send(self, title, body):
        self.sent.append((title, body))


class _FakeApprise:
    def __len__(self):
        return 1


def test_notifies_about_homework_dated_in_the_past():
    """Regression: a today-only filter dropped the real 09-02 English homework."""
    n = RecordingNotifier()
    n.notify_new_homework("דני", [HomeworkItem(subject="אנגלית", homework="Unit 2", date="2026-09-02")])
    assert len(n.sent) == 1
    assert "אנגלית" in n.sent[0][1]


def test_notifies_about_homework_dated_in_the_future():
    """The endpoint returns tomorrow too; that must not be silently dropped."""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    n = RecordingNotifier()
    n.notify_new_homework("דני", [HomeworkItem(subject="חשבון", homework="עמוד 12", date=tomorrow)])
    assert len(n.sent) == 1
    assert "חשבון" in n.sent[0][1]


def test_today_is_marked_in_the_message():
    today = datetime.now().strftime("%Y-%m-%d")
    n = RecordingNotifier()
    n.notify_new_homework("דני", [HomeworkItem(subject="מדעים", homework="ניסוי", date=today)])
    assert "(today)" in n.sent[0][1]


def test_multiple_items_are_sorted_and_counted_in_the_title():
    n = RecordingNotifier()
    n.notify_new_homework(
        "דני",
        [
            HomeworkItem(subject="ב", homework="x", date="2026-09-04"),
            HomeworkItem(subject="א", homework="y", date="2026-09-02"),
        ],
    )
    title, body = n.sent[0]
    assert "(2 new)" in title
    assert body.index("2026-09-02") < body.index("2026-09-04"), "must read chronologically"


def test_no_items_sends_nothing():
    n = RecordingNotifier()
    n.notify_new_homework("דני", [])
    assert n.sent == []


# ----------------------------------------------------------------------
# MQTT state payload
# ----------------------------------------------------------------------
class CapturingMqtt:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload


def test_state_payload_reports_week_count_not_just_today(tmp_path):
    import json

    n = RecordingNotifier()
    n.mqtt_client = CapturingMqtt()
    # Two assignments this week, neither dated today - the exact live situation.
    items = [
        HomeworkItem(subject="חשבון", homework="a", date="2026-09-01"),
        HomeworkItem(subject="אנגלית", homework="b", date="2026-09-02"),
    ]
    n.publish_state("דני", items)

    topic = next(t for t in n.mqtt_client.published if t.endswith("/state"))
    payload = json.loads(n.mqtt_client.published[topic])
    assert payload["count"] == 0, "nothing due today"
    assert payload["count_week"] == 2, "but two exist in the window"
    assert "This week:" in payload["details"], "details must surface them"
    assert payload["token_status"] == "ok"


def test_state_payload_marks_expired_token():
    import json

    n = RecordingNotifier()
    n.mqtt_client = CapturingMqtt()
    n.publish_state("דני", [], token_status="expired")
    topic = next(t for t in n.mqtt_client.published if t.endswith("/state"))
    assert json.loads(n.mqtt_client.published[topic])["token_status"] == "expired"


# ----------------------------------------------------------------------
# first-run seeding guard
# ----------------------------------------------------------------------
def test_first_run_is_detected_then_not(tmp_path: Path):
    state = HomeworkState(tmp_path / "s.json")
    assert state.is_first_run_for("דני") is True
    state.diff("דני", [HomeworkItem(subject="א", homework="x", date="2026-09-02")])
    assert state.is_first_run_for("דני") is False


def test_first_run_flag_survives_persistence(tmp_path: Path):
    path = tmp_path / "s.json"
    s1 = HomeworkState(path)
    s1.diff("דני", [HomeworkItem(subject="א", homework="x", date="2026-09-02")])
    s1.save()
    assert HomeworkState(path).is_first_run_for("דני") is False
