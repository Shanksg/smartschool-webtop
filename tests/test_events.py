"""Event behavior with synthetic data and no network access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.smartschool.api.models import HomeworkItem, Message, Student
from custom_components.smartschool.const import EVENT_NEW_HOMEWORK, EVENT_NEW_MESSAGE
from custom_components.smartschool.coordinator import SmartSchoolCoordinator, SmartSchoolData
from custom_components.smartschool.events import SmartSchoolEvents


def snapshot(homework=(), messages=(), *, full=True, fresh=True, student="student-a"):
    return SmartSchoolData(
        [Student(student, "Example student")], {student: list(homework)},
        list(messages), {student: full}, messages_fresh=fresh,
    )


@pytest.fixture
def tracker(monkeypatch):
    registry = Mock()
    registry.async_get_device.return_value = SimpleNamespace(id="device-a")
    monkeypatch.setattr(
        "custom_components.smartschool.events.dr.async_get", lambda hass: registry
    )
    return SmartSchoolEvents(SimpleNamespace(bus=Mock()), "entry-a")


def test_initial_load_silent_then_new_items_have_scoped_payload(tracker):
    old = HomeworkItem("Math", "Exercise 1", "2026-09-15")
    new = HomeworkItem("Math", "Exercise 2", "2026-09-15")
    message = Message("New notice", sender="Example sender", sent_at="2026-09-15T12:00:00",
                      raw={"credential": "never include raw data"})
    tracker.async_process(snapshot([old]))
    tracker.hass.bus.async_fire.assert_not_called()
    tracker.async_process(snapshot([old, new, new], [message, message]))
    calls = tracker.hass.bus.async_fire.call_args_list
    assert len(calls) == 2
    assert calls[0].args == (EVENT_NEW_HOMEWORK, {
        "entry_id": "entry-a", "device_id": "device-a", "student_id": "student-a",
        "student_name": "Example student", "item_id": new.identity(),
        **new.as_dict(), "date_is_synthetic": False,
    })
    assert calls[1].args == (EVENT_NEW_MESSAGE, {
        "entry_id": "entry-a", "device_id": "device-a", "item_id": message.identity(),
        **message.as_dict(),
    })
    message.has_read = True
    tracker.async_process(snapshot([new], [message]))
    tracker.async_process(snapshot())
    tracker.async_process(snapshot([old, new], [message]))
    assert tracker.hass.bus.async_fire.call_count == 2


def test_first_successful_inbox_after_outage_is_silent(tracker):
    old = Message("Existing notice")
    tracker.async_process(snapshot(fresh=False))
    tracker.async_process(snapshot(messages=[old]))
    tracker.hass.bus.async_fire.assert_not_called()
    tracker.async_process(snapshot(messages=[old], fresh=False))
    tracker.async_process(snapshot(messages=[old, Message("New notice")]))
    assert tracker.hass.bus.async_fire.call_count == 1


def test_source_changes_seed_silently_and_synthetic_dates_do_not_renotify(tracker):
    old = HomeworkItem("Math", "Exercise 1", "2026-09-15")
    fallback = HomeworkItem("Math", "Exercise 1", "2026-09-15", date_is_synthetic=True)
    new = HomeworkItem("Math", "Exercise 2", "2026-09-16", date_is_synthetic=True)
    tracker.async_process(snapshot([old]))
    tracker.async_process(snapshot([fallback], full=False))
    fallback.date = "2026-09-16"
    tracker.async_process(snapshot([fallback], full=False))
    tracker.hass.bus.async_fire.assert_not_called()
    tracker.async_process(snapshot([fallback, new], full=False))
    assert tracker.hass.bus.async_fire.call_count == 1
    tracker.async_process(snapshot([old, HomeworkItem("Math", "Exercise 2", "2026-09-16")]))
    assert tracker.hass.bus.async_fire.call_count == 1


def test_new_students_and_reloads_seed_silently_accounts_are_independent(tracker):
    item = HomeworkItem("Math", "Exercise 1")
    tracker.async_process(snapshot())
    tracker.async_process(snapshot([item], student="student-b"))
    tracker.hass.bus.async_fire.assert_not_called()
    tracker.async_process(snapshot([item]))
    assert tracker.hass.bus.async_fire.call_count == 1
    other = SmartSchoolEvents(tracker.hass, "entry-b")
    other.async_process(snapshot())
    other.async_process(snapshot([item]))
    assert tracker.hass.bus.async_fire.call_args.args[1]["entry_id"] == "entry-b"
    reloaded = SmartSchoolEvents(tracker.hass, "entry-a")
    reloaded.async_process(snapshot([item]))
    assert tracker.hass.bus.async_fire.call_count == 2


def test_events_run_after_executor_returns_and_failed_fetch_does_not_advance(tracker):
    coordinator = SmartSchoolCoordinator.__new__(SmartSchoolCoordinator)
    coordinator._events = tracker
    coordinator.hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=snapshot()))
    asyncio.run(coordinator._async_update_data())
    coordinator.hass.async_add_executor_job.side_effect = RuntimeError("fetch failed")
    with pytest.raises(RuntimeError, match="fetch failed"):
        asyncio.run(coordinator._async_update_data())
    tracker.hass.bus.async_fire.assert_not_called()
    coordinator.hass.async_add_executor_job.side_effect = None
    coordinator.hass.async_add_executor_job.return_value = snapshot([HomeworkItem("Math", "New")])
    asyncio.run(coordinator._async_update_data())
    assert tracker.hass.bus.async_fire.call_count == 1


@pytest.mark.parametrize("kind", ["homework", "messages"])
def test_publish_failure_keeps_data_and_retries_only_unpublished_items(tracker, caplog, kind):
    tracker.async_process(snapshot())
    items = ([HomeworkItem("Math", "First"), HomeworkItem("Math", "Second")]
             if kind == "homework" else [Message("First"), Message("Second")])
    data = snapshot(**{kind: items})
    coordinator = SmartSchoolCoordinator.__new__(SmartSchoolCoordinator)
    coordinator._events = tracker
    coordinator.hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=data))
    tracker.hass.bus.async_fire.side_effect = [None, RuntimeError("private-content-sentinel")]

    assert asyncio.run(coordinator._async_update_data()) is data
    assert "Event publishing failed" in caplog.text
    assert "private-content-sentinel" not in caplog.text

    tracker.hass.bus.async_fire.reset_mock(side_effect=True)
    assert asyncio.run(coordinator._async_update_data()) is data
    tracker.hass.bus.async_fire.assert_called_once()
    assert tracker.hass.bus.async_fire.call_args.args[1]["item_id"] == items[1].identity()
    asyncio.run(coordinator._async_update_data())
    tracker.hass.bus.async_fire.assert_called_once()
