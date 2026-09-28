"""Options flow, option handling in the coordinator/sensors, and diagnostics."""

import asyncio
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

import voluptuous as vol  # noqa: E402
from homeassistant.config_entries import ConfigEntryState, OptionsFlowWithReload  # noqa: E402

from custom_components.smartschool import coordinator as coord_mod  # noqa: E402
from custom_components.smartschool import diagnostics as diag_mod  # noqa: E402
from custom_components.smartschool import events as events_mod  # noqa: E402
from custom_components.smartschool import sensor as sensor_mod  # noqa: E402
from custom_components.smartschool.api.bio import BioCredentials  # noqa: E402
from custom_components.smartschool.api.models import HomeworkItem, Message, Student  # noqa: E402
from custom_components.smartschool.config_flow import (  # noqa: E402
    OPTIONS_SCHEMA,
    SmartSchoolConfigFlow,
    SmartSchoolOptionsFlow,
)
from custom_components.smartschool.const import EVENT_NEW_MESSAGE  # noqa: E402
from custom_components.smartschool.coordinator import SmartSchoolData  # noqa: E402
from custom_components.smartschool.events import SmartSchoolEvents  # noqa: E402

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "smartschool"


# ---------------------------------------------------------------- options flow
def options_flow(options=None):
    entry = SimpleNamespace(entry_id="entry-a", options=options or {})
    manager = Mock()
    manager.async_get_known_entry.return_value = entry
    flow = SmartSchoolConfigFlow.async_get_options_flow(entry)
    flow.hass = SimpleNamespace(config_entries=manager)
    flow.handler = "entry-a"
    return flow


def suggested(result, field):
    key = next(k for k in result["data_schema"].schema if k == field)
    return (key.description or {}).get("suggested_value")


def test_options_flow_reloads_automatically_without_update_listener():
    assert isinstance(options_flow(), SmartSchoolOptionsFlow)
    assert issubclass(SmartSchoolOptionsFlow, OptionsFlowWithReload)
    # OptionsFlowWithReload must not be combined with an update listener.
    assert "add_update_listener" not in (INTEGRATION / "__init__.py").read_text()


def test_options_form_defaults():
    result = asyncio.run(options_flow().async_step_init())
    assert result["type"] == "form" and result["step_id"] == "init"
    values = OPTIONS_SCHEMA({})
    assert values == {"scan_interval": 30, "messages_enabled": True}


def test_options_form_prefills_current_options():
    result = asyncio.run(options_flow({"scan_interval": 60, "messages_enabled": False}).async_step_init())
    assert suggested(result, "scan_interval") == 60
    assert suggested(result, "messages_enabled") is False


def test_options_saved_as_int_minutes_and_bool():
    result = asyncio.run(options_flow().async_step_init({"scan_interval": 45.0, "messages_enabled": False}))
    assert result["type"] == "create_entry"
    assert result["data"] == {"scan_interval": 45, "messages_enabled": False}
    assert isinstance(result["data"]["scan_interval"], int)


@pytest.mark.parametrize("minutes,ok", [(10, False), (15, True), (360, True), (400, False)])
def test_options_interval_range(minutes, ok):
    if ok:
        assert OPTIONS_SCHEMA({"scan_interval": minutes, "messages_enabled": True})
    else:
        with pytest.raises(vol.Invalid):
            OPTIONS_SCHEMA({"scan_interval": minutes, "messages_enabled": True})


def test_options_translations_cover_fields():
    for name in ("strings.json", "translations/en.json"):
        step = json.loads((INTEGRATION / name).read_text())["options"]["step"]["init"]
        assert set(step["data"]) == {"scan_interval", "messages_enabled"}


# ---------------------------------------------------------------- coordinator honours options
def real_coordinator(tmp_path, options):
    """A coordinator built through its real __init__ on a real HomeAssistant."""
    from homeassistant.core import HomeAssistant

    async def build():
        hass = HomeAssistant(str(tmp_path))
        entry = SimpleNamespace(
            entry_id="entry-a",
            data={"bio_login": "B", "unique_id": "u"},
            options=options,
            async_on_unload=lambda func: None,
        )
        try:
            coordinator = coord_mod.SmartSchoolCoordinator(hass, entry)
            return coordinator.update_interval, coordinator._messages_enabled
        finally:
            await hass.async_stop(force=True)

    return asyncio.run(build())


def test_coordinator_uses_default_interval_and_inbox(tmp_path):
    assert real_coordinator(tmp_path, {}) == (timedelta(minutes=30), True)


def test_coordinator_uses_configured_interval_and_inbox(tmp_path):
    options = {"scan_interval": 90, "messages_enabled": False}
    assert real_coordinator(tmp_path, options) == (timedelta(minutes=90), False)


class InboxClient:
    """Minimal client: one student, empty dashboard homework, inbox tracked."""

    def __init__(self):
        self.inbox_calls = 0

    def check_token(self):
        return True

    def get_students(self):
        return [Student(student_id="s1", name="kid", class_code=4, class_number=1)]

    def get_homework_pupilcard(self, params):
        return {"status": True, "data": []}

    def get_homework(self, student):
        return {"status": True, "data": {"dataTable": []}}

    def get_messages_inbox(self):
        self.inbox_calls += 1
        return [Message("hi")]

    def close(self):
        pass


def bare_coordinator(messages_enabled):
    c = coord_mod.SmartSchoolCoordinator.__new__(coord_mod.SmartSchoolCoordinator)
    c._creds = BioCredentials(bio_login="B", unique_id="u", is_mobile=True)
    c._client = InboxClient()
    c._messages_enabled = messages_enabled
    c.data = None
    return c


def test_inbox_disabled_skips_the_request_and_marks_data():
    c = bare_coordinator(messages_enabled=False)
    data = c._fetch()
    assert c._client.inbox_calls == 0
    assert data.messages == [] and data.messages_fresh is False and data.messages_enabled is False


def test_inbox_enabled_is_polled():
    c = bare_coordinator(messages_enabled=True)
    data = c._fetch()
    assert c._client.inbox_calls == 1 and data.messages_enabled is True and len(data.messages) == 1


# ---------------------------------------------------------------- sensors / events
class StubCoordinator:
    last_update_success = True

    def __init__(self, data):
        self.data = data

    def async_add_listener(self, *args, **kwargs):
        return lambda: None


def test_message_sensors_unavailable_when_inbox_disabled():
    desc = next(d for d in sensor_mod.MESSAGE_SENSORS if d.key == "unread")
    off = SmartSchoolData([], {}, [], messages_fresh=False, messages_enabled=False)
    on = SmartSchoolData([], {}, [Message("x")])
    assert sensor_mod.MessageSensor(StubCoordinator(off), "entry-a", desc).available is False
    assert sensor_mod.MessageSensor(StubCoordinator(on), "entry-a", desc).available is True


def test_homework_sensors_unaffected_by_inbox_option():
    stu = Student(student_id="s1", name="kid")
    desc = next(d for d in sensor_mod.HOMEWORK_SENSORS if d.key == "count")
    off = SmartSchoolData([stu], {"s1": []}, [], messages_fresh=False, messages_enabled=False)
    assert sensor_mod.HomeworkSensor(StubCoordinator(off), "entry-a", stu, desc).available is True


def test_turning_inbox_off_and_on_keeps_the_baseline(monkeypatch, make_store):
    monkeypatch.setattr(events_mod.dr, "async_get", lambda hass: Mock(
        async_get_device=Mock(return_value=None)))
    hass = SimpleNamespace(bus=Mock())
    tracker = SmartSchoolEvents(hass, "entry-a", store=make_store())
    old, new = Message("old notice"), Message("new notice")
    tracker.async_process(SmartSchoolData([], {}, [old]))  # baseline
    tracker.async_process(SmartSchoolData([], {}, [], messages_fresh=False, messages_enabled=False))
    tracker.async_process(SmartSchoolData([], {}, [old, new]))  # switched back on
    fired = [c.args for c in hass.bus.async_fire.call_args_list]
    assert [(t, d["item_id"]) for t, d in fired] == [(EVENT_NEW_MESSAGE, new.identity())]


# ---------------------------------------------------------------- diagnostics
SECRETS = {
    "bio_login": "SECRET-BIO",
    "unique_id": "SECRET-UNIQUE",
    "selected_user": "SECRET-USER",
    "device_id": "SECRET-DEVICE",
    "is_mobile": True,
}


def diag_entry(**extra):
    return SimpleNamespace(
        entry_id="entry-a", version=1, minor_version=1, state=ConfigEntryState.LOADED,
        unique_id="SECRET-UNIQUE", data=dict(SECRETS), options={"scan_interval": 45},
        **extra,
    )


class DiagCoordinator:
    def __init__(self, data, exception=None):
        self.data = data
        self.last_update_success = exception is None
        self.last_exception = exception
        self.update_interval = timedelta(minutes=45)

    def history_stats(self):
        return {"students": 1, "homework_identities": 3, "inbox_baseline": True,
                "message_identities": 2, "closed": False}


def diag(coordinator):
    hass = SimpleNamespace(data={"smartschool": {"entry-a": coordinator}} if coordinator else {})
    return asyncio.run(diag_mod.async_get_config_entry_diagnostics(hass, diag_entry()))


def test_diagnostics_redacts_credentials_ids_names_and_content():
    student = Student(student_id="SECRET-STUDENT-ID", name="SECRET-CHILD-NAME")
    data = SmartSchoolData(
        [student],
        {"SECRET-STUDENT-ID": [HomeworkItem("SECRET-SUBJECT", "SECRET-HOMEWORK", teacher="SECRET-TEACHER")]},
        [Message("SECRET-MESSAGE-SUBJECT", sender="SECRET-SENDER", has_read=False)],
        {"SECRET-STUDENT-ID": True},
    )
    result = diag(DiagCoordinator(data, exception=RuntimeError("SECRET-EXCEPTION-TEXT")))
    dumped = json.dumps(result)
    assert "SECRET" not in dumped, [s for s in dumped.split('"') if "SECRET" in s]
    assert result["entry"]["data"]["bio_login"] == "**REDACTED**"
    assert result["entry"]["unique_id"] == "**REDACTED**"
    assert result["entry"]["data"]["is_mobile"] is True, "non-sensitive settings stay visible"
    assert result["entry"]["options"] == {"scan_interval": 45}


def test_diagnostics_reports_useful_counts():
    s1, s2 = Student(student_id="a", name="x"), Student(student_id="b", name="y")
    data = SmartSchoolData(
        [s1, s2],
        {"a": [HomeworkItem("m", "h"), HomeworkItem("m", "h2")], "b": []},
        [Message("1", has_read=True), Message("2", has_read=False)],
        {"a": True, "b": False},
    )
    info = diag(DiagCoordinator(data))["coordinator"]
    assert info["last_update_success"] is True and info["last_exception"] is None
    assert info["update_interval_minutes"] == 45
    assert info["students"] == [
        {"student": "student_1", "homework_items": 2, "full_window": True},
        {"student": "student_2", "homework_items": 0, "full_window": False},
    ]
    assert info["inbox"] == {"enabled": True, "fresh": True, "messages": 2, "unread": 1}
    assert info["history"]["homework_identities"] == 3


def test_diagnostics_reports_exception_type_only():
    info = diag(DiagCoordinator(None, exception=RuntimeError("private")))["coordinator"]
    assert info["last_exception"] == "RuntimeError"
    assert info["students"] == [] and info["inbox"] is None


def test_diagnostics_when_not_loaded():
    result = diag(None)
    assert result["coordinator"] is None
    assert result["entry"]["data"]["unique_id"] == "**REDACTED**"


def test_history_stats_are_counts_only(make_store, monkeypatch):
    monkeypatch.setattr(events_mod.dr, "async_get", lambda hass: Mock(
        async_get_device=Mock(return_value=None)))
    tracker = SmartSchoolEvents(SimpleNamespace(bus=Mock()), "entry-a", store=make_store())
    tracker.async_process(SmartSchoolData(
        [Student(student_id="SECRET-ID", name="n")], {"SECRET-ID": [HomeworkItem("s", "h")]},
        [Message("m")], {"SECRET-ID": True},
    ))
    stats = tracker.stats()
    assert stats == {"students": 1, "homework_identities": 1, "inbox_baseline": True,
                     "message_identities": 1, "closed": False}
    assert "SECRET" not in json.dumps(stats)
