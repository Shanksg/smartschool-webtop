"""Stable student keys across an encrypted-id change (school-year rollover)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.smartschool import coordinator as coord_mod  # noqa: E402
from custom_components.smartschool import events as events_mod  # noqa: E402
from custom_components.smartschool import sensor as sensor_mod  # noqa: E402
from custom_components.smartschool import students as students_mod  # noqa: E402
from custom_components.smartschool.api.models import HomeworkItem, Student  # noqa: E402
from custom_components.smartschool.const import EVENT_NEW_HOMEWORK  # noqa: E402
from custom_components.smartschool.coordinator import SmartSchoolData  # noqa: E402
from custom_components.smartschool.events import SmartSchoolEvents  # noqa: E402
from custom_components.smartschool.students import assign_keys, known_students  # noqa: E402


# ---------------------------------------------------------------- assign_keys (pure)
def test_known_ids_keep_their_key():
    assert assign_keys([("A", "Dana"), ("B", "Noam")], {"A": "dana", "B": "noam"}) == {"A": "A", "B": "B"}


def test_first_sight_key_is_the_id_so_existing_installs_are_unchanged():
    assert assign_keys([("A", "Dana")], {}) == {"A": "A"}


def test_rotated_id_inherits_the_key_by_name():
    assert assign_keys([("A2", "  DANA ")], {"A": "dana"}) == {"A2": "A"}


def test_rotation_alongside_an_unchanged_sibling_and_a_new_child():
    mapping = assign_keys(
        [("A2", "Dana"), ("B", "Noam"), ("C", "Yael")],
        {"A": "dana", "B": "noam"},
    )
    assert mapping == {"A2": "A", "B": "B", "C": "C"}


def test_a_key_still_in_use_is_not_reassigned():
    # "Dana" is still A in this snapshot, so the extra same-name id is new.
    assert assign_keys([("A", "Dana"), ("X", "Dana")], {"A": "dana"}) == {"A": "A", "X": "X"}


def test_ambiguous_known_names_become_a_new_student():
    assert assign_keys([("Z", "Dana")], {"A": "dana", "B": "dana"}) == {"Z": "Z"}


def test_two_current_students_with_one_name_do_not_merge():
    assert assign_keys([("X", "Dana"), ("Y", "Dana")], {"A": "dana"}) == {"X": "X", "Y": "Y"}


def test_nameless_students_never_match_by_name():
    assert assign_keys([("A2", "")], {"A": ""}) == {"A2": "A2"}


def test_different_name_is_a_new_student():
    assert assign_keys([("A2", "Dana")], {"A": "noam"}) == {"A2": "A2"}


# ---------------------------------------------------------------- known_students (registry)
def registry(monkeypatch, devices_by_entry):
    monkeypatch.setattr(students_mod.dr, "async_get", lambda hass: "registry")
    monkeypatch.setattr(students_mod.dr, "async_entries_for_config_entry",
                        lambda reg, entry_id: devices_by_entry.get(entry_id, []))


def device(identifier, name, domain="smartschool"):
    return SimpleNamespace(identifiers={(domain, identifier)}, name=name)


def test_known_students_read_this_entrys_student_devices(monkeypatch):
    registry(monkeypatch, {"entry-a": [
        device("entry-a_student_A", "SmartSchool - Dana"),
        device("entry-a_student_B", "SmartSchool -   Noam  "),
        device("entry-a_student_C", "SmartSchool -"),          # nameless student
        device("entry-a_inbox", "SmartSchool - Messages"),
        device("entry-a_student_Z", "SmartSchool - Zed", domain="other"),
    ]})
    assert known_students(object(), "entry-a") == {"A": "dana", "B": "noam", "C": ""}
    assert known_students(object(), "entry-b") == {}


def test_known_student_format_matches_sensor_devices():
    stub = SimpleNamespace(data=None, last_update_success=True,
                           async_add_listener=lambda *a, **k: (lambda: None))
    entity = sensor_mod.HomeworkSensor(stub, "entry-a", Student(student_id="A", name="Dana"),
                                       sensor_mod.HOMEWORK_SENSORS[0])
    (domain, identifier), = entity._attr_device_info["identifiers"]
    assert (domain, identifier) == ("smartschool", students_mod.device_identifier("entry-a", "A"))
    assert entity._attr_device_info["name"] == students_mod.DEVICE_NAME_PREFIX + "Dana"


# ---------------------------------------------------------------- coordinator re-keying
def rekeying_coordinator(monkeypatch, registered=None):
    c = coord_mod.SmartSchoolCoordinator.__new__(coord_mod.SmartSchoolCoordinator)
    c.hass = object()
    c.entry = SimpleNamespace(entry_id="entry-a")
    monkeypatch.setattr(coord_mod, "known_students", lambda hass, entry_id: dict(registered or {}))
    return c


def snapshot(sid, name="Dana", homework=(), full=True):
    return SmartSchoolData([Student(student_id=sid, name=name)], {sid: list(homework)}, [], {sid: full})


def test_rotation_within_a_run_keeps_the_key(monkeypatch):
    c = rekeying_coordinator(monkeypatch)
    first = snapshot("A")
    c._async_assign_student_keys(first)
    rotated = snapshot("A2", homework=[HomeworkItem("m", "h")], full=False)
    c._async_assign_student_keys(rotated)
    student = rotated.students[0]
    assert rotated.key_of(student) == "A"
    assert list(rotated.homework) == ["A"] and rotated.full_window == {"A": False}
    assert student.student_id == "A2", "API requests keep using the current id"


def test_rotation_across_a_restart_uses_the_registry(monkeypatch):
    c = rekeying_coordinator(monkeypatch, registered={"A": "dana"})
    data = snapshot("A2")
    c._async_assign_student_keys(data)
    assert data.key_of(data.students[0]) == "A"


def test_without_rekeying_the_key_is_the_id():
    data = snapshot("A")
    assert data.key_of(data.students[0]) == "A"


# ---------------------------------------------------------------- sensors keep their entities
def test_no_new_entities_after_rotation(monkeypatch):
    c = rekeying_coordinator(monkeypatch)
    c.last_update_success = True
    c.data = snapshot("A")
    c._async_assign_student_keys(c.data)
    listeners, added = [], []
    c.async_add_listener = lambda cb, *a, **k: (listeners.append(cb) or (lambda: None))

    class Entry:
        entry_id = "entry-a"

        def async_on_unload(self, unsub):
            pass

    hass = SimpleNamespace(data={"smartschool": {"entry-a": c}})
    asyncio.run(sensor_mod.async_setup_entry(hass, Entry(), lambda new, *a, **k: added.extend(new)))
    homework_entities = [e for e in added if isinstance(e, sensor_mod.HomeworkSensor)]
    ids_before = {e._attr_unique_id for e in homework_entities}

    c.data = snapshot("A2", homework=[HomeworkItem("m", "h")])
    c._async_assign_student_keys(c.data)
    for cb in listeners:
        cb()
    homework_entities = [e for e in added if isinstance(e, sensor_mod.HomeworkSensor)]
    assert {e._attr_unique_id for e in homework_entities} == ids_before
    assert ids_before == {f"entry-a_student_A_{d.key}" for d in sensor_mod.HOMEWORK_SENSORS}
    # ...and the existing entities show the rotated student's homework.
    count = next(e for e in homework_entities if e.entity_description.key == "count_week")
    count.coordinator = c
    assert count.native_value == 1 and count.available is True


# ---------------------------------------------------------------- history carries over
def test_history_survives_rotation_no_silent_reseed(monkeypatch, make_store):
    monkeypatch.setattr(events_mod.dr, "async_get", lambda hass: Mock(
        async_get_device=Mock(return_value=None)))
    hass = SimpleNamespace(bus=Mock())
    tracker = SmartSchoolEvents(hass, "entry-a", store=make_store())
    c = rekeying_coordinator(monkeypatch)
    old, new = HomeworkItem("Math", "old", "2026-09-01"), HomeworkItem("Math", "new", "2026-09-02")

    before = snapshot("A", homework=[old])
    c._async_assign_student_keys(before)
    tracker.async_process(before)                       # baseline
    after = snapshot("A2", homework=[old, new])          # id rotated, one new item
    c._async_assign_student_keys(after)
    tracker.async_process(after)
    events = [call.args[1] for call in hass.bus.async_fire.call_args_list]
    assert [(e["student_id"], e["item_id"]) for e in events] == [("A", new.identity())]
    assert hass.bus.async_fire.call_args.args[0] == EVENT_NEW_HOMEWORK


def test_diagnostics_counts_follow_the_key(monkeypatch):
    from custom_components.smartschool import diagnostics as diag_mod
    from datetime import timedelta
    from homeassistant.config_entries import ConfigEntryState

    c = rekeying_coordinator(monkeypatch, registered={"A": "dana"})
    c.data = snapshot("A2", homework=[HomeworkItem("m", "h")])
    c._async_assign_student_keys(c.data)
    c.last_update_success, c.last_exception = True, None
    c.update_interval = timedelta(minutes=30)
    c.history_stats = lambda: {}
    entry = SimpleNamespace(entry_id="entry-a", version=1, minor_version=1,
                            state=ConfigEntryState.LOADED, unique_id=None, data={}, options={})
    hass = SimpleNamespace(data={"smartschool": {"entry-a": c}})
    info = asyncio.run(diag_mod.async_get_config_entry_diagnostics(hass, entry))["coordinator"]
    assert info["students"] == [{"student": "student_1", "homework_items": 1, "full_window": True}]


def test_update_path_rekeys_before_publishing(monkeypatch, make_store):
    """The real _async_update_data re-keys the fetched snapshot."""
    from unittest.mock import AsyncMock

    c = rekeying_coordinator(monkeypatch, registered={"A": "dana"})
    rotated = snapshot("A2", homework=[HomeworkItem("m", "h")])
    c.hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=rotated))
    seen = []
    c._events = SimpleNamespace(async_process=lambda data: seen.append(dict(data.homework)))
    result = asyncio.run(c._async_update_data())
    assert list(result.homework) == ["A"]
    assert seen == [{"A": result.homework["A"]}], "events already see stable keys"


def test_entities_created_after_a_restart_past_rotation_keep_their_ids(monkeypatch):
    """HA restarts after the id changed: entities are rebuilt from the new id."""
    c = rekeying_coordinator(monkeypatch, registered={"A": "dana"})
    c.last_update_success = True
    c.data = snapshot("A2", homework=[HomeworkItem("m", "h")])
    c._async_assign_student_keys(c.data)
    added = []
    c.async_add_listener = lambda cb, *a, **k: (lambda: None)

    class Entry:
        entry_id = "entry-a"

        def async_on_unload(self, unsub):
            pass

    hass = SimpleNamespace(data={"smartschool": {"entry-a": c}})
    asyncio.run(sensor_mod.async_setup_entry(hass, Entry(), lambda new, *a, **k: added.extend(new)))
    homework_entities = [e for e in added if isinstance(e, sensor_mod.HomeworkSensor)]
    assert {e._attr_unique_id for e in homework_entities} == {
        f"entry-a_student_A_{d.key}" for d in sensor_mod.HOMEWORK_SENSORS
    }
    assert {e._attr_device_info["identifiers"].pop()[1] for e in homework_entities} == {
        "entry-a_student_A"
    }
    count = next(e for e in homework_entities if e.entity_description.key == "count_week")
    assert count.native_value == 1
