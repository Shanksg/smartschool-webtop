"""Tests for token expiry prediction and the proactive warning.

The measured fact behind all of this: the webToken cookie carries a 9-hour
lifetime (observed 2026-09-03, issued 15:46:06 / Expires 00:46:06). No API
response ever sends Set-Cookie, so expiry has to be inferred.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartschool.models import TokenState
from smartschool.notifiers import Notifier


def _aged(hours):
    return TokenState(
        token="t",
        obtained_at=(datetime.now().astimezone() - timedelta(hours=hours)).isoformat(),
    )


# ----------------------------------------------------------------------
# prediction
# ----------------------------------------------------------------------
def test_fresh_token_has_a_full_ttl_remaining():
    assert 535 < _aged(0).minutes_remaining(9) <= 540


def test_remaining_shrinks_with_age():
    assert 55 < _aged(8).minutes_remaining(9) < 65


def test_remaining_goes_negative_past_expiry():
    assert _aged(10).minutes_remaining(9) < 0


def test_unknown_obtained_at_yields_none():
    assert TokenState(token="t").minutes_remaining(9) is None
    assert TokenState(token="t").predicted_expiry(9) is None


def test_unparseable_obtained_at_yields_none():
    assert TokenState(token="t", obtained_at="not a date").minutes_remaining(9) is None


def test_explicit_expires_at_wins_over_the_ttl_guess():
    """A real Set-Cookie expiry, if we ever get one, must take precedence."""
    exact = datetime.now(timezone.utc) + timedelta(hours=2)
    st = TokenState(
        token="t",
        obtained_at=datetime.now().astimezone().isoformat(),   # would imply 9h
        expires_at=exact.isoformat(),
    )
    left = st.minutes_remaining(9)
    assert 110 < left < 125, "must use expires_at (2h), not the 9h default"


def test_http_date_expires_at_is_parsed():
    st = TokenState(token="t", expires_at="Fri, 04 Sep 2026 00:46:06 GMT")
    assert st.predicted_expiry(9) is not None


def test_ttl_is_configurable():
    st = _aged(0)
    assert st.minutes_remaining(1) <= 60
    assert st.minutes_remaining(24) > 1400


# ----------------------------------------------------------------------
# warning notification
# ----------------------------------------------------------------------
class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent = []
        self.apobj = type("A", (), {"__len__": lambda s: 1})()
        self.mqtt_client = None
        self._discovery_sent = set()
        self._last_state = {}

    def _send(self, title, body):
        self.sent.append((title, body))
        return True


def test_expiring_notification_states_the_time_and_the_fix():
    n = RecordingNotifier()
    n.notify_token_expiring("config/token.txt", 42)
    title, body = n.sent[0]
    assert "expiring soon" in title
    assert "42 minutes" in body
    assert "config/token.txt" in body
    assert "no restart needed" in body


# ----------------------------------------------------------------------
# monitor behaviour
# ----------------------------------------------------------------------
def _monitor(tmp_path, monkeypatch, hours_old, warn_minutes="45"):
    from smartschool.config import Config, Paths
    from smartschool.monitor import Monitor

    monkeypatch.setenv("NOTIFIERS", "")
    monkeypatch.setenv("MQTT_BROKER", "")
    monkeypatch.setenv("TOKEN_WARN_MINUTES", warn_minutes)

    paths = Paths(root=tmp_path)
    paths.ensure()
    (paths.config_dir / "token.txt").write_text("tok", encoding="utf-8")

    m = Monitor(Config(paths))
    assert m.connect()
    # age the token as if it were pasted `hours_old` ago
    m.token_state.obtained_at = (
        datetime.now().astimezone() - timedelta(hours=hours_old)
    ).isoformat()

    warned = []
    monkeypatch.setattr(
        m.notifier, "notify_token_expiring", lambda f, mins: warned.append(mins)
    )
    return m, warned


def test_no_warning_while_the_token_is_young(tmp_path, monkeypatch):
    m, warned = _monitor(tmp_path, monkeypatch, hours_old=1)
    m.warn_if_expiring()
    assert warned == []


def test_warns_once_inside_the_window(tmp_path, monkeypatch):
    m, warned = _monitor(tmp_path, monkeypatch, hours_old=8.5)   # ~30 min left
    m.warn_if_expiring()
    m.warn_if_expiring()
    m.warn_if_expiring()
    assert len(warned) == 1, "must warn once per token, not every poll"
    assert 0 < warned[0] <= 45


def test_no_warning_once_already_past_expiry(tmp_path, monkeypatch):
    """Past expiry the 401 path owns it; a 'expiring soon' note would be wrong."""
    m, warned = _monitor(tmp_path, monkeypatch, hours_old=12)
    m.warn_if_expiring()
    assert warned == []


def test_warning_window_is_configurable(tmp_path, monkeypatch):
    m, warned = _monitor(tmp_path, monkeypatch, hours_old=7, warn_minutes="180")
    m.warn_if_expiring()
    assert len(warned) == 1, "a 3h window must fire with ~2h left"


def test_new_token_rearms_the_warning(tmp_path, monkeypatch):
    m, warned = _monitor(tmp_path, monkeypatch, hours_old=8.5)
    m.warn_if_expiring()
    assert len(warned) == 1

    # user pastes a replacement -> connect() re-reads and must re-arm
    (m.config.paths.config_dir / "token.txt").write_text("tok2", encoding="utf-8")
    assert m.connect()
    assert m._expiring_notified is False, "a fresh token must re-arm the warning"


def test_hours_left_reaches_the_mqtt_payload(tmp_path, monkeypatch):
    import json

    m, _ = _monitor(tmp_path, monkeypatch, hours_old=1)

    class Cap:
        def __init__(self):
            self.p = {}

        def publish(self, t, payload, retain=False):
            self.p[t] = payload

    m.notifier.mqtt_client = Cap()
    m.notifier.publish_state(
        "kid", [], token_status="ok", token_minutes_left=m.token_minutes_left()
    )
    topic = next(t for t in m.notifier.mqtt_client.p if t.endswith("/state"))
    payload = json.loads(m.notifier.mqtt_client.p[topic])
    assert 7.5 < payload["token_hours_left"] < 8.5


def test_hours_left_is_null_when_unknown():
    import json

    class Cap:
        def __init__(self):
            self.p = {}

        def publish(self, t, payload, retain=False):
            self.p[t] = payload

    n = RecordingNotifier()
    n.mqtt_client = Cap()
    n.publish_state("kid", [], token_minutes_left=None)
    payload = json.loads(n.mqtt_client.p["smartschool/student_" + __import__("hashlib").md5(b"kid").hexdigest()[:8] + "/state"])
    assert payload["token_hours_left"] is None


# ----------------------------------------------------------------------
# optional expiry line in token.txt (the remember-me / 6-month case)
# ----------------------------------------------------------------------
def test_token_file_expiry_line_is_read(tmp_path):
    from smartschool.session import TokenStore

    (tmp_path / "token.txt").write_text(
        "THE-TOKEN\n2027-03-02T14:03:26.466Z\n", encoding="utf-8"
    )
    st = TokenStore(tmp_path).load()
    assert st.token == "THE-TOKEN"
    assert st.expires_at == "2027-03-02T14:03:26.466+00:00"
    # ~6 months out, so the 9h TTL guess is overridden
    assert st.minutes_remaining(9) > 100_000


def test_token_file_without_expiry_still_works(tmp_path):
    from smartschool.session import TokenStore

    (tmp_path / "token.txt").write_text("JUST-A-TOKEN\n", encoding="utf-8")
    st = TokenStore(tmp_path).load()
    assert st.token == "JUST-A-TOKEN"
    assert st.expires_at is None
    # falls back to the TTL guess
    assert 530 < st.minutes_remaining(9) <= 540


def test_bad_expiry_line_is_ignored_not_fatal(tmp_path):
    from smartschool.session import TokenStore

    (tmp_path / "token.txt").write_text("TOKEN\ngarbage-not-a-date\n", encoding="utf-8")
    st = TokenStore(tmp_path).load()
    assert st.token == "TOKEN"
    assert st.expires_at is None


def test_expiry_survives_a_reload(tmp_path):
    from smartschool.session import TokenStore

    (tmp_path / "token.txt").write_text("T\n2027-03-02T14:03:26.466Z\n", encoding="utf-8")
    store = TokenStore(tmp_path)
    store.load()
    # second load reads from cache; expiry must persist
    assert store.load().expires_at == "2027-03-02T14:03:26.466+00:00"
