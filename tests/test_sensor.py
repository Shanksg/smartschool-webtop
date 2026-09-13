"""Tests for the SmartSchool sensor platform.

Requires Home Assistant importable (skipped otherwise). Entities are exercised
directly with a stub coordinator - no running HA instance - which is enough to
cover value/attribute logic, device grouping, unique ids and availability.
"""

import datetime
import os
import sys

import pytest

pytest.importorskip("homeassistant")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.smartschool import sensor as sensor_mod  # noqa: E402
from custom_components.smartschool.api.models import (  # noqa: E402
    HomeworkItem,
    Message,
    Student,
)
from custom_components.smartschool.coordinator import SmartSchoolData  # noqa: E402

# Match the sensor: it classifies dates against HA's configured timezone.
TODAY = dt_util.now().strftime("%Y-%m-%d")
ENTRY_ID = "entry123"


class StubCoordinator:
    """Minimal stand-in for the DataUpdateCoordinator."""

    last_update_success = True

    def __init__(self, data):
        self.data = data

    def async_add_listener(self, *args, **kwargs):
        return lambda: None


def _data(homework=None, messages=None, students=None):
    stu = Student(student_id="s1", name="דני", class_code=4)
    return StubCoordinator(
        SmartSchoolData(
            students=students if students is not None else [stu],
            homework=homework if homework is not None else {"s1": []},
            messages=messages if messages is not None else [],
        )
    ), stu


def _hw(coord, student, key):
    desc = next(d for d in sensor_mod.HOMEWORK_SENSORS if d.key == key)
    return sensor_mod.HomeworkSensor(coord, ENTRY_ID, student, desc)


def _msg(coord, key):
    desc = next(d for d in sensor_mod.MESSAGE_SENSORS if d.key == key)
    return sensor_mod.MessageSensor(coord, ENTRY_ID, desc)


# ---------------------------------------------------------------- homework
def test_homework_counts():
    items = [
        HomeworkItem(subject="מתמטיקה", homework="a", date=TODAY, teacher="T"),
        HomeworkItem(subject="אנגלית", homework="b", date="2026-09-01", teacher="Y"),
    ]
    coord, stu = _data(homework={"s1": items})
    assert _hw(coord, stu, "count").native_value == 1        # today only
    assert _hw(coord, stu, "count_week").native_value == 2   # all in window
    assert _hw(coord, stu, "count_upcoming").native_value == 1  # today+future


def test_homework_details_state_short_full_text_in_attr():
    items = [HomeworkItem(subject=f"נושא{i}", homework="x" * 300, date=TODAY) for i in range(10)]
    coord, stu = _data(homework={"s1": items})
    e = _hw(coord, stu, "details")
    assert len(str(e.native_value)) < 255, "details STATE must be short"
    assert "נושא0" in e.extra_state_attributes["text"], "full list in the attribute"


def test_homework_details_empty():
    coord, stu = _data(homework={"s1": []})
    e = _hw(coord, stu, "details")
    assert e.native_value == "0 today / 0 this week"
    assert e.extra_state_attributes["text"] == "No homework"


def test_homework_device_and_unique_id_scoped_by_entry():
    coord, stu = _data()
    e = _hw(coord, stu, "count")
    assert e._attr_unique_id == f"{ENTRY_ID}_student_s1_count"
    assert e._attr_device_info["name"] == "SmartSchool - דני"
    assert e._attr_device_info["identifiers"] == {
        ("smartschool", f"{ENTRY_ID}_student_s1")
    }


def test_homework_ids_differ_across_entries_same_student():
    coord, stu = _data()
    desc = next(d for d in sensor_mod.HOMEWORK_SENSORS if d.key == "count")
    a = sensor_mod.HomeworkSensor(coord, "acctA", stu, desc)
    b = sensor_mod.HomeworkSensor(coord, "acctB", stu, desc)
    assert a._attr_unique_id != b._attr_unique_id
    assert a._attr_device_info["identifiers"] != b._attr_device_info["identifiers"]


def test_homework_unavailable_when_student_dropped():
    coord, stu = _data(homework={})  # student not in the homework dict
    e = _hw(coord, stu, "count")
    assert e.available is False


def test_homework_available_when_present():
    coord, stu = _data(homework={"s1": []})
    assert _hw(coord, stu, "count").available is True


# ---------------------------------------------------------------- messages
def test_message_counts_and_latest():
    msgs = [
        Message(subject="old", sender="A", sent_at="2026-09-01T09:00:00", has_read=True),
        Message(subject="new", sender="B", sent_at="2026-09-10T09:00:00", has_read=False),
    ]
    coord, _ = _data(messages=msgs)
    assert _msg(coord, "unread").native_value == 1
    assert _msg(coord, "total").native_value == 2
    assert _msg(coord, "latest").native_value == "new"  # newest by sent_at


def test_message_details_marks_unread():
    msgs = [Message(subject="hi", sender="X", sent_at="2026-09-10T08:00:00", has_read=False)]
    coord, _ = _data(messages=msgs)
    e = _msg(coord, "details")
    assert e.native_value == "1 unread / 1 total"
    assert e.extra_state_attributes["text"].startswith("* hi")  # * marks unread


def test_message_empty():
    coord, _ = _data(messages=[])
    assert _msg(coord, "total").native_value == 0
    assert _msg(coord, "latest").native_value == "No messages"
    assert _msg(coord, "details").extra_state_attributes["text"] == "No messages"


def test_message_device_and_unique_id_scoped_by_entry():
    coord, _ = _data()
    e = _msg(coord, "unread")
    assert e._attr_unique_id == f"{ENTRY_ID}_inbox_unread"
    assert e._attr_device_info["identifiers"] == {("smartschool", f"{ENTRY_ID}_inbox")}
    assert e._attr_device_info["name"] == "SmartSchool - Messages"


def test_inbox_ids_differ_across_entries():
    coord, _ = _data()
    desc = next(d for d in sensor_mod.MESSAGE_SENSORS if d.key == "unread")
    a = sensor_mod.MessageSensor(coord, "acctA", desc)
    b = sensor_mod.MessageSensor(coord, "acctB", desc)
    assert a._attr_unique_id != b._attr_unique_id
    assert a._attr_device_info["identifiers"] != b._attr_device_info["identifiers"]


def test_latest_subject_clipped_to_ha_limit():
    long = "כ" * 400
    coord, _ = _data(messages=[Message(subject=long, sender="X", sent_at="2026-09-10T08:00:00")])
    assert len(str(_msg(coord, "latest").native_value)) <= 250


# ---------------------------------------------------------------- dynamic roster
def test_setup_adds_sensors_for_students_linked_after_setup():
    import asyncio

    stu1 = Student(student_id="s1", name="A", class_code=4)
    coord = StubCoordinator(
        SmartSchoolData(students=[stu1], homework={"s1": []}, messages=[])
    )
    listeners = []
    coord.async_add_listener = lambda cb, *a, **k: (
        listeners.append(cb) or (lambda: None)
    )

    added = []

    def add_entities(new, *a, **k):
        added.extend(list(new))

    class FakeEntry:
        entry_id = ENTRY_ID

        def async_on_unload(self, unsub):
            pass

    class FakeHass:
        data = {sensor_mod.DOMAIN: {ENTRY_ID: coord}}

    asyncio.run(sensor_mod.async_setup_entry(FakeHass(), FakeEntry(), add_entities))
    hw = [e for e in added if isinstance(e, sensor_mod.HomeworkSensor)]
    assert {e._student_id for e in hw} == {"s1"}, "initial student gets sensors"
    assert listeners, "a coordinator listener must be registered for roster changes"

    # A second student appears on a later coordinator update.
    stu2 = Student(student_id="s2", name="B", class_code=5)
    coord.data = SmartSchoolData(
        students=[stu1, stu2], homework={"s1": [], "s2": []}, messages=[]
    )
    for cb in listeners:
        cb()

    hw = [e for e in added if isinstance(e, sensor_mod.HomeworkSensor)]
    assert {e._student_id for e in hw} == {"s1", "s2"}, "new student gets sensors live"
    # s1 must not be re-added.
    assert sum(1 for e in hw if e._student_id == "s1") == len(sensor_mod.HOMEWORK_SENSORS)


# ---------------------------------------------------------------- coverage of set
def test_expected_sensor_keys():
    assert {d.key for d in sensor_mod.HOMEWORK_SENSORS} == {
        "count", "count_week", "count_upcoming", "details"
    }
    assert {d.key for d in sensor_mod.MESSAGE_SENSORS} == {
        "unread", "total", "latest", "details"
    }
